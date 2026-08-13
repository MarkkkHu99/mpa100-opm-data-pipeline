# -*- coding: utf-8 -*-
"""Regression tests for routing, QA transactions and log recovery.

Run from the repository root with::

    python -m pytest -q tests/test_regressions.py

The suite covers fail-closed QA-name routing, transactional QA-state handling,
instrument-state isolation, persistent attention flags, report/state recovery,
duplicate handling and conservative log-integrity semantics.
"""
import json
import os
import re
import tempfile
import pytest
from datetime import datetime

import mpa_pipeline as mp


# --------------------------------------------------------------------------
# Defect 1 - QA name fail-closed routing
# --------------------------------------------------------------------------
def meta(name):
    return {"Chemical name": name, "Instrument serial number": "SN-TEST-01"}


@pytest.mark.parametrize("name", ["QA_VANILLIN", "QA_PHENACETIN", "QA_CAFFEINE",
                                  "qa_caffeine", "QA-vanillin", "QA vanillin"])
def test_valid_qa_names_resolve_to_standard(name):
    assert mp.FileHandler._has_qa_intent(meta(name)) is True
    assert mp.FileHandler._qa_standard_name(meta(name)) is not None


@pytest.mark.parametrize("name", [
    "QA_VANILIN",     # Representative misspelling (one L short).
    "qa_vanillin ",   # trailing space + case  -> still QA intent, valid std after strip
    "QA_XYZ",         # unknown QA_ name
    "QA_VANILLIN2",   # trailing digit -> not a bare reserved name
])
def test_qa_intent_detected_for_all_prefixed_names(name):
    # Every QA_-prefixed label is recognised as QA *intent*...
    assert mp.FileHandler._has_qa_intent(meta(name)) is True


@pytest.mark.parametrize("name", ["QA_VANILIN", "QA_XYZ", "QA_VANILLIN2"])
def test_unrecognised_qa_name_is_not_a_valid_standard(name):
    # ...but only exact reserved names resolve to a standard.
    assert mp.FileHandler._qa_standard_name(meta(name)) is None


def test_routing_gate_rejects_unrecognised_qa_name():
    """The core fail-closed rule: QA intent + no reserved match => reject."""
    for bad in ["QA_VANILIN", "QA_XYZ", "QA_VANILLIN2"]:
        m = meta(bad)
        would_reject = (mp.FileHandler._has_qa_intent(m)
                        and mp.FileHandler._qa_standard_name(m) is None)
        assert would_reject is True, f"{bad!r} should be rejected fail-closed"


def test_ordinary_student_name_is_not_qa_intent():
    for ok in ["vanillin", "aspirin", "1,4-diiodobenzene", "unknown sample"]:
        assert mp.FileHandler._has_qa_intent(meta(ok)) is False


def test_qa_name_error_is_raisable_and_is_a_valueerror():
    assert issubclass(mp.QANameError, ValueError)


# --------------------------------------------------------------------------
# Fixtures for Defect 2 - a minimal instrument QA state on disk
# --------------------------------------------------------------------------
def make_calibrator(tmp_path):
    config = {
        "instrument_accuracy_spec": {
            "ranges": [{"min_C": 0, "max_C": 400, "accuracy_C": 0.5}]
        },
        "standards": {"list": [
            {"name": "vanillin", "certified_clear_point_C": 83.0,
             "aliases": ["vanillin"]},
        ]},
        "calibration_curve": {"status": "measured",
                              "parameters": {"slope": 1.0, "intercept": 0.0},
                              "goodness_of_fit": {"r_squared": 1.0, "rmse": 0.0}},
        "calibration_data": {"date_performed": "2026-01-01",
                             "date_expires": "2026-07-01",
                             "recommended_recalibration_months": 6},
    }
    cfg = os.path.join(tmp_path, "qa_state.json")
    log = os.path.join(tmp_path, "qa_history.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(config, f)
    return mp.MeltingPointCalibrator(cfg, log)


def test_out_of_tolerance_sets_attention_required(tmp_path):
    cal = make_calibrator(str(tmp_path))
    # certified 83.0, tol 0.5 -> a 3 C low reading is well outside tolerance
    result = cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=True)
    assert result["drift"]["within_tolerance"] is False
    assert cal.qa_attention is not None
    assert cal.qa_attention["state"] == "attention_required"
    # and it is persisted into the config
    with open(cal.calib_file, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["calibration_curve"]["qa_attention"]["state"] == "attention_required"


def test_attention_shows_in_status_text(tmp_path):
    cal = make_calibrator(str(tmp_path))
    cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=True)
    text = mp._qa_status_text(cal)
    assert "ATTENTION REQUIRED" in text


