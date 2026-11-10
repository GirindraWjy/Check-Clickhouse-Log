import base64
import csv
import ipaddress
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openpyxl import Workbook
from openpyxl.styles import Font
import paramiko

from ceklog.analysis_phase2 import summarize_restart_causes

logger = logging.getLogger(__name__)

TIME_RANGE_PATTERN = re.compile(r"^([0-2]\d):([0-5]\d)-([0-2]\d):([0-5]\d)$")
CONFIDENCE_PATTERN = re.compile(r"^Keyakinan:\s*Tinggi\s*\((\d+)/100\)\.", re.M)
JOURNAL_TIMESTAMP_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})"
)
RESTART_EVENT_PATTERN = re.compile(
    r"(?:out of memory:\s*killed process\s+\d+\s+\([^)]*clickhouse|"
    r"(?:clickhouse|clickhouse-server)[^\n]*"
    r"(?:starting|started|stopping|stopped|restart|main process exited|"
    r"failed|shutdown|signal 9|signal 15)|"
    r"(?:starting|started|stopping|stopped|restart|main process exited|"
    r"failed|shutdown|signal 9|signal 15)[^\n]*"
    r"(?:clickhouse|clickhouse-server))",
    re.I,
)
CLICKHOUSE_QUERY_VIEWS = (
    "WITH toDateTime('{event_time}', 'UTC') AS restart_time "
    "SELECT view_name, "
    "count() runs, "
    "formatReadableSize(max(peak_memory_usage)) peak_mem, "
    "round(avg(view_duration_ms)) avg_ms, "
    "sum(read_rows) read_rows "
    "FROM system.query_views_log "
    "WHERE event_time > restart_time - INTERVAL 1 HOUR "
    "AND event_time <= restart_time "
    "GROUP BY view_name "
    "ORDER BY max(peak_memory_usage) DESC "
    "LIMIT 20 "
    "FORMAT CSVWithNames"
)
CLICKHOUSE_QUERY_LOG = (
    "WITH toDateTime('{event_time}', 'UTC') AS restart_time "
    "SELECT replaceAll(query, '\\n', ' ') AS query, "
    "event_time, "
    "formatReadableSize(memory_usage) AS peak_memory "
    "FROM system.query_log "
    "WHERE event_time > restart_time - INTERVAL 1 HOUR "
    "AND event_time <= restart_time "
    "ORDER BY memory_usage DESC "
    "FORMAT CSVWithNames"
)


@dataclass(frozen=True)
class CheckResult:
    summary: str
    query_evidence_workbook: bytes | None = None
    query_evidence_filename: str | None = None


class ClickHouseQueryError(RuntimeError):
    pass

PHASE1_COMMAND = (
    "bash -o pipefail -c 'tail -n 2000 "
    "/var/log/clickhouse-server/clickhouse-server.log | "
    "grep -Ei -A 12 \"Error|Fatal|Exception|Killed|OOM|Down|Shutdown|signal|"
    "crash|abort|out of memory|no space left|read-only file system|"
    "cannot allocate memory|Memory limit exceeded|OvercommitTracker|"
    "Peak memory usage|maximum memory usage\"'"
)

JOURNAL_FILTER = (
    'grep -Ei "clickhouse|systemctl|stop|restart|start|Out of memory|'
    'oom_reaper|oom-kill|Killed process|memory cgroup out of memory"'
)


def parse_time_range(value: str) -> tuple[str, str]:
    match = TIME_RANGE_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("Format waktu harus HH:MM-HH:MM, contoh: 01:00-01:15.")

    start_hour, start_minute, end_hour, end_minute = (
        int(part) for part in match.groups()
    )
    if start_hour > 23 or end_hour > 23:
        raise ValueError("Jam harus berada di antara 00:00 dan 23:59.")

    start = f"{start_hour:02d}:{start_minute:02d}"
    end = f"{end_hour:02d}:{end_minute:02d}"
    if end <= start:
        raise ValueError(
            "Waktu akhir harus setelah waktu mulai pada hari yang sama."
        )
    return start, end


