import re

NO_MATCHES_MESSAGE = "Tidak ada log yang cocok dalam 2000 baris terakhir."

MAX_EVIDENCE_LINES = 5
MAX_EVIDENCE_LENGTH = 1000

STACK_FRAME = re.compile(r"^\d+\s+\S")
LOG_TIMESTAMP = re.compile(r"^\d{4}[.-]\d{2}[.-]\d{2}[T ]")
OOM_EVIDENCE = re.compile(
    r"out of memory|oom[-_ ]kill|oom_reaper|"
    r"killed process \d+.*clickhouse|memory cgroup out of memory",
    re.I,
)
OOM_KILLED_PROCESS = re.compile(
    r"out of memory:\s*killed process\s+(\d+)\s+\(([^)]*clickhouse)",
    re.I,
)
OOM_PROCESS_ID = re.compile(
    r"(?:killed process|reaped process|pid=)\s*(\d+)",
    re.I,
)
LOG_TIME = re.compile(
    r"^(?:"
    r"\d{4}[.-]\d{2}[.-]\d{2}[T ]"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+)"
    r"(\d{2}:\d{2}:\d{2})"
)

ERROR_INDICATOR = re.compile(
    r"<Error>|<Fatal>|<Exception>|\b(?:OOM|Killed)\b",
    re.I,
)


# =========================================================
# CAUSE RULES
# =========================================================

CAUSE_RULES = (
    (
        re.compile(
            r"oom-kill|out of memory|memory cgroup out of memory|"
            r"killed process \d+.*clickhouse",
            re.I,
        ),
        "Kemungkinan besar proses terkena OOM killer(kehabisan memori).",
        "oom",
    ),
    (
        re.compile(
            r"no space left on device|disk is full|read-only file system",
            re.I,
        ),
        "Kemungkinan ada masalah kapasitas atau kondisi filesystem "
        "yang mengganggu operasi ClickHouse.",
        "disk",
    ),
    (
        re.compile(
            r"sigkill|signal 9|killed by signal 9",
            re.I,
        ),
        "Proses dihentikan paksa (SIGKILL); pemicunya belum diketahui.",
        "sigkill",
    ),
    (
        re.compile(
            r"uncaught exception|fatal|std::terminate|terminate called|"
            r"segmentation fault|sigsegv|signal 11|sigabrt|signal 6|core   dumped",
            re.I,
        ),
        "Ada indikasi kuat proses ClickHouse mengalami kegagalan fatal/crash.",
        "crash",
    ),
    (
        re.compile(
            r"received signal 15|sigterm|signal 15|shutdown complete|"
            r"shutting down|will shutdown",
            re.I,
        ),
        "Terlihat indikasi shutdown teratur/SIGTERM.",
        "shutdown",
    ),
)


# =========================================================
# CONFIDENCE SCORING
# =========================================================

CONFIDENCE_RULES = {
    "oom": (
        (r"out of memory: killed process \d+ \([^)]*clickhouse", 100),
        (r"oom-kill", 40),
        (r"out of memory", 30),
        (r"memory cgroup out of memory", 40),
        (r"killed process \d+.*clickhouse", 45),
    ),
    "disk": (
        (r"no space left on device", 50),
        (r"disk is full", 45),
        (r"read-only file system", 35),
    ),
    "crash": (
        (r"segmentation fault|sigsegv", 50),
        (r"core dumped", 30),
        (r"std::terminate|terminate called", 35),
        (r"uncaught exception", 25),
        (r"signal 11|sigabrt|signal 6", 40),
    ),
    "sigkill": ((r"sigkill|signal 9|killed by signal 9", 50),),
    "shutdown": (
        (r"received signal 15|sigterm|signal 15", 40),
        (r"shutdown complete", 30),
        (r"shutting down|will shutdown", 20),
    ),
}