def test_deferred_persistence_does_not_write_until_commit(tmp_path):
    """persist=False must not touch disk; commit_pending_state() flushes it."""
    cal = make_calibrator(str(tmp_path))
    before = os.path.getmtime(cal.calib_file)
    result = cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=False)
    assert result.get("deferred") is True
    # On-disk config still has NO attention flag yet (write was deferred)...
    with open(cal.calib_file, encoding="utf-8") as f:
        assert "qa_attention" not in json.load(f).get("calibration_curve", {})
    # ...until we explicitly commit (this is what the pipeline does *after* the
    # report directory has been published).
    cal.commit_pending_state()
    with open(cal.calib_file, encoding="utf-8") as f:
        assert json.load(f)["calibration_curve"]["qa_attention"]["state"] \
            == "attention_required"


def test_in_tolerance_run_does_not_silently_clear_prior_attention(tmp_path):
    cal = make_calibrator(str(tmp_path))
    cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=True)   # trip it
    assert cal.qa_attention is not None
    cal.record_standard_run("vanillin", 83.0, ramp_rate=1.0, persist=True)   # good run
    assert cal.qa_attention is not None
    assert cal.clear_qa_attention("qa-admin", "reviewed repeat and maintenance record")
    assert cal.qa_attention is None


def test_deferred_abort_restores_memory_and_clears_pending(tmp_path):
    cal = make_calibrator(str(tmp_path))
    before = json.loads(json.dumps(cal.config))
    cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=False)
    assert cal.qa_attention is not None
    cal.abort_pending_state()
    assert cal.config == before
    assert cal.qa_attention is None
    assert cal._pending_state is None


