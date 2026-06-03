"""Step 0 verification: the audit logger writes a readable run_log.jsonl.

Runs with NO API calls.
"""

import json

from tailor.audit import AuditLogger, read_entries, utc_now_iso
from tailor.models import ReasoningEntry


def test_log_event_writes_one_jsonl_line(tmp_path):
    log_path = tmp_path / "run_log.jsonl"
    logger = AuditLogger(log_path)
    logger.log_event(
        phase="refinement_loop",
        event="critique_item_rejected",
        reasoning="JD explicitly requires metrics.",
        iteration=2,
        keyword_score=0.71,
        critique_score=7.4,
        rubric_version=2,
        ts="2026-06-03T14:23:01Z",
    )
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["phase"] == "refinement_loop"
    assert record["event"] == "critique_item_rejected"
    assert record["iteration"] == 2
    assert record["rubric_version"] == 2


def test_creates_parent_directory(tmp_path):
    nested = tmp_path / "outputs" / "run_x" / "run_log.jsonl"
    AuditLogger(nested).log_event("phase0", "started", "begin")
    assert nested.exists()


def test_append_and_read_round_trip(tmp_path):
    log_path = tmp_path / "run_log.jsonl"
    logger = AuditLogger(log_path)
    logger.log_event("phase0", "jd_parsed", "extracted 8 requirements", ts="t1")
    logger.log_event("phase1", "fit_assessed", "partial fit 0.74", ts="t2")
    logger.log_footer({"type": "run_complete", "total_usd": 0.0548, "iterations_run": 3})

    records = read_entries(log_path)
    assert len(records) == 3
    assert records[0]["event"] == "jd_parsed"
    assert records[1]["event"] == "fit_assessed"
    assert records[2]["type"] == "run_complete"

    # A reasoning record can be rehydrated into the typed object.
    entry = ReasoningEntry.from_dict(records[0])
    assert isinstance(entry, ReasoningEntry)
    assert entry.phase == "phase0"


def test_log_typed_entry(tmp_path):
    log_path = tmp_path / "run_log.jsonl"
    logger = AuditLogger(log_path)
    entry = ReasoningEntry(
        ts="2026-06-03T00:00:00Z",
        phase="phase2",
        event="draft_written",
        reasoning="drafted profile from generic CV",
    )
    logger.log(entry)
    assert read_entries(log_path)[0]["reasoning"] == "drafted profile from generic CV"


def test_read_missing_file_returns_empty(tmp_path):
    assert read_entries(tmp_path / "nope.jsonl") == []


def test_utc_now_iso_format():
    ts = utc_now_iso()
    # 'YYYY-MM-DDTHH:MM:SSZ'
    assert ts.endswith("Z")
    assert ts[4] == "-" and ts[10] == "T"
