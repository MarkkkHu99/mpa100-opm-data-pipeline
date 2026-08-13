"""Run the data-dependent robustness evaluation for the MPA100 pipeline.

Usage from the repository root::

    python evaluation/run_robustness_suite.py data-private/example.opm

The suite creates malformed derivatives only in a temporary directory. The
known-valid OPM input is not included in the public repository because release
permission for the laboratory file has not been established.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import types
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opm_parser import MAGIC, FRAME_MARKER, OPMFormatError, parse_opm


def load_main_module(source: Path):
    # Allow the test suite to inspect parser/adaptor functions even if watchdog
    # is not installed in a minimal test environment.
    try:
        import watchdog  # noqa: F401
    except ImportError:
        watchdog = types.ModuleType("watchdog")
        observers = types.ModuleType("watchdog.observers")
        events = types.ModuleType("watchdog.events")
        observers.Observer = type("Observer", (), {})
        events.FileSystemEventHandler = type("FileSystemEventHandler", (), {})
        sys.modules.update({
            "watchdog": watchdog,
            "watchdog.observers": observers,
            "watchdog.events": events,
        })
    spec = importlib.util.spec_from_file_location("mpa_pipeline", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.plt.rcParams["font.family"] = "DejaVu Sans"
    return module


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("opm", type=Path, help="known-valid OPM version 4 fixture")
    ap.add_argument("--main", type=Path, default=REPO_ROOT / "mpa_pipeline.py",
                    help="integrated main program")
    ap.add_argument("--json", type=Path,
                    default=REPO_ROOT / "evaluation/results/robustness_results.json")
    ap.add_argument("--csv", type=Path,
                    default=REPO_ROOT / "evaluation/results/robustness_results.csv")
    ap.add_argument("--markdown", type=Path,
                    default=REPO_ROOT / "evaluation/results/robustness_results.md")
    ap.add_argument("--txt", type=Path, help="TXT export from the same run as the OPM")
    ap.add_argument("--crossvalidator", type=Path,
                    default=Path(__file__).resolve().with_name("cross_validate_opm_txt.py"))
    args = ap.parse_args()
    source_bytes = args.opm.read_bytes()
    pipeline = load_main_module(args.main.resolve())
    results = []
    crossvalidation_evidence = {}

    def case(name, expected, fn):
        started = time.perf_counter()
        try:
            detail = fn()
            observed = "success"
        except Exception as exc:
            observed = "rejected"
            detail = f"{type(exc).__name__}: {exc}"
        passed = observed == expected
        results.append({
            "test": name,
            "expected": expected,
            "observed": observed,
            "passed": passed,
            "detail": str(detail),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        })

    with tempfile.TemporaryDirectory(prefix="mpa100_robustness_") as td:
        tmp = Path(td)

        case("valid_opm", "success", lambda: f'{len(parse_opm(args.opm)["frames"])} frames')

        bad = bytearray(source_bytes); bad[0:3] = b"BAD"
        case("opm_bad_magic", "rejected", lambda: parse_opm(write(tmp / "bad_magic.opm", bad)))

        bad = bytearray(source_bytes)
        version_offset = len(MAGIC) + 17
        struct.pack_into("<I", bad, version_offset, 99)
        case("opm_unsupported_version", "rejected", lambda: parse_opm(write(tmp / "v99.opm", bad)))

        case("opm_truncated_half", "rejected", lambda: parse_opm(write(tmp / "half.opm", source_bytes[:len(source_bytes)//2])))
        case("opm_truncated_tail", "rejected", lambda: parse_opm(write(tmp / "tail.opm", source_bytes[:-100])))

        bad = bytearray(source_bytes)
        marker = bad.find(FRAME_MARKER)
        struct.pack_into("<I", bad, marker + 4 + 20, len(bad) * 2)
        case("opm_invalid_image_size", "rejected", lambda: parse_opm(write(tmp / "image_size.opm", bad)))

        bad = bytearray(source_bytes)
        frame_count_offset = len(MAGIC) + 17 + 12
        original_count = struct.unpack_from("<I", bad, frame_count_offset)[0]
        struct.pack_into("<I", bad, frame_count_offset, original_count + 10)
        case("opm_frame_count_mismatch", "rejected", lambda: parse_opm(write(tmp / "frame_count.opm", bad)))

        empty_txt = tmp / "empty.txt"; empty_txt.write_text("", encoding="utf-8")
        case("txt_empty", "rejected", lambda: pipeline.load_txt_input(empty_txt))

        bad_num = tmp / "bad_numeric.txt"
        bad_num.write_text("Time(s)\tTemp(C)\tLeft\tCenter\tRight\n0\t36\tBAD\t0.2\t0.3\n", encoding="utf-8")
        case("txt_non_numeric", "rejected", lambda: pipeline.load_txt_input(bad_num))

        bad_cols = tmp / "bad_columns.txt"
        bad_cols.write_text("Time(s)\tTemp(C)\tLeft\tCenter\tRight\n0\t36\t0.1\t0.2\n", encoding="utf-8")
        case("txt_wrong_column_count", "rejected", lambda: pipeline.load_txt_input(bad_cols))

        valid_txt = tmp / "valid.txt"
        valid_txt.write_text(
            "Chemical name: TEST\nHeating rate: 15 C/min\n"
            "Time(s)\tTemp(C)\tLeft\tCenter\tRight\n"
            "0\t36\t0.1\t0.2\t0.3\n[End of document]\n",
            encoding="utf-8",
        )
        case("txt_minimal_valid", "success", lambda: f'{len(pipeline.load_txt_input(valid_txt)[1])} row')

        stable = tmp / "stable.opm"; stable.write_bytes(b"12345")
        case("file_ready_stable", "success", lambda: pipeline.wait_for_file_ready(stable, timeout=3, interval=0.05, stable_checks=3) or (_ for _ in ()).throw(RuntimeError("not ready")))

        def growing_file():
            target = tmp / "growing.opm"
            target.write_bytes(b"x")
            stop = threading.Event()
            def writer():
                while not stop.is_set():
                    with target.open("ab") as f:
                        f.write(b"x")
                    time.sleep(0.025)
            thread = threading.Thread(target=writer, daemon=True); thread.start()
            try:
                ready = pipeline.wait_for_file_ready(target, timeout=0.35,
                                                      interval=0.04, stable_checks=3)
            finally:
                stop.set(); thread.join(timeout=1)
            if ready:
                raise RuntimeError("growing file was incorrectly declared ready")
            return "continued waiting until timeout"
        case("file_ready_growing", "success", growing_file)

        zero = tmp / "never_ready.opm"; zero.write_bytes(b"")
        case("file_ready_timeout", "success",
             lambda: ("timeout returned False" if not pipeline.wait_for_file_ready(
                 zero, timeout=0.25, interval=0.05, stable_checks=2)
                 else (_ for _ in ()).throw(RuntimeError("timeout was not enforced"))))

        def naming_policy():
            source = args.main.read_text(encoding="utf-8")
            forbidden = ("InstrumentUnc_U_k2(C)", "onset_corr", "single_corr", "clear_corr")
            found = [term for term in forbidden if term in source]
            if found:
                raise RuntimeError(f"forbidden legacy labels remain: {found}")
            return "no k=2 CSV label or *_corr keys"
        case("student_output_naming_policy", "success", naming_policy)

        def alias_policy():
            calib = pipeline.MeltingPointCalibrator.__new__(pipeline.MeltingPointCalibrator)
            calib.standards = [{"name": "phenacetin", "aliases": ["acetophenetidin"]}]
            if calib.match_standard("Acetophenetidin") is not calib.standards[0]:
                raise RuntimeError("explicit alias was not matched")
            if calib.match_standard("unrelated sample") is not None:
                raise RuntimeError("unrelated sample was falsely matched")
            return "explicit alias accepted; unrelated name rejected"
        case("standard_alias_and_false_match", "success", alias_policy)

        def drift_freeze_policy():
            calib = pipeline.MeltingPointCalibrator.__new__(pipeline.MeltingPointCalibrator)
            calib.standards = [{"name": "standard-a", "certified_clear_point_C": 100.0}]
            calib.accuracy_ranges = [{"min_C": 0, "max_C": 200, "accuracy_C": 0.5}]
            calib.curve = {"slope": 1.0, "intercept": 0.0, "r_squared": None, "rmse": None}
            calib.curve_status = "configured"
            calib.config = {"calibration_data": {"date_expires": "2030-01-01",
                                                  "recommended_recalibration_months": 6}}
            calib.calib_file = str(tmp / "drift_config.json")
            calib.log_file = str(tmp / "drift_log.jsonl")
            calib.drift_alert = None
            calib.qa_attention = None
            calib.calib_valid = True
            calib._pending_state = None
            calib._pending_snapshot = None
            calib._try_fit_from_standards = lambda: (_ for _ in ()).throw(
                RuntimeError("out-of-tolerance run attempted to refit"))
            result = calib.record_standard_run("standard-a", 103.0)
            if result.get("accepted_for_calibration") is not False:
                raise RuntimeError("out-of-tolerance run was accepted")
            if calib.config["calibration_data"]["date_expires"] != "2030-01-01":
                raise RuntimeError("expiry date changed")
            if calib.standards[0].get("measured_clear_points_C"):
                raise RuntimeError("failed run entered calibration fit data")
            return "event logged; fit data and expiry unchanged"
        case("drift_failure_freezes_calibration", "success", drift_freeze_policy)

        def missing_certified_value_policy():
            calib = pipeline.MeltingPointCalibrator.__new__(pipeline.MeltingPointCalibrator)
            calib.standards = [{"name": "standard-b"}]
            calib.accuracy_ranges = []
            calib.curve = {"slope": 1.0, "intercept": 0.0,
                           "r_squared": None, "rmse": None}
            calib.curve_status = "configured"
            calib.config = {
                "standards": {"list": calib.standards},
                "calibration_data": {"date_expires": "2030-01-01"},
            }
            calib.calib_file = str(tmp / "missing_cert_config.json")
            calib.log_file = str(tmp / "missing_cert_log.jsonl")
            calib.drift_alert = None
            calib.qa_attention = None
            calib.calib_valid = True
            calib._pending_state = None
            calib._pending_snapshot = None
            try:
                calib.record_standard_run("standard-b", 100.0)
            except ValueError:
                pass
            else:
                raise RuntimeError("standard without certified value was accepted")
            if calib.standards[0].get("measured_clear_points_C"):
                raise RuntimeError("invalid standard entered calibration state")
            return "configuration error rejected fail-closed; state unchanged"
        case("missing_certified_value_fail_closed", "success", missing_certified_value_policy)

        def explicit_routing_policy():
            if pipeline.FileHandler._is_qa_run("student/caffeine.opm", {"Chemical name": "caffeine"}):
                raise RuntimeError("chemical name alone triggered QA routing")
            if not pipeline.FileHandler._is_qa_run("any.opm", {"Chemical name": "QA_VANILLIN"}):
                raise RuntimeError("reserved QA label was ignored")
            if pipeline.FileHandler._is_qa_run("any.opm", {"Chemical name": "QA_UNKNOWN"}):
                raise RuntimeError("unknown reserved QA label was accepted")
            return "plain caffeine remained student; reserved QA label accepted"
        case("explicit_qa_routing_policy", "success", explicit_routing_policy)

        def single_folder_monitor_registration():
            class FakeObserver:
                def __init__(self): self.paths = []
                def schedule(self, handler, path, recursive=False):
                    self.paths.append((path, recursive))
            pipeline.MONITOR_FOLDER = str(tmp / "student_inputs")
            observer = FakeObserver()
            pipeline.configure_observer(observer, object())
            if observer.paths != [(pipeline.MONITOR_FOLDER, False)]:
                raise RuntimeError(f"unexpected watcher registration: {observer.paths}")
            return "one MeltView automatic-save folder registered"
        case("single_folder_monitor_registration", "success",
             single_folder_monitor_registration)

        def three_standard_completion_policy():
            calib = pipeline.MeltingPointCalibrator.__new__(pipeline.MeltingPointCalibrator)
            calib.standards = [
                {"name": "vanillin", "certified_clear_point_C": 82.0,
                 "measured_mean_C": 82.1},
                {"name": "phenacetin", "certified_clear_point_C": 134.0,
                 "measured_mean_C": 134.1},
                {"name": "caffeine", "certified_clear_point_C": 236.0},
            ]
            calib.curve = {"slope": 1.0, "intercept": 0.0,
                           "r_squared": None, "rmse": None,
                           "u_slope": None, "u_intercept": None}
            if calib._try_fit_from_standards():
                raise RuntimeError("partial two-standard set was accepted")
            calib.standards[2]["measured_mean_C"] = 236.1
            if not calib._try_fit_from_standards():
                raise RuntimeError("complete three-standard set was rejected")
            return "two standards rejected; complete three-standard set fitted"
        case("manufacturer_three_standard_completion", "success",
             three_standard_completion_policy)

        def manufacturer_run_checks():
            def meta(onsets, clears):
                out = {"Start temperature": "78.0 C",
                       "Stop temperature": "88.0 C"}
                for ch, onset, clear in zip(("left", "center", "right"), onsets, clears):
                    out[f"Onset point ({ch})"] = f"{onset} C"
                    out[f"Clear point ({ch})"] = f"{clear} C"
                return out
            standard = {"expected_start_C": 78.0, "expected_stop_C": 88.0,
                        "ramp_rate_C_min": 1.0,
                        "three_capillary_max_spread_C": 0.3,
                        "max_melting_range_C": 2.0}
            valid = meta([81.0, 81.1, 81.0], [82.0, 82.2, 82.1])
            result = pipeline.FileHandler._validate_standard_run(
                valid, 1.0, "vanillin", standard)
            if result["clear_spread_C"] > 0.3:
                raise RuntimeError("valid three-capillary spread rejected")
            for label, bad_meta, bad_rate in [
                ("wrong ramp", valid, 2.0),
                ("wide clear spread", meta([81,81,81], [82.0,82.4,82.1]), 1.0),
                ("wide melting range", meta([79.5,81,81], [82.0,82.1,82.0]), 1.0),
            ]:
                try:
                    pipeline.FileHandler._validate_standard_run(
                        bad_meta, bad_rate, "vanillin", standard)
                except ValueError:
                    continue
                raise RuntimeError(f"{label} was accepted")
            return "valid run accepted; ramp, spread and melting-range failures rejected"
        case("manufacturer_per_crs_run_checks", "success", manufacturer_run_checks)

        def per_instrument_qa_state_isolation():
            template = tmp / "qa_template.json"
            template.write_text(json.dumps({
                "standards": {"list": []},
                "instrument_accuracy_spec": {"ranges": []},
                "calibration_curve": {"status": "NEEDS_MEASUREMENT"},
            }), encoding="utf-8")
            pipeline.CALIBRATION_FILE = str(template)
            pipeline.QA_STATE_ROOT = str(tmp / "qa_by_instrument")
            handler = pipeline.FileHandler()
            a = handler._get_calibrator({"Instrument serial number": "SN-A"})
            b = handler._get_calibrator({"Instrument serial number": "SN-B"})
            if a is b or a.calib_file == b.calib_file or a.log_file == b.log_file:
                raise RuntimeError("two instruments shared QA state or history")
            if set(handler.calibrators) != {"SN-A", "SN-B"}:
                raise RuntimeError(f"unexpected instrument cache: {handler.calibrators.keys()}")
            return "SN-A and SN-B received separate state and history paths"
        case("per_instrument_qa_state_isolation", "success",
             per_instrument_qa_state_isolation)

        def pipeline_rejects_malformed():
            out = tmp / "reject_outputs"; out.mkdir()
            pipeline.OUTPUT_FOLDER = str(out)
            pipeline.CALIBRATION_FILE = str(tmp / "missing_reject_calibration.json")
            pipeline.CALIBRATION_LOG = str(tmp / "reject_history.json")
            handler = pipeline.FileHandler()
            accepted = handler.process_file(str(tmp / "half.opm"))
            files = [p for p in out.rglob("*") if p.is_file()]
            if accepted or files:
                raise RuntimeError(f"malformed input left normal outputs: {files}")
            return "pipeline returned failure and left zero output files"
        case("pipeline_rejects_malformed_without_outputs", "success", pipeline_rejects_malformed)

        def missing_heating_rate():
            source = tmp / "missing_rate.txt"
            source.write_text(
                "Acquired on: 2026-08-11 13:00:00\n"
                "Chemical name: TEST\nInstrument serial number: SN-MISSING-RATE\n"
                "Time(s)\tTemp(C)\tLeft\tCenter\tRight\n"
                "0\t36\t0.1\t0.2\t0.3\n[End of document]\n", encoding="utf-8")
            out = tmp / "missing_rate_outputs"; out.mkdir()
            pipeline.OUTPUT_FOLDER = str(out)
            pipeline.CALIBRATION_FILE = str(tmp / "missing_rate_calibration.json")
            pipeline.CALIBRATION_LOG = str(tmp / "missing_rate_history.json")
            if pipeline.FileHandler().process_file(str(source)):
                raise RuntimeError("missing required heating rate was accepted")
            if any(out.rglob("*")):
                raise RuntimeError("rejected run left normal outputs")
            return "missing required heating rate rejected without outputs"
        case("missing_heating_rate_fail_closed", "success", missing_heating_rate)

        def atomic_output_failure():
            out = tmp / "write_failure_outputs"; out.mkdir()
            pipeline.OUTPUT_FOLDER = str(out)
            pipeline.CALIBRATION_FILE = str(tmp / "missing_write_calibration.json")
            pipeline.CALIBRATION_LOG = str(tmp / "write_history.json")
            original = pipeline.generate_channel_pdf
            calls = {"n": 0}
            def fail_second(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated PDF write failure")
                return original(*args, **kwargs)
            pipeline.generate_channel_pdf = fail_second
            try:
                accepted = pipeline.FileHandler().process_file(str(args.opm))
            finally:
                pipeline.generate_channel_pdf = original
            files = [p for p in out.rglob("*") if p.is_file()]
            if accepted or files:
                raise RuntimeError(f"partial output escaped staging directory: {files}")
            return "simulated write failure left zero committed files"
        case("atomic_output_on_write_failure", "success", atomic_output_failure)

        if args.txt:
            def crossvalidation():
                nonlocal crossvalidation_evidence
                json_out = tmp / "crossvalidation.json"
                command = [sys.executable, str(args.crossvalidator.resolve()), str(args.opm),
                           str(args.txt), "--main", str(args.main.resolve()),
                           "--json", str(json_out), "--csv", str(tmp / "cross.csv"),
                           "--markdown", str(tmp / "cross.md")]
                completed = subprocess.run(command, capture_output=True, text=True,
                                           env={**os.environ, "MPLCONFIGDIR": "/tmp/mpl"})
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr or completed.stdout)
                summary = json.loads(json_out.read_text(encoding="utf-8"))
                if not summary["passed"]:
                    raise RuntimeError("OPM–TXT values did not match")
                crossvalidation_evidence = summary
                return (f"rows={summary['opm_rows']}; metadata mismatches="
                        f"{summary['metadata_mismatches']}; point mismatches="
                        f"{summary['detection_point_mismatches']}; series mismatches="
                        f"{summary['time_series_mismatched_cells']}")
            case("opm_txt_crossvalidation", "success", crossvalidation)

        # Full pipeline smoke test: direct OPM -> CSV, plots and three PDFs.
        def integration():
            out = tmp / "outputs"; out.mkdir()
            pipeline.OUTPUT_FOLDER = str(out)
            pipeline.CALIBRATION_FILE = str(tmp / "missing_calibration.json")
            pipeline.CALIBRATION_LOG = str(tmp / "calibration_history.json")
            handler = pipeline.FileHandler()
            handler.process_file(str(args.opm))
            files = [p for p in out.rglob("*") if p.is_file()]
            pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
            csvs = [p for p in files if p.suffix.lower() == ".csv"]
            pngs = [p for p in files if p.suffix.lower() == ".png"]
            if len(pdfs) != 3 or len(csvs) != 2 or len(pngs) != 3:
                raise RuntimeError(f"unexpected outputs: PDF={len(pdfs)}, CSV={len(csvs)}, PNG={len(pngs)}")
            results_csv = next(p for p in csvs if p.name.endswith("_Results.csv"))
            columns = list(pipeline.pd.read_csv(results_csv).columns)
            if "InstrumentAccuracy(C)" not in columns:
                raise RuntimeError(f"new accuracy column missing: {columns}")
            if any("k2" in c.lower() or "unc" in c.lower() for c in columns):
                raise RuntimeError(f"legacy uncertainty label remains: {columns}")
            return f"PDF={len(pdfs)}, CSV={len(csvs)}, PNG={len(pngs)}"
        case("opm_end_to_end_outputs", "success", integration)

    total = len(results)
    passed = sum(r["passed"] for r in results)
    invalid = [r for r in results if r["expected"] == "rejected"]
    valid = [r for r in results if r["expected"] == "success"]
    correct_rejections = sum(r["passed"] for r in invalid)
    silent_status_errors = sum(1 for r in results
                               if not r["passed"] and r["observed"] == "success")
    silent_numerical_errors = crossvalidation_evidence.get("silent_numerical_errors", 0)
    silent_errors = silent_status_errors + silent_numerical_errors
    summary = {
        "total_tests": total,
        "passed_tests": passed,
        "test_pass_rate_percent": round(100 * passed / total, 2),
        "valid_cases": len(valid),
        "successful_valid_cases": sum(r["passed"] for r in valid),
        "valid_case_success_rate_percent": round(
            100 * sum(r["passed"] for r in valid) / len(valid), 2),
        "invalid_cases": len(invalid),
        "correct_rejections": correct_rejections,
        "correct_rejection_rate_percent": round(100 * correct_rejections / len(invalid), 2),
        "silent_status_errors": silent_status_errors,
        "silent_numerical_errors": silent_numerical_errors,
        "silent_errors": silent_errors,
        "results": results,
    }
    args.json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with args.csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["test", "expected", "observed", "passed",
                                                   "detail", "elapsed_ms"])
        writer.writeheader()
        writer.writerows(results)
    md = ["# Robustness test results", "",
          f"- Tests passed: {passed}/{total}",
          f"- Correct rejection rate: {correct_rejections}/{len(invalid)} "
          f"({summary['correct_rejection_rate_percent']}%)",
          f"- Silent errors: {silent_errors}", "",
          "| Test | Expected | Observed | Pass | Detail |",
          "|---|---|---|---:|---|"]
    for row in results:
        detail = str(row["detail"]).replace("|", "\\|").replace("\n", " ")
        md.append(f"| {row['test']} | {row['expected']} | {row['observed']} | "
                  f"{'Yes' if row['passed'] else 'No'} | {detail} |")
    args.markdown.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