def test_persistence_failure_raises_and_restores_state(tmp_path, monkeypatch):
    cal = make_calibrator(str(tmp_path))
    before_disk = json.loads(open(cal.calib_file, encoding="utf-8").read())
    monkeypatch.setattr(cal, "_append_audit_log",
                        lambda entry: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(mp.QAStatePersistenceError):
        cal.record_standard_run("vanillin", 80.0, ramp_rate=1.0, persist=True)
    after_disk = json.loads(open(cal.calib_file, encoding="utf-8").read())
    assert after_disk == before_disk
    assert cal.qa_attention is None
    assert cal._pending_state is None


def test_new_instrument_state_is_blank_even_if_template_is_trusted(tmp_path, monkeypatch):
    template = {
        "instrument_accuracy_spec": {"ranges": []},
        "standards": {"list": [{
            "name": "vanillin", "certified_clear_point_C": 83.0,
            "expected_start_C": 78.0, "expected_stop_C": 88.0,
            "measured_clear_points_C": [83.0], "measured_mean_C": 83.0,
        }]},
        "calibration_curve": {"status": "measured",
                              "parameters": {"slope": 1.0, "intercept": 0.0}},
        "calibration_data": {"date_performed": "2026-01-01",
                             "date_expires": "2030-01-01"},
    }
    template_path = tmp_path / "trusted_template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    monkeypatch.setattr(mp, "CALIBRATION_FILE", str(template_path))
    monkeypatch.setattr(mp, "QA_STATE_ROOT", str(tmp_path / "states"))
    cal = mp.FileHandler()._get_calibrator({"Instrument serial number": "NEW-001"})
    assert cal.curve_status != "measured"
    assert cal.calib_valid is False
    assert "date_expires" not in cal.config["calibration_data"]
    assert "measured_clear_points_C" not in cal.standards[0]


def _qa_meta(start="78.0 C", stop="88.0 C"):
    return {
        "Start temperature": start,
        "Stop temperature": stop,
        "Onset point (left)": "81.5 C", "Clear point (left)": "82.9 C",
        "Onset point (center)": "81.6 C", "Clear point (center)": "83.0 C",
        "Onset point (right)": "81.7 C", "Clear point (right)": "83.1 C",
    }


def test_qa_start_stop_are_checked_against_standard_configuration():
    standard = {"expected_start_C": 78.0, "expected_stop_C": 88.0,
                "ramp_rate_C_min": 1.0, "temperature_tolerance_C": 0.11,
                "three_capillary_max_spread_C": 0.3, "max_melting_range_C": 2.0}
    checks = mp.FileHandler._validate_standard_run(
        _qa_meta(), 1.0, "vanillin", standard)
    assert checks["observed_start_C"] == 78.0
    with pytest.raises(ValueError, match="must start"):
        mp.FileHandler._validate_standard_run(
            _qa_meta(start="77.0 C"), 1.0, "vanillin", standard)


def _write_minimal_txt(path, chemical, rate=1.0, start=78.0, stop=88.0):
    path.write_text(
        f"Acquired on: 2026-08-11 13:00:00\n"
        f"Chemical name: {chemical}\n"
        f"Start temperature: {start:.1f} C\nStop temperature: {stop:.1f} C\n"
        f"Heating rate: {rate:.1f} C/min\nInstrument serial number: SN-TXN-01\n"
        "Onset point (left): 81.5 C\nClear point (left): 82.9 C\n"
        "Onset point (center): 81.6 C\nClear point (center): 83.0 C\n"
        "Onset point (right): 81.7 C\nClear point (right): 83.1 C\n"
        "Time(s)\tTemp(C)\tLeft\tCenter\tRight\n"
        "0\t78\t0.1\t0.2\t0.3\n[End of document]\n",
        encoding="utf-8")
    return path


def _configure_pipeline_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "OUTPUT_FOLDER", str(tmp_path / "outputs"))
    monkeypatch.setattr(mp, "PIPELINE_LOG_ROOT", str(tmp_path / "pipeline_logs"))
    monkeypatch.setattr(mp, "QA_STATE_ROOT", str(tmp_path / "states"))
    monkeypatch.setattr(mp, "CALIBRATION_FILE",
                        str(mp.BASE_DIR / "config" / "qa_template.json"))


def test_actual_routing_rejects_unrecognised_qa_name(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "bad_qa.txt", "QA_VANILIN")
    assert mp.FileHandler().process_file(str(source)) is False
    output = tmp_path / "outputs"
    assert not output.exists() or not any(output.rglob("*"))


def test_report_publish_failure_rolls_back_disk_and_memory(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "qa.txt", "QA_VANILLIN")
    real_replace = mp.os.replace

    def fail_directory_publish(src, dst):
        if os.path.isdir(src):
            raise OSError("simulated report-directory publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(mp.os, "replace", fail_directory_publish)
    handler = mp.FileHandler()
    assert handler.process_file(str(source)) is False
    cal = handler.calibrators["SN-TXN-01"]
    assert not cal.standards[0].get("measured_clear_points_C")
    saved = json.loads(open(cal.calib_file, encoding="utf-8").read())
    assert not saved["standards"]["list"][0].get("measured_clear_points_C")
    assert not any((tmp_path / "outputs").glob("20*"))


def test_state_commit_failure_does_not_publish_report(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "qa_state_fail.txt", "QA_VANILLIN")

    def fail_state_commit(self, audit_entry=None):
        raise mp.QAStatePersistenceError("simulated state persistence failure")

    monkeypatch.setattr(mp.MeltingPointCalibrator, "_persist_state", fail_state_commit)
    handler = mp.FileHandler()
    assert handler.process_file(str(source)) is False
    output = tmp_path / "outputs"
    assert not output.exists() or not any(output.glob("20*"))
    cal = handler.calibrators["SN-TXN-01"]
    assert cal._pending_state is None
    assert not cal.standards[0].get("measured_clear_points_C")


# --------------------------------------------------------------------------
# Defect 3 - corrupt log line must not fabricate 'interrupted'
# --------------------------------------------------------------------------
def write_log(tmp_path, lines):
    path = os.path.join(tmp_path, "pipeline_2026-08-11.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _terminals_by_outcome(root):
    outcomes = {}
    for fn in os.listdir(root):
        with open(os.path.join(root, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "terminal" and rec.get("detected_on_restart"):
                    outcomes.setdefault(rec["outcome"], []).append(rec["run_id"])
    return outcomes


def test_clean_log_missing_terminal_is_interrupted(tmp_path):
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1", "source_file": "a.opm"}),
        # no terminal for R1, and log is clean
    ])
    res = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    assert res["interrupted"] == 1
    assert res["integrity_unknown"] == 0
    assert "R1" in _terminals_by_outcome(str(tmp_path)).get("interrupted", [])


def test_corrupt_terminal_line_yields_integrity_unknown_not_interrupted(tmp_path):
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1", "source_file": "a.opm"}),
        '{"event": "terminal", "run_id": "R1", "outcome": "succ',  # <-- corrupt terminal
    ])
    res = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    assert res["malformed_lines"] == 1
    assert res["integrity_unknown"] == 1
    assert res["interrupted"] == 0
    outcomes = _terminals_by_outcome(str(tmp_path))
    assert "R1" in outcomes.get("integrity_unknown", [])
    assert "R1" not in outcomes.get("interrupted", [])