def calculate_confidence(
    logs: list[str],
    cause: str,
) -> tuple[int, str]:

    score = 0

    rules = CONFIDENCE_RULES.get(cause, ())

    for pattern, weight in rules:
        if any(re.search(pattern, line, re.I) for line in logs):
            score += weight

    score = min(score, 100)

    if score >= 70:
        confidence = "Tinggi"
    elif score >= 40:
        confidence = "Sedang"
    else:
        confidence = "Rendah"

    return score, confidence


# =========================================================
# MAIN SUMMARY
# =========================================================


def summarize_restart_cause(
    log_text: str,
    oom_log_text: str | None = None,
) -> str:

    lines = [
        line.strip()
        for line in log_text.splitlines()
        if line.strip() and line.strip() != "--"
    ]
    lines = list(dict.fromkeys(lines))

    # No matching logs
    if not lines or log_text.strip() == NO_MATCHES_MESSAGE:

        evidence = ["Tidak ada baris log yang cocok dalam 2000 baris terakhir."]

        return (
            "Ringkasan pemeriksaan restart ClickHouse\n"
            "Dugaan: Belum dapat ditentukan.\n"
            "Keyakinan: Rendah (0/100).\n"
            "Bukti dari log:\n"
            f"```\n- {evidence[0]}\n```\n\n"
            "Catatan: Tidak ditemukan bukti yang cukup untuk "
            "menentukan penyebab restart. Periksa journal systemd "
            "dan log kernel pada waktu yang sama."
        )

    # Detect cause
    cause = ""
    incidents: list[tuple[str, list[str]]] = []
    for pattern, hypothesis, cause in CAUSE_RULES:

        matching_indexes = [
            index for index, line in enumerate(lines) if pattern.search(line)
        ]

        if matching_indexes:

            if cause == "oom":
                event_lines = [
                    line.strip()
                    for line in (oom_log_text or "").splitlines()
                    if line.strip() and line.strip() != "--"
                ]
                event_lines = list(dict.fromkeys(event_lines))
                if oom_log_text is not None:
                    incidents = _oom_incidents(event_lines)
                else:
                    incidents = _oom_incidents(lines)
                evidence = [
                    line
                    for incident in incidents
                    for line in incident[1]
                ]
            else:
                incidents = []
                evidence = []
            if not evidence:
                evidence = _evidence_around(lines, matching_indexes[-1])

            score, confidence = calculate_confidence(lines, cause)

            break

    else:

        cause = ""
        hypothesis = (
            "Penyebab restart belum teridentifikasi. "
            "Baris Error/Exception saja tidak membuktikan "
            "bahwa itu yang me-restart ClickHouse."
        )

        score = 15 if any(ERROR_INDICATOR.search(line) for line in lines) else 0

        confidence = "Rendah"

        matching_indexes = [
            index for index, line in enumerate(lines) if ERROR_INDICATOR.search(line)
        ]

        evidence = (
            _evidence_around(lines, matching_indexes[-1])
            if matching_indexes
            else lines[-MAX_EVIDENCE_LINES:]
        )

    # Format evidence
    recorded_at = ""
    formatted_evidence = ""
    if cause == "oom":
        if incidents:
            recorded_times = ", ".join(
                incident[0] for incident in incidents
            )
            recorded_at = f"Tercatat pada : {recorded_times}\n"
            formatted_evidence = "\n\n".join(
                f"{recorded_time}\n```\n"
                + "\n".join(f"- {_truncate(line)}" for line in incident_lines)
                + "\n```"
                for recorded_time, incident_lines in incidents
            )
    if not formatted_evidence:
        evidence_text = "\n".join(f"- {_truncate(line)}" for line in evidence)
        formatted_evidence = f"```\n{evidence_text}\n```"

    # Final output
    return (
        "Ringkasan pemeriksaan restart ClickHouse\n"
        f"Dugaan: {hypothesis}\n"
        f"Keyakinan: {confidence} ({score}/100).\n"
        f"{recorded_at}"
        "Bukti dari log:\n\n"
        f"{formatted_evidence}\n\n"
    )