def fetch_clickhouse_errors(
    server: str,
    username: str,
    password: str,
    start_time: str,
    end_time: str,
    db_host: str = "",
    db_scheme: str = "",
    db_port: str = "",
    db_username: str = "",
    db_password: str = "",
) -> CheckResult:
    client = paramiko.SSHClient()
    client.load_system_host_keys()

    try:
        client.connect(
            hostname=server,
            username=username,
            password=password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        phase1_logs = _run_log_command(client, PHASE1_COMMAND)
        phase2_logs = _run_log_command(
            client, _build_phase2_command(start_time, end_time)
        )
    finally:
        client.close()

    summary = summarize_restart_causes(phase1_logs, phase2_logs)
    confidence_match = CONFIDENCE_PATTERN.search(summary)
    if confidence_match is None or int(confidence_match.group(1)) < 70:
        return CheckResult(summary)

    restart_time = _latest_restart_time(phase2_logs)
    if restart_time is None:
        return CheckResult(
            summary
            + "\n\nEvidence query_views_log tidak diambil: confidence tinggi, "
            "tetapi waktu restart bertimestamp tidak ditemukan pada journal."
        )

    missing = [
        name
        for name, value in (
            ("db_host", db_host),
            ("db_scheme", db_scheme),
            ("db_port", db_port),
            ("db_usn", db_username),
        )
        if not value.strip()
    ]
    if missing:
        return CheckResult(
            summary
            + "\n\nEvidence query_views_log tidak diambil: konfigurasi .env "
            f"belum lengkap ({', '.join(missing)})."
        )

    try:
        workbook_content = _fetch_query_evidence_workbook(
            db_host,
            db_scheme,
            db_port,
            db_username,
            db_password,
            restart_time,
        )
    except ClickHouseQueryError as exc:
        logger.warning("Gagal mengambil evidence query ClickHouse: %s", exc)
        return CheckResult(
            summary + f"\n\nEvidence query ClickHouse gagal diambil: {exc}"
        )

    filename_time = re.sub(r"\D", "", restart_time)
    return CheckResult(
        summary,
        query_evidence_workbook=workbook_content,
        query_evidence_filename=f"clickhouse_query_evidence_{filename_time}.xlsx",
    )


def _latest_restart_time(journal_logs: str) -> str | None:
    event_time = None
    for line in journal_logs.splitlines():
        if not RESTART_EVENT_PATTERN.search(line):
            continue
        timestamp_match = JOURNAL_TIMESTAMP_PATTERN.match(line.strip())
        if timestamp_match is not None:
            timestamp = timestamp_match.group(1)
            if re.search(r"[+-]\d{4}$", timestamp):
                timestamp = f"{timestamp[:-2]}:{timestamp[-2:]}"
            try:
                parsed_time = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            event_time = parsed_time.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    return event_time


def _fetch_query_evidence_workbook(
    host: str,
    database: str,
    port: str,
    username: str,
    password: str,
    restart_time: str,
) -> bytes:
    if not database.strip():
        raise ClickHouseQueryError("db_scheme harus berisi nama database ClickHouse.")
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        restart_time,
    ):
        raise ClickHouseQueryError("Waktu restart tidak valid.")

    try:
        port_number = int(port)
    except ValueError as exc:
        raise ClickHouseQueryError("db_port harus berupa angka 1-65535.") from exc
    if not 1 <= port_number <= 65535:
        raise ClickHouseQueryError("db_port harus berupa angka 1-65535.")

    normalized_host = host.strip()
    if ":" in normalized_host:
        try:
            normalized_host = f"[{ipaddress.IPv6Address(normalized_host.strip('[]'))}]"
        except ipaddress.AddressValueError as exc:
            raise ClickHouseQueryError("db_host tidak valid.") from exc
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", normalized_host):
        raise ClickHouseQueryError("db_host tidak valid.")

    authorization = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    query_parameters = urlencode({"database": database.strip()})

    workbook = Workbook()
    query_views_sheet = workbook.active
    query_views_sheet.title = "query_views_log"
    query_log_sheet = workbook.create_sheet("query_log")

    for sheet, query_template, query_name in (
        (query_views_sheet, CLICKHOUSE_QUERY_VIEWS, "query_views_log"),
        (query_log_sheet, CLICKHOUSE_QUERY_LOG, "query_log"),
    ):
        query = query_template.format(event_time=restart_time)
        request = Request(
            f"http://{normalized_host}:{port_number}/?{query_parameters}",
            data=query.encode("utf-8"),
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                csv_content = response.read().decode("utf-8-sig")
        except HTTPError as exc:
            response_text = exc.read(500).decode("utf-8", errors="replace").strip()
            details = f"HTTP {exc.code}"
            if response_text:
                details += f": {response_text}"
            raise ClickHouseQueryError(f"{query_name}: {details}") from exc
        except URLError as exc:
            raise ClickHouseQueryError(
                f"{query_name}: koneksi gagal: {exc.reason}"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ClickHouseQueryError(
                f"{query_name}: koneksi gagal: {exc}"
            ) from exc

        rows = csv.reader(io.StringIO(csv_content, newline=""))
        try:
            headers = next(rows)
        except StopIteration as exc:
            raise ClickHouseQueryError(
                f"{query_name}: ClickHouse mengembalikan CSV kosong."
            ) from exc
        if not headers:
            raise ClickHouseQueryError(
                f"{query_name}: header CSV tidak ditemukan."
            )

        sheet.append(headers)
        for cell in sheet[1]:
            cell.data_type = "s"
        for row_number, row in enumerate(rows, start=2):
            if len(row) != len(headers):
                raise ClickHouseQueryError(
                    f"{query_name}: format CSV tidak valid pada baris {row_number}."
                )
            if row_number > 1_048_576:
                raise ClickHouseQueryError(
                    f"{query_name}: jumlah baris melebihi batas maksimum Excel."
                )
            sheet.append(row)
            for cell in sheet[row_number]:
                cell.data_type = "s"

        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _build_phase2_command(start_time: str, end_time: str) -> str:
    return (
        "bash -o pipefail -c 'today=$(date +%F); journalctl "
        f'--since "$today {start_time}:00" '
        f'--until "$today {end_time}:59" --output=short-iso '
        f"| {JOURNAL_FILTER}'"
    )


def _run_log_command(client: paramiko.SSHClient, command: str) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=60)
    output = stdout.read().decode("utf-8", errors="replace").strip()
    error_output = stderr.read().decode("utf-8", errors="replace").strip()
    exit_code = stdout.channel.recv_exit_status()

    if exit_code == 1 and not error_output:
        return ""
    if exit_code != 0:
        details = error_output or f"Perintah SSH berakhir dengan kode {exit_code}."
        raise RuntimeError(details)
    return output