def test_corruption_raises_operator_visible_integrity_warning(tmp_path):
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1"}),
        "{ this is not valid json",
    ])
    mp.recover_incomplete_pipeline_runs(str(tmp_path))
    found = False
    for fn in os.listdir(str(tmp_path)):
        with open(os.path.join(str(tmp_path), fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "integrity_warning":
                    assert rec.get("severity") == "operator_action_required"
                    found = True
    assert found, "an operator-visible integrity_warning must be logged"


def test_log_corruption_is_scoped_to_its_daily_file(tmp_path):
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1"}),
        "{bad json",
    ])
    other = tmp_path / "pipeline_2026-08-12.txt"
    other.write_text(json.dumps({"event": "started", "run_id": "R2"}) + "\n",
                     encoding="utf-8")
    res = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    outcomes = _terminals_by_outcome(str(tmp_path))
    assert res["integrity_unknown"] == 1
    assert res["interrupted"] == 1
    assert "R1" in outcomes.get("integrity_unknown", [])
    assert "R2" in outcomes.get("interrupted", [])


def test_log_recovery_is_idempotent(tmp_path):
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1"}),
        "{bad json",
    ])
    first = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    second = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    assert first["integrity_unknown"] == 1
    assert first["warnings_added"] == 1
    assert second["integrity_unknown"] == 0
    assert second["warnings_added"] == 0


def test_log_recovery_is_idempotent_across_calendar_days(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mp, "_pipeline_timestamp", lambda: "2026-08-13T09:00:00.000+00:00")
    write_log(str(tmp_path), [
        json.dumps({"event": "started", "run_id": "R1"}),
        "{bad json",
    ])
    first = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    second = mp.recover_incomplete_pipeline_runs(str(tmp_path))
    assert (tmp_path / "pipeline_2026-08-13.txt").exists()
    assert first["integrity_unknown"] == 1
    assert first["warnings_added"] == 1
    assert second["integrity_unknown"] == 0
    assert second["warnings_added"] == 0
    assert second["interrupted"] == 0