def _oom_incidents(lines: list[str]) -> list[tuple[str, list[str]]]:
    kill_events: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = OOM_KILLED_PROCESS.search(line)
        seconds = _log_seconds(line)
        if match and seconds is not None:
            kill_events.append((index, seconds, match.group(1)))

    if not kill_events:
        return _cluster_oom_evidence(lines)

    incidents: list[tuple[str, list[str]]] = []
    for index, seconds, process_id in kill_events:
        incident_lines: list[str] = []
        for line in lines:
            if not OOM_EVIDENCE.search(line):
                continue
            line_seconds = _log_seconds(line)
            process_match = OOM_PROCESS_ID.search(line)
            same_process = bool(
                process_match and process_match.group(1) == process_id
            )
            near_event = (
                line_seconds is not None
                and _seconds_apart(line_seconds, seconds) <= 15
            )
            if near_event or (same_process and line_seconds is None):
                incident_lines.append(line)
        if lines[index] not in incident_lines:
            incident_lines.append(lines[index])
        incidents.append((_format_log_time(lines[index])[:5], incident_lines))

    return sorted(incidents, key=lambda incident: _clock_seconds(incident[0]) or 0)


def _cluster_oom_evidence(lines: list[str]) -> list[tuple[str, list[str]]]:
    incidents: list[tuple[str, list[str]]] = []
    for line in lines:
        if not OOM_EVIDENCE.search(line):
            continue
        recorded_time = _format_log_time(line)
        if not incidents:
            incidents.append((recorded_time[:5], [line]))
            continue

        previous_time = incidents[-1][0]
        current_seconds = _log_seconds(line)
        previous_seconds = _clock_seconds(previous_time)
        if (
            current_seconds is not None
            and previous_seconds is not None
            and _seconds_apart(current_seconds, previous_seconds) <= 15
        ):
            incidents[-1][1].append(line)
        else:
            incidents.append((recorded_time[:5], [line]))
    return incidents


def _format_log_time(line: str) -> str:
    match = LOG_TIME.match(line)
    return match.group(1) if match else "Waktu tidak diketahui"


def _log_seconds(line: str) -> int | None:
    time_text = _format_log_time(line)
    if time_text == "Waktu tidak diketahui":
        return None
    return _clock_seconds(time_text)


def _clock_seconds(time_text: str) -> int | None:
    match = re.match(r"^(\d{2}):(\d{2}):?(\d{2})?$", time_text)
    if not match:
        return None
    hour, minute, second = match.groups()
    return int(hour) * 3600 + int(minute) * 60 + int(second or 0)


def _seconds_apart(first: int, second: int) -> int:
    difference = abs(first - second)
    return min(difference, 24 * 60 * 60 - difference)


# =========================================================
# EVIDENCE EXTRACTION
# =========================================================


def _evidence_around(
    lines: list[str],
    start: int,
) -> list[str]:

    evidence = [lines[start]]

    for line in lines[start + 1 :]:

        if LOG_TIMESTAMP.match(line):
            break

        if STACK_FRAME.match(line) or len(evidence) == 1:
            evidence.append(line)

        if len(evidence) >= MAX_EVIDENCE_LINES:
            break

    return evidence


# =========================================================
# TEXT TRUNCATION
# =========================================================


def _truncate(line: str) -> str:

    if _utf16_length(line) <= MAX_EVIDENCE_LENGTH:
        return line

    truncated = []
    length = 0

    for character in line:

        units = 2 if ord(character) > 0xFFFF else 1

        if length + units > MAX_EVIDENCE_LENGTH - 3:
            break

        truncated.append(character)
        length += units

    return f"{''.join(truncated)}..."


def _utf16_length(text: str) -> int:

    return len(text.encode("utf-16-le")) // 2
