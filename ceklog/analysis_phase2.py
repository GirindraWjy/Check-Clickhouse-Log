import re

from ceklog.analysis import NO_MATCHES_MESSAGE, summarize_restart_cause

QUERY_MEMORY_PATTERN = re.compile(
    r"memory limit exceeded|overcommit tracker|"
    r"peak memory usage|maximum memory usage",
    re.I,
)
QUERY_ID_PATTERN = re.compile(r"\bquery_id\b", re.I)
MAX_QUERY_EVIDENCE_LINES = 3


def summarize_restart_causes(
    phase1_logs: str,
    phase2_logs: str,
) -> str:
    logs = "\n".join(
        log.strip() for log in (phase1_logs, phase2_logs) if log.strip()
    )
    summary = summarize_restart_cause(
        logs or NO_MATCHES_MESSAGE,
        oom_log_text=phase2_logs,
    )
    query_evidence = _query_memory_evidence(logs)
    if not query_evidence:
        return summary

    evidence_text = "\n".join(f"- {line[:300]}" for line in query_evidence)
    query_section = (
        "\n\nKonteks penyebab OOM (jika tercatat; belum tentu penyebab langsung):\n"
        f"```\n{evidence_text}\n```"
    )
    return summary.replace("\n\nCatatan:", f"{query_section}\n\nCatatan:")


def _query_memory_evidence(log_text: str) -> list[str]:
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    memory_indexes = [
        index for index, line in enumerate(lines) if QUERY_MEMORY_PATTERN.search(line)
    ]
    if not memory_indexes:
        return []

    evidence = [lines[memory_indexes[-1]]]
    query_id_line = next(
        (
            line
            for line in lines[memory_indexes[-1] + 1 : memory_indexes[-1] + 13]
            if QUERY_ID_PATTERN.search(line)
        ),
        None,
    )
    if query_id_line is not None and query_id_line not in evidence:
        evidence.append(query_id_line)
    return evidence[:MAX_QUERY_EVIDENCE_LINES]
