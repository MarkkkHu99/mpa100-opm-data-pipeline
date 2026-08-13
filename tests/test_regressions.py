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
import tempfile
import pytest

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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