def test_pending_staging_manifest_is_not_a_committed_duplicate(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    outputs = tmp_path / "outputs"
    pending = outputs / ".20260811_QA_VANILLIN.pending-xyz"
    pending.mkdir(parents=True)
    (pending / "run_manifest.json").write_text(
        json.dumps({"source_sha256": "abc123"}), encoding="utf-8")
    assert mp.FileHandler._find_duplicate("abc123") is None

    final = outputs / "20260811_QA_VANILLIN"
    final.mkdir()
    (final / "run_manifest.json").write_text(
        json.dumps({"source_sha256": "abc123"}), encoding="utf-8")
    assert mp.FileHandler._find_duplicate("abc123") == str(final)


def test_interrupted_publication_can_be_reprocessed(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "teach.txt", "aspirin")
    real_replace = mp.os.replace

    def hard_interrupt(src, dst):
        if os.path.isdir(src):
            raise KeyboardInterrupt("simulated power loss before publication")
        return real_replace(src, dst)

    monkeypatch.setattr(mp.os, "replace", hard_interrupt)
    with pytest.raises(KeyboardInterrupt):
        mp.FileHandler().process_file(str(source))
    monkeypatch.setattr(mp.os, "replace", real_replace)

    # The leftover staging directory must not mask the retry.
    assert mp.FileHandler().process_file(str(source)) is True
    finals = [p for p in (tmp_path / "outputs").iterdir()
              if p.is_dir() and not p.name.startswith(".")]
    assert len(finals) == 1


def test_orphaned_staging_dirs_are_discarded(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    outputs = tmp_path / "outputs"
    orphan = outputs / ".20260811_run.pending-abc"
    orphan.mkdir(parents=True)
    (orphan / "run_manifest.json").write_text("{}", encoding="utf-8")
    final = outputs / "20260811_run"
    final.mkdir()
    (final / "run_manifest.json").write_text("{}", encoding="utf-8")

    assert mp.discard_orphaned_staging_dirs() == 1
    assert not orphan.exists()
    assert final.exists()


def test_startup_settles_uncommitted_qa_journal(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    pending = outputs / ".20260811_QA_VANILLIN.pending-abc"
    pending.mkdir()
    state_dir = tmp_path / "states" / "SN-TXN-01"
    state_dir.mkdir(parents=True)
    (state_dir / "qa_state.json").write_text(
        json.dumps({"standards": {"list": []},
                    "instrument_accuracy_spec": {"ranges": []}}),
        encoding="utf-8")
    (state_dir / "qa_transaction.json").write_text(
        json.dumps({
            "transaction_id": "qa-R1",
            "pending_report_dir": str(pending.resolve()),
            "final_report_dir": str((outputs / "20260811_QA_VANILLIN").resolve()),
        }), encoding="utf-8")

    result = mp.FileHandler().recover_all_qa_transactions()

    assert result["recovered"] == 1
    assert result["failed"] == []
    # Not committed: the staging directory and the journal are both discarded.
    assert not pending.exists()
    assert not (state_dir / "qa_transaction.json").exists()


def test_qa_journal_cleanup_failure_still_reports_success(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "qa.txt", "QA_VANILLIN")
    real_unlink = mp.os.unlink

    def lock_journal(path, *args, **kwargs):
        if str(path).endswith("qa_transaction.json"):
            raise OSError("simulated journal lock")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(mp.os, "unlink", lock_journal)
    assert mp.FileHandler().process_file(str(source)) is True

    outcomes = []
    for log in (tmp_path / "pipeline_logs").iterdir():
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("event") == "terminal":
                outcomes.append(rec["outcome"])
    assert outcomes == ["success"]


def test_file_moved_into_folder_is_ingested(tmp_path, monkeypatch):
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    source = _write_minimal_txt(tmp_path / "moved.txt", "aspirin")
    seen = []
    monkeypatch.setattr(mp, "wait_for_file_ready", lambda *a, **kw: True)
    monkeypatch.setattr(mp.FileHandler, "process_file",
                        lambda self, path, **kw: (seen.append(path), True)[1])

    class _MovedEvent:
        is_directory = False
        src_path = str(tmp_path / "partial.tmp")
        dest_path = str(source)

    mp.FileHandler().on_moved(_MovedEvent())
    assert seen == [str(source)]


@pytest.mark.parametrize("start,months,expected", [
    ("2026-08-31", 6, "2027-02-28"),
    ("2026-03-31", 6, "2026-09-30"),
    ("2026-10-31", 6, "2027-04-30"),
    ("2026-01-15", 6, "2026-07-15"),
    ("2024-08-31", 6, "2025-02-28"),
    ("2026-12-31", 1, "2027-01-31"),
])
def test_recalibration_expiry_clamps_to_month_end(start, months, expected):
    moment = datetime.strptime(start, "%Y-%m-%d")
    assert mp._add_months(moment, months).strftime("%Y-%m-%d") == expected


def _pdf_page_count(path):
    """Count page objects in a ReportLab-produced PDF without extra dependencies."""
    return len(re.findall(rb"/Type\s*/Page[^s]", path.read_bytes()))


_QA_TEXT_SHORT = "Instrument QA calibration has not yet been performed on this unit."
_QA_TEXT_ATTENTION = (
    "ATTENTION REQUIRED - a reference-standard run was out of tolerance on "
    "2026-08-01 (TOC 0.42 C, tol +/-0.30 C). The previous fit is retained for "
    "reference only and must not be relied on until reviewed.")


@pytest.mark.parametrize("filename,qa_text", [
    ("baseline.txt", _QA_TEXT_SHORT),
    ("attention.txt", _QA_TEXT_ATTENTION),
    ("GroupB-Bench4-2026-08-11-run17-repeat-after-recalibration.txt",
     _QA_TEXT_ATTENTION),
    # Far longer than any status text the code produces today, so the plot has
    # to be scaled down rather than merely fitted.
    ("overflowing.txt", " ".join([_QA_TEXT_ATTENTION] * 3)),
])
def test_channel_report_fits_one_page(tmp_path, monkeypatch, filename, qa_text):
    """The report must stay on one page across the content that varies in height.

    The information table wraps, so QA status text and source-file length decide
    the overall height. A layout tuned only to the shortest case silently spills
    onto a second page once an instrument raises a QA alert.
    """
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_qa_status_text", lambda calib: qa_text)
    source = _write_minimal_txt(tmp_path / filename, "1,4-diiodobenzene")

    assert mp.FileHandler().process_file(str(source)) is True

    reports = sorted((tmp_path / "outputs").rglob("*.pdf"))
    assert len(reports) == 3, "expected one report per channel"
    for report in reports:
        assert _pdf_page_count(report) == 1, (
            f"{filename}: {report.name} spilled onto a second page")


class _FakeObserver:
    """Stands in for the watchdog observer so main() can be driven in-process."""

    def schedule(self, handler, path, recursive=False):
        return None

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, timeout=None):
        return None


def _stop_watcher(seconds):
    raise KeyboardInterrupt


def test_startup_publishes_committed_report_before_sweeping_staging(
        tmp_path, monkeypatch):
    """Startup must settle QA journals before discarding staging directories.

    A run interrupted between the state commit and the publication leaves the
    only copy of the report in a staging directory. Sweeping first would delete
    it while the state still claims the transaction succeeded, and no journal
    would survive to report the loss.
    """
    _configure_pipeline_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "MONITOR_FOLDER", str(tmp_path / "monitor"))
    monkeypatch.setattr(mp.time, "sleep", _stop_watcher)
    monkeypatch.setattr(mp, "Observer", _FakeObserver)

    outputs = tmp_path / "outputs"
    final = outputs / "2026-08-11_QA_VANILLIN"
    pending = outputs / ".2026-08-11_QA_VANILLIN.pending-abc"
    pending.mkdir(parents=True)
    (pending / "run_manifest.json").write_text("{}", encoding="utf-8")
    (pending / "report.csv").write_text("only copy", encoding="utf-8")

    state_dir = tmp_path / "states" / "SN-TXN-01"
    state_dir.mkdir(parents=True)
    (state_dir / "qa_state.json").write_text(
        json.dumps({"standards": {"list": []},
                    "instrument_accuracy_spec": {"ranges": []},
                    "transaction": {"last_committed_id": "qa-R1"}}),
        encoding="utf-8")
    (state_dir / "qa_transaction.json").write_text(
        json.dumps({
            "transaction_id": "qa-R1",
            "pending_report_dir": str(pending.resolve()),
            "final_report_dir": str(final.resolve()),
            "audit_entry": {"transaction_id": "qa-R1", "event": "qa_run"},
        }), encoding="utf-8")

    assert mp.main() == 0

    assert final.is_dir(), "committed report was swept away instead of published"
    assert (final / "report.csv").read_text(encoding="utf-8") == "only copy"
    assert not pending.exists()
    assert not (state_dir / "qa_transaction.json").exists()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
