# -*- coding: utf-8 -*-
"""MPA100 OPM data-processing pipeline for a teaching laboratory.

Evidence and reporting policy
-----------------------------
For unidentified teaching channels, the software does not apply a relationship
derived from reference-standard runs. It reports the instrument-indicated
melting point, the recorded heating rate and the manufacturer's accuracy
specification while preserving run- and instrument-level provenance.

The separate reference-standard path supports instrument-condition QA only:

* vanillin, phenacetin and caffeine runs under configured conditions;
* comparison of measured and assigned clear points;
* instrument-scoped QA-state and validity tracking; and
* append-only, integrity-sensitive QA history.

The software does not identify a student or material at channel level, control
instrument heating, write temperature offsets back to the instrument, or apply
standard-derived corrections to teaching-channel results.
"""
import re
from difflib import SequenceMatcher
import os
import shutil
import tempfile
import time
import json
import uuid
import copy
import io
import hashlib
import calendar
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import font_manager
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

__version__ = "0.9.0"

# Direct OPM parser. Keep opm_parser.py importable alongside this module.
try:
    from opm_parser import parse_opm, OPMFormatError
    OPM_SUPPORT = True
except ImportError:
    parse_opm = None
    OPMFormatError = ValueError
    OPM_SUPPORT = False

# ====================== Core configuration ======================
BASE_DIR = Path(__file__).resolve().parent
MONITOR_FOLDER = os.environ.get("MPA100_MONITOR_FOLDER", r"E:\MPA100txt")
OUTPUT_FOLDER = os.environ.get(
    "MPA100_OUTPUT_FOLDER", r"E:\MPA100txt\Auto_Converted_Results")
# This is a definitions-only template. Instrument measurements and trusted-state
# fields are deliberately stripped when a new serial number is initialised.
CALIBRATION_FILE = os.environ.get(
    "MPA100_QA_TEMPLATE", str(BASE_DIR / "config" / "qa_template.json"))
CALIBRATION_LOG = os.environ.get(
    "MPA100_LEGACY_CALIBRATION_LOG", str(BASE_DIR / "runtime" / "legacy_history.jsonl"))
QA_STATE_ROOT = os.environ.get(
    "MPA100_QA_STATE_ROOT", str(BASE_DIR / "runtime" / "qa_by_instrument"))
PIPELINE_LOG_ROOT = os.environ.get(
    "MPA100_PIPELINE_LOG_ROOT", str(BASE_DIR / "runtime" / "pipeline_logs"))
# All OPM files remain in MeltView's one automatic-save folder. A QA run is
# declared at source by a reserved chemical-name label entered before the run.
QA_NAME_PREFIX = "QA_"
QA_RESERVED_NAMES = {"vanillin", "phenacetin", "caffeine"}
# A run whose chemical-name label begins with the QA_ prefix declares QA intent.
# Anything after the prefix that is NOT one of the reserved standards above is
# rejected fail-closed at routing (see FileHandler.process_file), so an operator
# typo such as "QA_VANILIN" can never silently fall through to the student path.
_QA_INTENT_RE = re.compile(r"\s*QA[_\-\s]", flags=re.IGNORECASE)


class QANameError(ValueError):
    """A run declared QA intent (QA_ prefix) but the name is not a configured
    reserved standard. Fail-closed: no report is produced and the attempt is
    logged as a QA rejection."""


class QAStatePersistenceError(RuntimeError):
    """An instrument QA-state transaction could not be durably committed."""


def _atomic_write_json(path, payload):
    """Atomically replace one JSON document and fsync its containing directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (AttributeError, OSError):
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _blank_instrument_config(template_path, instrument_id):
    """Create definitions-only state for a previously unseen instrument.

    No measured points, curve parameters, validity dates, attention flags or
    trusted status are inherited from the template or another instrument.
    """
    template = {}
    if template_path and os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as handle:
            template = json.load(handle)
    allowed_standard_fields = {
        "name", "aliases", "certified_clear_point_C", "expected_start_C",
        "expected_stop_C", "ramp_rate_C_min", "temperature_tolerance_C",
        "three_capillary_max_spread_C", "max_melting_range_C",
    }
    standards = []
    for source in template.get("standards", {}).get("list", []):
        standards.append({key: copy.deepcopy(value) for key, value in source.items()
                          if key in allowed_standard_fields})
    calibration_policy = template.get("calibration_data", {})
    config = {
        "schema_version": 2,
        "instrument": {"serial_number": instrument_id},
        "instrument_accuracy_spec": copy.deepcopy(
            template.get("instrument_accuracy_spec", {})),
        "standards": {"list": standards},
        "calibration_curve": {
            "status": "NEEDS_MEASUREMENT",
            "parameters": {"slope": None, "slope_uncertainty": None,
                           "intercept": None, "intercept_uncertainty": None},
            "goodness_of_fit": {"r_squared": None, "rmse": None},
        },
        "calibration_data": {
            "recommended_recalibration_months": calibration_policy.get(
                "recommended_recalibration_months", 6),
        },
        "transaction": {"last_committed_id": None},
    }
    return config
try:
    font_manager.findfont("Arial", fallback_to_default=False)
    plt.rcParams['font.family'] = 'Arial'
except ValueError:
    plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['axes.unicode_minus'] = False
# =========================================================


# ====================== Instrument QA and drift monitoring ======================
# Design policy:
#   - a relationship is fitted only from the configured reference standards;
#   - that relationship is restricted to instrument QA and drift monitoring;
#   - teaching-channel reports show the manufacturer's accuracy specification,
#     not an expanded measurement uncertainty or a corrected result.
#
# Each reference-standard run compares its measured and assigned clear points.
# An out-of-tolerance result raises a persistent attention-required state.
class MeltingPointCalibrator:
    def __init__(self, calib_file, log_file=None):
        self.calib_file = calib_file
        self.log_file = log_file
        self.config = {}
        self.loaded = False
        self.curve_status = "unavailable"
        self.calib_valid = False
        self.curve = {"slope": None, "u_slope": None,
                      "intercept": None, "u_intercept": None,
                      "r_squared": None, "rmse": None}
        self.accuracy_ranges = []
        self.valid_range = None
        self.standards = []
        self.drift_alert = None  # Result of the most recent drift check, if any.
        self.qa_attention = None  # Persistent flag raised by an out-of-tolerance run.
        self._pending_state = None
        self._pending_snapshot = None
        self.load_calibration()
        self.check_calibration_validity()

    # ---------- Configuration loading ----------
    def load_calibration(self):
        try:
            with open(self.calib_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            self.loaded = True
        except Exception as e:
            print(f"ERROR: Calibration file error: {str(e)}; proceeding without QA curve")
            self.loaded = False
            return

        self.accuracy_ranges = self.config.get("instrument_accuracy_spec", {}).get("ranges", [])

        self.standards = self.config.get("standards", {}).get("list", [])
        certs = [s.get("certified_clear_point_C") for s in self.standards
                 if s.get("certified_clear_point_C") is not None]
        if certs:
            self.valid_range = [min(certs), max(certs)]

        cc = self.config.get("calibration_curve", {})
        self.qa_attention = cc.get("qa_attention")
        p = cc.get("parameters", {})
        if p.get("slope") is not None and p.get("intercept") is not None:
            self.curve.update({
                "slope": p["slope"], "u_slope": p.get("slope_uncertainty"),
                "intercept": p["intercept"], "u_intercept": p.get("intercept_uncertainty"),
                "r_squared": cc.get("goodness_of_fit", {}).get("r_squared"),
                "rmse": cc.get("goodness_of_fit", {}).get("rmse"),
            })
            self.curve_status = "measured"
        elif self._try_fit_from_standards():
            self.curve_status = "measured"
        else:
            self.curve.update({"slope": 1.0, "intercept": 0.0})
            self.curve_status = cc.get("status", "NEEDS_MEASUREMENT")
            print("WARNING: QA calibration curve has not been measured.")

        if self.curve_status == "measured":
            print(f"QA calibration curve loaded | valid range = {self.valid_range} °C")

    def _try_fit_from_standards(self):
        xs, ys = [], []
        for s in self.standards:
            cert = s.get("certified_clear_point_C")
            meas = s.get("measured_clear_points_C") or []
            mean = s.get("measured_mean_C")
            if mean is None and meas:
                mean = sum(meas) / len(meas)
            if cert is not None and mean is not None:
                xs.append(mean); ys.append(cert)
        # The OptiMelt manufacturer procedure uses three CRSs. Do not mark the
        # QA relationship as measured from a partial two-standard subset.
        configured = [s for s in self.standards
                      if s.get("certified_clear_point_C") is not None]
        if len(configured) < 3 or len(xs) != len(configured):
            return False
        x = np.array(xs); y = np.array(ys); n = len(x)
        slope, intercept = np.polyfit(x, y, 1)
        yhat = slope * x + intercept
        resid = y - yhat
        ss_res = float((resid ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
        rmse = (ss_res / n) ** 0.5
        if n > 2:
            sxx = float(((x - x.mean()) ** 2).sum())
            s_err = (ss_res / (n - 2)) ** 0.5
            u_slope = float(s_err / (sxx ** 0.5)) if sxx > 0 else None
            u_intercept = float(s_err * ((1.0 / n + x.mean() ** 2 / sxx) ** 0.5)) if sxx > 0 else None
        else:
            u_slope = u_intercept = None
        self.curve.update({"slope": float(slope), "u_slope": u_slope,
                           "intercept": float(intercept), "u_intercept": u_intercept,
                           "r_squared": round(r2, 4), "rmse": round(rmse, 3)})
        print(f"Fitted QA curve from {n} standards: "
              f"slope={slope:.4f}, intercept={intercept:.3f}, R²={r2:.4f}")
        return True

    def check_calibration_validity(self):
        try:
            exp = self.config["calibration_data"]["date_expires"]
            self.calib_valid = datetime.now() <= datetime.strptime(exp, "%Y-%m-%d")
        except Exception:
            self.calib_valid = False

    # ---------- Reference-standard matching ----------
    @staticmethod
    def _norm_name(s):
        return re.sub(r'[^a-z0-9]', '', str(s).lower())

    def match_standard_with_info(self, chemical):
        if not chemical:
            return None, {"method": "none", "score": None}
        key = self._norm_name(chemical)
        for s in self.standards:
            accepted_names = [s.get("name", "")] + list(s.get("aliases", []) or [])
            normalized = {self._norm_name(name): name for name in accepted_names}
            if key in normalized:
                method = "exact" if key == self._norm_name(s.get("name", "")) else "alias"
                return s, {"method": method, "score": 1.0,
                           "original_name": chemical, "matched_name": s.get("name")}
        # Conservative fallback for minor spelling differences.  A match is
        # accepted only when it is both strong and unambiguous.
        if len(key) >= 5:
            scored = []
            for s in self.standards:
                accepted_names = [s.get("name", "")] + list(s.get("aliases", []) or [])
                score = max(SequenceMatcher(None, key, self._norm_name(name)).ratio()
                            for name in accepted_names if self._norm_name(name))
                scored.append((score, s))
            scored.sort(key=lambda item: item[0], reverse=True)
            if scored and scored[0][0] >= 0.92:
                if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05:
                    return scored[0][1], {"method": "fuzzy", "score": scored[0][0],
                                          "original_name": chemical,
                                          "matched_name": scored[0][1].get("name")}
        return None, {"method": "none", "score": None, "original_name": chemical}

    def match_standard(self, chemical):
        return self.match_standard_with_info(chemical)[0]

    def is_standard_run(self, chemical):
        return self.match_standard(chemical) is not None

    # ---------- Drift check: measured versus assigned clear point ----------
    def check_drift(self, chemical, measured_clear):
        """Compare a reference run's clear point with its assigned value."""
        std = self.match_standard(chemical)
        if std is None or measured_clear is None:
            return None
        cert = std.get("certified_clear_point_C")
        if cert is None:
            return None
        # Manufacturer convention: TOC = certified (rated) - measured.
        diff = cert - measured_clear
        tol = self.instrument_accuracy(cert)  # Manufacturer specification for this zone.
        within = abs(diff) <= tol
        alert = {
            "standard": std.get("name"),
            "certified_C": cert,
            "measured_C": measured_clear,
            "temperature_offset_correction_C": round(diff, 2),
            "tolerance_C": tol,
            "within_tolerance": within,
            "recommendation": ("ok" if within else
                               "Drift detected — instrument may need recalibration."),
        }
        if not within:
            print(f"WARNING: DRIFT: {std.get('name')} measured {measured_clear:.2f}°C "
                  f"vs certified {cert:.1f}°C (TOC={diff:+.2f}°C, tol ±{tol:.1f}°C). "
                  "Instrument may need recalibration.")
        return alert

    # ---------- Reference-run recording and QA history ----------
    def record_standard_run(self, chemical, clear_point, ramp_rate=None,
                            operator=None, persist=True):
        """Record one reference run and return its drift-check result.

        Accepted values update the standard history and may refit the
        instrument QA relationship. Persistence can be deferred so the caller
        can coordinate state with report publication.
        """
        std = self.match_standard(chemical)
        if std is None or clear_point is None:
            return {"recorded": False}

        if self._pending_state is not None:
            raise QAStatePersistenceError(
                "A previous QA-state transaction is still pending")
        self._pending_snapshot = self._snapshot_runtime_state()

        if std.get("certified_clear_point_C") is None:
            print(f"WARNING: QA configuration error: '{std.get('name')}' has no certified value; "
                  "run rejected and state left unchanged.")
            self.abort_pending_state()
            raise ValueError(
                f"QA standard '{std.get('name')}' has no certified clear point")

        # Evaluate this observation before it can enter the fitted relationship.
        drift = self.check_drift(chemical, clear_point)
        self.drift_alert = drift

        accepted_for_calibration = bool(drift and drift.get("within_tolerance"))
        if accepted_for_calibration:
            arr = std.setdefault("measured_clear_points_C", [])
            arr.append(round(float(clear_point), 2))
            std["measured_mean_C"] = round(sum(arr) / len(arr), 3)
            if len(arr) > 1:
                m = std["measured_mean_C"]
                std["measured_std_C"] = round(
                    (sum((x - m) ** 2 for x in arr) / (len(arr) - 1)) ** 0.5, 3)

        # An out-of-tolerance run is logged, but it cannot alter the fitted
        # relationship or renew the calibration-validity period.
        refit = self._try_fit_from_standards() if accepted_for_calibration else False
        if refit:
            self.curve_status = "measured"
            cc = self.config.setdefault("calibration_curve", {})
            cc["status"] = "measured"
            cc.setdefault("parameters", {}).update({
                "slope": self.curve["slope"], "slope_uncertainty": self.curve["u_slope"],
                "intercept": self.curve["intercept"], "intercept_uncertainty": self.curve["u_intercept"],
            })
            cc.setdefault("goodness_of_fit", {}).update({
                "r_squared": self.curve["r_squared"], "rmse": self.curve["rmse"],
            })
            cd = self.config.setdefault("calibration_data", {})
            today = datetime.now()
            cd["date_performed"] = today.strftime("%Y-%m-%d")
            months = cd.get("recommended_recalibration_months", 6)
            exp = _add_months(today, months)
            cd["date_expires"] = exp.strftime("%Y-%m-%d")
            self.check_calibration_validity()

        # An out-of-tolerance reference run must not leave the instrument looking
        # acceptable on the strength of a previous pass. Preserve the prior fit
        # for reference, but raise a persistent "attention required" flag that
        # supersedes the displayed QA status until an administrator clears it.
        cc = self.config.setdefault("calibration_curve", {})
        if drift is not None and not drift.get("within_tolerance"):
            self.qa_attention = {
                "state": "attention_required",
                "raised_on": datetime.now().strftime("%Y-%m-%d"),
                "reason": "reference-standard run out of tolerance",
                "standard": std.get("name"),
                "measured_clear_C": round(float(clear_point), 2),
                "temperature_offset_correction_C": drift.get("temperature_offset_correction_C"),
                "tolerance_C": drift.get("tolerance_C"),
            }
            cc["qa_attention"] = self.qa_attention
        # A later passing run does not clear an earlier alert. Clearing requires
        # an explicit, attributable administrator action (clear_qa_attention).

        audit_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "standard_run",
            "standard": std.get("name"),
            "ramp_rate_C_min": ramp_rate,
            "measured_clear_C": round(float(clear_point), 2),
            "operator": operator or "unspecified",
            "drift_check": drift,
            "accepted_for_calibration": accepted_for_calibration,
            "qa_attention": self.qa_attention,
            "curve_status_after": self.curve_status,
            "curve_params_after": {k: self.curve.get(k) for k in
                                   ("slope", "intercept", "r_squared", "rmse")},
        }
        result = {"recorded": True, "drift": drift, "refit": refit,
                  "accepted_for_calibration": accepted_for_calibration,
                  "qa_attention": self.qa_attention}
        if persist:
            self._pending_state = audit_entry
            try:
                self.commit_pending_state()
                self.finalize_pending_state()
            except Exception:
                self.abort_pending_state()
                raise
        else:
            # Defer the write so FileHandler can prepare the supporting report
            # and a recovery journal before committing either side.
            self._pending_state = audit_entry
            result["deferred"] = True
        return result

    def _snapshot_runtime_state(self):
        return {
            "config": copy.deepcopy(self.config),
            "curve": copy.deepcopy(self.curve),
            "curve_status": self.curve_status,
            "calib_valid": self.calib_valid,
            "drift_alert": copy.deepcopy(self.drift_alert),
            "qa_attention": copy.deepcopy(self.qa_attention),
        }

    def _restore_runtime_state(self, snapshot):
        if snapshot is None:
            return
        self.config = copy.deepcopy(snapshot["config"])
        self.curve = copy.deepcopy(snapshot["curve"])
        self.curve_status = snapshot["curve_status"]
        self.calib_valid = snapshot["calib_valid"]
        self.drift_alert = copy.deepcopy(snapshot["drift_alert"])
        self.qa_attention = copy.deepcopy(snapshot["qa_attention"])
        self.standards = self.config.get("standards", {}).get("list", [])

    def _persist_state(self, audit_entry=None):
        """Atomically replace state, then durably append its audit event.

        Errors are raised to the caller. The earlier version swallowed write
        failures and cleared pending state, which could publish a report while
        silently losing the instrument status update.
        """
        old_config = None
        existed = os.path.exists(self.calib_file)
        try:
            if existed:
                with open(self.calib_file, "r", encoding="utf-8") as handle:
                    old_config = json.load(handle)
            _atomic_write_json(self.calib_file, self.config)
            if audit_entry is not None:
                self._append_audit_log(audit_entry)
        except Exception as e:
            try:
                if old_config is not None:
                    _atomic_write_json(self.calib_file, old_config)
                elif not existed and os.path.exists(self.calib_file):
                    os.unlink(self.calib_file)
            except Exception as rollback_error:
                raise QAStatePersistenceError(
                    f"QA-state commit failed ({e}); disk rollback also failed "
                    f"({rollback_error})") from e
            raise QAStatePersistenceError(
                f"QA-state commit failed and was rolled back: {e}") from e

    def commit_pending_state(self, transaction_id=None):
        """Durably write a deferred status update; keep rollback snapshot alive."""
        if self._pending_state is not None:
            if transaction_id:
                self._pending_state["transaction_id"] = transaction_id
                self.config.setdefault("transaction", {})[
                    "last_committed_id"] = transaction_id
            self._persist_state(self._pending_state)
            return True
        return False

    def finalize_pending_state(self):
        """Forget rollback material only after the supporting report is published."""
        self._pending_state = None
        self._pending_snapshot = None

    def abort_pending_state(self, restore_disk=False):
        """Restore the pre-run in-memory state and optionally its on-disk state."""
        snapshot = self._pending_snapshot
        pending = copy.deepcopy(self._pending_state)
        self._restore_runtime_state(snapshot)
        if restore_disk and snapshot is not None:
            _atomic_write_json(self.calib_file, snapshot["config"])
            self._append_audit_log({
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": "qa_transaction_rolled_back",
                "transaction_id": (pending or {}).get("transaction_id"),
                "reason": "supporting report could not be published",
            })
        self._pending_state = None
        self._pending_snapshot = None

    def clear_qa_attention(self, actor, reason):
        """Explicitly clear a persistent QA alert with an attributable audit entry."""
        if not str(actor).strip() or not str(reason).strip():
            raise ValueError("actor and reason are required to clear QA attention")
        if not self.qa_attention:
            return False
        self._pending_snapshot = self._snapshot_runtime_state()
        cleared = copy.deepcopy(self.qa_attention)
        self.qa_attention = None
        self.config.setdefault("calibration_curve", {}).pop("qa_attention", None)
        self._pending_state = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "qa_attention_cleared",
            "actor": str(actor).strip(),
            "reason": str(reason).strip(),
            "cleared_attention": cleared,
        }
        try:
            self.commit_pending_state()
            self.finalize_pending_state()
        except Exception:
            self.abort_pending_state()
            raise
        return True

    def _append_audit_log(self, entry):
        """Append one fsynced, hash-chained JSONL audit event.

        Existing malformed content is never treated as an empty history and
        overwritten. It blocks the commit for operator review.
        """
        if not self.log_file:
            return
        path = Path(self.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_hash = "GENESIS"
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise QAStatePersistenceError(
                            f"QA audit log is malformed at line {line_number}; "
                            "history was preserved and the commit was blocked") from exc
                    claimed = record.get("entry_hash")
                    base = {k: v for k, v in record.items() if k != "entry_hash"}
                    expected = hashlib.sha256(json.dumps(
                        base, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")).hexdigest()
                    if claimed != expected or base.get("previous_hash") != previous_hash:
                        raise QAStatePersistenceError(
                            f"QA audit hash-chain verification failed at line {line_number}")
                    previous_hash = claimed
        payload = copy.deepcopy(entry)
        payload["previous_hash"] = previous_hash
        payload["entry_hash"] = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---------- Manufacturer accuracy specification by temperature zone ----------
    def instrument_accuracy(self, temp):
        """Return the manufacturer's accuracy specification for a temperature."""
        if temp is None:
            return 0.5
        for r in self.accuracy_ranges:
            lo = r.get("min_C")
            hi = r.get("max_C")
            lower_ok = lo is None or (temp >= lo if r.get("min_inclusive", True)
                                      else temp > lo)
            upper_ok = hi is None or (temp <= hi if r.get("max_inclusive", False)
                                      else temp < hi)
            if lower_ok and upper_ok:
                return r["accuracy_C"]
        return 0.5

    # ---------- Teaching-channel reporting policy ----------
    def student_sample_accuracy(self, temp):
        """Return the manufacturer specification used in channel reports.

        This value is not labelled as expanded uncertainty (k=2). No fitted
        reference-standard relationship or extrapolation is applied.
        """
        return self.instrument_accuracy(temp)


# ====================== Utility functions ======================
def wait_for_file_ready(file_path, timeout=30, interval=0.2, stable_checks=5):
    """Wait until a newly created TXT/OPM has stopped growing.

    Five unchanged checks at 0.2 s intervals require roughly one full second
    of size stability.  This avoids parsing a multi-megabyte OPM while
    MeltView is still writing it.
    """
    start_time = time.time()
    size = -1
    stable_count = 0
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(file_path)
            if current_size == size and current_size > 0:
                stable_count += 1
                if stable_count >= stable_checks:
                    return True
            else:
                size = current_size
                stable_count = 0
        except OSError:
            stable_count = 0
        time.sleep(interval)
    return False


def read_optimelt_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.readlines()
    except:
        with open(file_path, "r", encoding="gbk", errors="ignore") as f:
            return f.readlines()


# ====================== Explicit TXT metadata refresh ======================
# An administrator invokes this utility outside the automatic OPM ingestion
# loop. It updates instrument_metadata.json for the relevant serial number and
# never changes the project's reference-standard relationship or QA dates.
def _parse_meltview_date(value):
    """Return an ISO date for supported MeltView date forms, else None."""
    text = str(value or "").strip()
    match = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)),
                            int(match.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    for pattern, fmt in ((r"\b\d{1,2}[A-Za-z]{3}\d{2,4}\b", "%d%b%y"),
                         (r"\b\d{1,2}[A-Za-z]{3}\d{4}\b", "%d%b%Y")):
        match = re.search(pattern, text)
        if match:
            try:
                return datetime.strptime(match.group(0), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def refresh_instrument_calibration_metadata_from_txt(
        txt_path, qa_state_root=None, review_window_days=183, as_of=None):
    """Refresh instrument-recorded calibration metadata from one MeltView TXT.

    This is an optional administrative QA initialisation/refresh step.  It is
    deliberately separate from automatic OPM run ingestion and from the
    project-maintained reference-standard calibration state.
    """
    fields = {}
    for raw_line in read_optimelt_file(txt_path):
        match = re.match(r"^([^:]+):\s*(.*)$", raw_line.strip())
        if match:
            fields[match.group(1).strip().lower()] = match.group(2).strip()

    serial = re.sub(r"[^A-Za-z0-9_.-]", "_",
                    fields.get("instrument serial number", "").strip())
    if not serial:
        raise ValueError("TXT calibration metadata refresh requires an instrument serial number")

    aliases = {
        "oven_calibrated_date": ("oven calibrated date", "oven calibration date"),
        "camera_calibrated_date": ("camera calibrated date", "camera calibration date"),
        "calibration_expiration_date": (
            "calibration expiration date", "temperature calibration expiration date",
            "calibration expiry date", "calibration expires"),
    }
    parsed = {}
    present_but_invalid = []
    for output_name, labels in aliases.items():
        raw = next((fields[label] for label in labels if label in fields), None)
        parsed[output_name] = _parse_meltview_date(raw)
        if raw is not None and parsed[output_name] is None:
            present_but_invalid.append(output_name)

    today = as_of.date() if isinstance(as_of, datetime) else (as_of or datetime.now().date())
    expiry = parsed["calibration_expiration_date"]
    oven = parsed["oven_calibrated_date"]
    if expiry:
        expired = today > datetime.strptime(expiry, "%Y-%m-%d").date()
        status = "expired_by_instrument_record" if expired else "current_by_instrument_record"
        reason = "status determined from an explicit expiration date in the TXT report"
    elif oven:
        oven_date = datetime.strptime(oven, "%Y-%m-%d").date()
        if today <= oven_date + timedelta(days=int(review_window_days)):
            status = "within_configured_review_window"
            reason = ("no instrument expiration date was present; status uses the "
                      "configured review window")
        else:
            status = "unknown"
            reason = ("recorded oven calibration date exceeds the configured review "
                      "window, but no instrument expiration date was present")
    else:
        status = "unknown"
        reason = "no parseable oven calibration or expiration date was present"

    record = {
        "instrument_serial_number": serial,
        "instrument_recorded_metadata": {
            **parsed,
            "status": status,
            "reason": reason,
            "review_window_days": int(review_window_days),
            "invalid_date_fields": present_but_invalid,
            "source_type": "MeltView TXT export",
            "source_file": os.path.basename(str(txt_path)),
            "refreshed_at": today.isoformat(),
        },
    }
    root = qa_state_root or QA_STATE_ROOT
    instrument_dir = os.path.join(root, serial)
    os.makedirs(instrument_dir, exist_ok=True)
    target = os.path.join(instrument_dir, "instrument_metadata.json")
    fd, staging = tempfile.mkstemp(prefix=".instrument_metadata_", suffix=".json",
                                   dir=instrument_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
        os.replace(staging, target)
    except Exception:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise
    return record


def load_instrument_recorded_calibration_metadata(serial_number, qa_state_root=None):
    """Load TXT-derived metadata for exactly one instrument, failing closed."""
    serial = re.sub(r"[^A-Za-z0-9_.-]", "_", str(serial_number or "").strip())
    if not serial:
        return {"availability": "unavailable", "reason": "instrument serial number missing"}
    path = os.path.join(qa_state_root or QA_STATE_ROOT, serial,
                        "instrument_metadata.json")
    if not os.path.exists(path):
        return {"availability": "not_refreshed",
                "reason": "TXT calibration metadata has not been refreshed"}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except Exception as exc:
        return {"availability": "unavailable",
                "reason": f"metadata file could not be read: {type(exc).__name__}"}
    if str(record.get("instrument_serial_number", "")).strip() != serial:
        return {"availability": "unavailable",
                "reason": "instrument identity mismatch"}
    metadata = record.get("instrument_recorded_metadata")
    if not isinstance(metadata, dict):
        return {"availability": "unavailable",
                "reason": "instrument metadata object missing"}
    return {"availability": "available", **metadata}


def _instrument_recorded_calibration_text(serial_number, qa_state_root=None):
    record = load_instrument_recorded_calibration_metadata(serial_number, qa_state_root)
    availability = record.get("availability")
    if availability == "not_refreshed":
        return "Not available - TXT calibration metadata has not been refreshed."
    if availability != "available":
        return f"Unavailable - {record.get('reason', 'metadata could not be read')}."
    labels = {
        "current_by_instrument_record": "Current by explicit instrument-recorded expiration date",
        "expired_by_instrument_record": "Expired by explicit instrument-recorded expiration date",
        "within_configured_review_window": (
            "Within locally configured review window; this is not a calibration-validity statement"),
        "unknown": "Unknown",
    }
    parts = [labels.get(record.get("status"), "Unknown")]
    if record.get("oven_calibrated_date"):
        parts.append(f"oven date {record['oven_calibrated_date']}")
    if record.get("camera_calibrated_date"):
        parts.append(f"camera date {record['camera_calibrated_date']}")
    if record.get("calibration_expiration_date"):
        parts.append(f"expiration date {record['calibration_expiration_date']}")
    elif record.get("status") in {"unknown", "within_configured_review_window"}:
        parts.append("no explicit expiration date in TXT")
    if record.get("refreshed_at"):
        parts.append(f"metadata refreshed {record['refreshed_at']}")
    parts.append("source: MeltView TXT")
    return "; ".join(parts) + "."


def parse_acquired_time(time_str):
    if not time_str:
        return "Unknown"
    # Accept the common year/month/day suffixes without embedding locale text.
    match_localized = re.search(
        r'(\d{4})\u5e74(\d{1,2})\u6708(\d{1,2})\u65e5', str(time_str))
    if match_localized:
        return (f"{match_localized.group(1)}-"
                f"{match_localized.group(2).zfill(2)}-"
                f"{match_localized.group(3).zfill(2)}")
    match_en = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', str(time_str))
    if match_en:
        return f"{match_en.group(1)}-{match_en.group(2).zfill(2)}-{match_en.group(3).zfill(2)}"
    # OPM stores dates such as 23Mar26.
    match_opm = re.search(r'\b(\d{1,2}[A-Za-z]{3}\d{2})\b', str(time_str))
    if match_opm:
        try:
            return datetime.strptime(match_opm.group(1), "%d%b%y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return "Unknown"


def load_txt_input(path):
    """Load the legacy MeltView text export into meta + DataFrame."""
    meta, rows, header = {}, [], []
    malformed_rows = []
    start = False
    for line in read_optimelt_file(path):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([^:]+):\s*(.*)$", line)
        if m:
            meta[m.group(1).strip()] = m.group(2).strip()
            continue
        if line.startswith("Time(s)"):
            start = True
            header = [x.strip() for x in line.split("\t") if x.strip()]
            continue
        if line == "[End of document]":
            break
        if start:
            row = [x.strip() for x in line.split("\t") if x.strip()]
            if len(row) == len(header):
                rows.append(row)
            else:
                malformed_rows.append(line)
    if not header or not rows:
        raise ValueError("TXT contains no supported measurement table")
    if malformed_rows:
        raise ValueError(
            f"TXT contains {len(malformed_rows)} row(s) with an unexpected column count"
        )
    meta["Source format"] = "TXT"
    meta["Source file"] = os.path.basename(str(path))
    return meta, pd.DataFrame(rows, columns=header).astype(float)


def load_opm_input(path):
    """Load an automatically saved MeltView OPM into the legacy data model."""
    if not OPM_SUPPORT:
        raise RuntimeError(
            "OPM support unavailable. Put opm_parser.py in the same folder as this program."
        )
    result = parse_opm(path, include_images=False)
    m = result["metadata"]
    points = result["points"]

    meta = {
        "Acquired on": f'{m["acquired_date"]} {m["acquired_time"]}',
        "Chemical name": m["chemical_name"],
        "Batch number": m["batch_number"],
        "Start temperature": f'{m["start_temperature_c"]:.1f} C',
        "Stop temperature": f'{m["stop_temperature_c"]:.1f} C',
        "Heating rate": f'{m["heating_rate_c_min"]:.1f} C/min',
        "Onset point threshold": f'{m["onset_threshold_percent"]}%',
        "Single point threshold": f'{m["single_threshold_percent"]}%',
        "Clear point threshold": f'{m["clear_threshold_percent"]}%',
        "Instrument name": m["instrument_name"],
        "Instrument serial number": str(m["instrument_serial_number"]),
        "Source format": "OPM",
        "Source file": os.path.basename(path),
    }
    for stage in ("onset", "single", "clear"):
        for channel in ("left", "center", "right"):
            val = points[stage][channel]
            meta[f"{stage.capitalize()} point ({channel})"] = f"{val:.6f} C"

    df = pd.DataFrame([
        {
            "Time(s)": frame.time_s,
            "Temp(C)": frame.temp_c,
            "Left": frame.left,
            "Center": frame.center,
            "Right": frame.right,
        }
        for frame in result["frames"]
    ])
    if df.empty:
        raise ValueError("OPM contains no measurement frames")
    return meta, df


def clean_filename(text):
    illegal_chars = r'[\\/:*?"<>|]'
    return re.sub(illegal_chars, "", str(text)).strip()


def clean_pdf_text(text):
    return re.sub(r'[^\x00-\x7F]+', '', str(text)).strip() or "N/A"


def get_temp_value(meta, key):
    val_str = meta.get(key, "").replace("C", "").strip()
    try:
        val = float(val_str)
        return val if val > 0 else None
    except ValueError:
        return None


def format_temp(val):
    return f"{val:.2f}" if val is not None else "N/A"


class RequiredRunDataError(ValueError):
    """A required provenance field or measurement structure is unavailable."""


def validate_required_run_data(meta, df):
    """Fail closed when required run provenance or measurement data are absent."""
    serial = str(meta.get("Instrument serial number", "")).strip()
    if not serial or serial.upper() == "UNKNOWN":
        raise RequiredRunDataError("Required run field missing: instrument serial number")
    if parse_acquired_time(meta.get("Acquired on")) == "Unknown":
        raise RequiredRunDataError("Required run field missing or invalid: acquisition date")
    ramp_match = re.search(r"[-+]?\d+(?:\.\d+)?",
                           str(meta.get("Heating rate", "")))
    ramp = float(ramp_match.group(0)) if ramp_match else None
    if ramp is None or not np.isfinite(ramp) or ramp <= 0:
        raise RequiredRunDataError(
            "Required measurement condition missing or invalid: heating rate")
    if not str(meta.get("Source file", "")).strip():
        raise RequiredRunDataError("Required provenance field missing: source file")
    if meta.get("Source format") not in {"OPM", "TXT"}:
        raise RequiredRunDataError("Required provenance field missing or invalid: source format")
    required_columns = {"Time(s)", "Temp(C)", "Left", "Center", "Right"}
    missing = sorted(required_columns.difference(df.columns))
    if df.empty or missing:
        detail = f"; missing columns: {missing}" if missing else ""
        raise RequiredRunDataError(f"Required measurement series missing or empty{detail}")
    return ramp


def _pipeline_timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _add_months(moment, months):
    """Shift a date by whole months, clamping to the last valid day."""
    total = moment.month - 1 + int(months)
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def append_pipeline_log(record, log_root=None):
    """Append and synchronise one structured record to the daily TXT log."""
    root = log_root or PIPELINE_LOG_ROOT
    os.makedirs(root, exist_ok=True)
    payload = dict(record)
    payload.setdefault("timestamp", _pipeline_timestamp())
    if payload.get("reason") is not None:
        payload["reason"] = str(payload["reason"])[:1000]
    day = payload["timestamp"][:10]
    path = os.path.join(root, f"pipeline_{day}.txt")
    needs_separator = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_separator = existing.read(1) not in {b"\n", b"\r"}
    line = (("\n" if needs_separator else "") +
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def recover_incomplete_pipeline_runs(log_root=None):
    """Recover starts without terminals, scoped to each daily log and idempotent.

    Terminals and integrity warnings may be written to today's daily file.
    A first pass therefore indexes every log so a later restart does not
    re-recover a run whose terminal already exists in another day's file.
    """
    root = log_root or PIPELINE_LOG_ROOT
    if not os.path.isdir(root):
        return {"interrupted": 0, "integrity_unknown": 0,
                "malformed_lines": 0, "warnings_added": 0}

    indexed = []
    all_terminals = set()
    all_warned_locations = set()
    malformed_all = []
    for path in sorted(Path(root).glob("pipeline_*.txt")):
        started, malformed = {}, []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    malformed.append((path.name, line_number))
                    continue
                run_id = record.get("run_id")
                if record.get("event") == "started" and run_id:
                    started[run_id] = record
                if record.get("event") == "terminal" and run_id:
                    all_terminals.add(run_id)
                if record.get("event") == "integrity_warning":
                    location = record.get("corrupt_location")
                    if location:
                        all_warned_locations.add(location)
        malformed_all.extend(malformed)
        indexed.append((started, malformed))

    interrupted_ids, integrity_ids = [], []
    warnings_added = 0
    for started, malformed in indexed:
        corruption_present = bool(malformed)
        for run_id, record in started.items():
            if run_id in all_terminals:
                continue
            if corruption_present:
                outcome = "integrity_unknown"
                reason = ("Start has no parseable terminal and its daily log contains "
                          "corrupt line(s); operator confirmation is required.")
                integrity_ids.append(run_id)
            else:
                outcome = "interrupted"
                reason = ("Previous run had no terminal event; possible power loss, "
                          "process termination or system failure.")
                interrupted_ids.append(run_id)
            append_pipeline_log({
                "event": "terminal", "run_id": run_id,
                "source_file": record.get("source_file"),
                "source_format": record.get("source_format"),
                "decision_path": record.get("decision_path", "pending"),
                "outcome": outcome, "failure_stage": "unknown",
                "reason": reason, "detected_on_restart": True,
            }, root)
            all_terminals.add(run_id)
        for filename, line_number in malformed:
            location = f"{filename}:{line_number}"
            if location in all_warned_locations:
                continue
            append_pipeline_log({
                "event": "integrity_warning", "run_id": None,
                "outcome": "log_corruption_detected",
                "severity": "operator_action_required",
                "failure_stage": "audit_logging",
                "corrupt_location": location,
                "reason": (f"Corrupt log line preserved at {location}; it may hide "
                           "a terminal record. Manual review required."),
            }, root)
            all_warned_locations.add(location)
            warnings_added += 1
    if malformed_all:
        print(f"INTEGRITY WARNING: {len(malformed_all)} corrupt log line(s) detected; "
              f"{len(integrity_ids)} run(s) marked 'integrity_unknown' pending review.")
    return {"interrupted": len(interrupted_ids),
            "integrity_unknown": len(integrity_ids),
            "malformed_lines": len(malformed_all),
            "warnings_added": warnings_added}


def discard_orphaned_staging_dirs(output_root=None):
    """Remove staging directories left behind by an interrupted publication.

    Must run after QA transaction recovery, which may still need to promote a
    staging directory into its final location.
    """
    root = Path(output_root or OUTPUT_FOLDER)
    if not root.is_dir():
        return 0
    discarded = 0
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        if not (candidate.name.startswith(".") and ".pending-" in candidate.name):
            continue
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            print(f"WARNING: Could not remove staging directory {candidate}: {exc}")
            continue
        discarded += 1
    return discarded


# ====================== Single-channel plotting ======================
def plot_single_channel(df, save_path, meta, channel):
    plt.figure(figsize=(10, 6))
    color_map = {"Left": "#E53935", "Center": "#1E88E5", "Right": "#43A047"}
    ch_color = color_map[channel]

    raw = df[channel].values
    baseline = float(np.mean(raw[:10]))
    if baseline <= 0:
        baseline = 1.0
    norm_abs = (raw / baseline) * 100.0

    ax = plt.gca()
    ax.plot(df['Temp(C)'], norm_abs, linewidth=2.2, color=ch_color)

    ax.set_xlabel('Temperature (°C)', fontweight='bold', fontsize=12)
    ax.set_ylabel('Normalized Absorbance (%)', fontweight='bold', fontsize=12)
    ax.grid(alpha=0.25, linestyle='--')
    ax.set_ylim(-5, 115)

    x_right = df['Temp(C)'].max()
    for y_thr, label in [(100, 'Onset thr. 100%'),
                         (30,  'Single thr. 30%'),
                         (10,  'Clear thr. 10%')]:
        ax.axhline(y_thr, color='gray', linestyle=':', linewidth=1, alpha=0.6)
        ax.text(x_right, y_thr + 1.5, label,
                fontsize=8, color='gray', ha='right', va='bottom')

    points = ["Onset", "Single", "Clear"]
    offsets = [(4, 8), (4, 8), (4, -12)]
    for p, (dx, dy) in zip(points, offsets):
        key = f"{p} point ({channel.lower()})"
        val_str = meta.get(key, "").replace("C", "").strip()
        try:
            t = float(val_str)
            if t <= 0:
                continue
        except ValueError:
            continue
        if t < df['Temp(C)'].min() or t > df['Temp(C)'].max():
            continue

        y_actual = float(np.interp(t, df['Temp(C)'], norm_abs))
        ax.scatter(t, y_actual, color=ch_color, s=110, zorder=5,
                   edgecolor='white', linewidth=2)
        ax.annotate(f'{p}\n{t:.1f}°C',
                    xy=(t, y_actual),
                    xytext=(t + dx, y_actual + dy),
                    fontsize=10, fontweight='bold', color=ch_color,
                    arrowprops=dict(arrowstyle='->', color=ch_color, lw=1.2))

    chem = meta.get('Chemical name', 'Unspecified')
    ax.set_title(f"File-level label: {chem} (not verified as channel identity)\n"
                 f"{channel} Channel | "
                 f"X = Temperature (°C),  Y = Normalized Absorbance (0–100%)",
                 fontweight='bold', fontsize=12, pad=12)

    plt.tight_layout()
    plt.savefig(save_path, dpi=300, bbox_inches='tight')
    plt.close()


# ====================== Teaching-channel PDF report ======================
# Reports contain instrument-indicated values, the recorded heating rate, the
# manufacturer accuracy specification, process-stage observations and concise
# instrument QA-state text. They contain no corrected or extrapolated values.
REPORT_IMAGE_SIZE = (430, 240)
# Report height varies with the QA status text and label lengths, so no fixed
# plot size keeps every report on one page. Summing measured flowable heights
# does not predict the rendered layout closely enough to size the plot from it,
# so each candidate size is rendered and the page count is read back. The first
# step is the natural size, which is what ordinary reports use.
_IMAGE_SCALE_STEPS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)


def _report_doc(target):
    return SimpleDocTemplate(target, pagesize=A4,
                             topMargin=28, bottomMargin=22,
                             leftMargin=48, rightMargin=48)


def _build_report_story(meta, calib, img_path, ch_data, channel, image_size,
                        ramp=None, run_id=None):
    """Assemble the report flowables for one candidate plot size."""
    styles = getSampleStyleSheet()
    small = styles["Normal"].clone('small'); small.fontSize = 8; small.leading = 10
    note = styles["Normal"].clone('note'); note.fontSize = 8; note.leading = 10
    note.textColor = colors.HexColor("#666666")
    # Default heading spacing costs about 18 pt each, which is enough to push
    # the plot onto a second page. The report is meant to be read as one sheet.
    heading = styles["Heading2"].clone('tightHeading')
    heading.spaceBefore = 6; heading.spaceAfter = 2
    title = styles["Title"].clone('tightTitle'); title.spaceAfter = 2
    # A plain string in a table cell does not wrap, so the longest parameter
    # names used to overflow into the value column.
    label = styles["Normal"].clone('label'); label.fontSize = 9; label.leading = 11
    story = []

    story.append(Paragraph(f"Melting Point Report - {channel} Channel", title))
    story.append(Spacer(1, 4))

    # ===== Run and channel information =====
    story.append(Paragraph("Run and Channel Information", heading))
    cal_date = calib.config.get("calibration_data", {}).get("date_performed", "PLACEHOLDER")
    qa_calibrated = (calib.curve_status == "measured")
    cal_date_disp = cal_date if (cal_date and cal_date != "PLACEHOLDER") else "not yet performed"
    _v = lambda s: Paragraph(str(s), small)
    _k = lambda s: Paragraph(str(s), label)
    data = [
        ["Parameter", "Value"],
        [_k("File-level label"), _v(clean_pdf_text(meta.get("Chemical name", "Unknown")) +
                                    " (not verified as channel identity)")],
        [_k("Test Date"), _v(parse_acquired_time(meta.get("Acquired on", "Unknown")))],
        [_k("Tube / Channel"), _v(channel)],
        [_k("Source file"), _v(clean_pdf_text(meta.get("Source file", "N/A")))],
        [_k("Source format"), _v(clean_pdf_text(meta.get("Source format", "N/A")))],
        [_k("Instrument serial number"), _v(clean_pdf_text(
            meta.get("Instrument serial number", "N/A")))],
        [_k("Pipeline run ID"), _v(clean_pdf_text(run_id or "N/A"))],
        [_k("Start / Stop temperature"),
         _v(clean_pdf_text(meta.get("Start temperature", "N/A")) +
            " / " + clean_pdf_text(meta.get("Stop temperature", "N/A")))],
        [_k("Heating rate"), _v(clean_pdf_text(meta.get("Heating rate", "N/A")))],
        [_k("Project QA last performed"), _v(cal_date_disp)],
        [_k("Project reference-standard QA status"), _v(_qa_status_text(calib))],
        [_k("Instrument-recorded calibration metadata"), _v(
            _instrument_recorded_calibration_text(
                meta.get("Instrument serial number", "")))],
    ]
    table = Table(data, colWidths=[170, 320])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey)
    ]))
    story.append(table)
    story.append(Spacer(1, 6))

    # ===== Reported clear point, heating rate and manufacturer specification =====
    cc = ch_data.get("clear_reported")
    story.append(Paragraph("Melting Point Result", heading))

    if cc is None or cc.get("measured") is None:
        result_line = "Clear point not detected for this channel."
        result_color = colors.HexColor("#999999")
    else:
        raw_T = cc["measured"]
        accuracy = calib.student_sample_accuracy(raw_T)
        ramp_disp = f"{ramp} °C/min" if ramp is not None else "n/a"
        head = (
            f"<b>Melting point (clear point) = {raw_T:.1f} &#176;C</b> "
            f"(&#177; {accuracy:.1f} &#176;C; manufacturer's accuracy specification)<br/>"
            f"<font size=8>Heating rate: {ramp_disp}. "
            f"This is the instrument-indicated result; no software correction is applied "
            f"to teaching-channel values. The &#177; value is the manufacturer's instrument "
            f"accuracy specification for this temperature range.</font>"
        )
        # Educational note on heating-rate effects; this is not a correction.
        head += (
            "<br/><font size=8 color='#666666'>"
            "Note: heating rate materially affects melting-point results and must be "
            "reported with the measurement. Higher rates can increase thermal lag; "
            "the size of the effect is sample-dependent. This software does not infer "
            "or apply a rate correction to an unidentified channel sample."
            "</font>"
        )
        result_color = colors.HexColor("#1565C0")
        result_line = head

    rbox = Table([[Paragraph(result_line, small)]], colWidths=[490])
    rbox.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F1F8E9")),
        ('BOX', (0, 0), (-1, -1), 1.2, result_color),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(rbox)
    story.append(Spacer(1, 6))

    # ===== Instrument-indicated process-stage observations =====
    story.append(Paragraph("Measured Points (during melting)", heading))
    P = lambda s: Paragraph(s, small)

    def mval(reported):
        return (f"{reported['measured']:.2f}" if reported and
                reported.get("measured") is not None else "N/A")

    rows_pts = [
        [P("<b>Stage</b>"), P("<b>Measured (&#176;C)</b>"), P("<b>What it means</b>")],
        [P("Onset"), P(mval(ch_data.get("onset_reported"))),
         P("First clear sign of melting (liquid appears).")],
        [P("Meniscus"), P(mval(ch_data.get("single_reported"))),
         P("Solid and liquid coexist with a visible meniscus.")],
        [P("Clear"), P(mval(ch_data.get("clear_reported"))),
         P("Fully melted - the reported melting point.")],
    ]
    t = Table(rows_pts, colWidths=[90, 110, 290])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgreen),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t)
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "Onset and meniscus are stages along the way; only the clear point is reported "
        "as the melting point (US Pharmacopoeia convention).", note))
    story.append(Spacer(1, 4))

    story.append(Image(img_path, width=image_size[0], height=image_size[1]))
    return story


def generate_channel_pdf(pdf_path, meta, calib, img_path, ch_data, channel,
                         ramp=None, run_id=None):
    """Write a single-page channel report, shrinking the plot only if needed."""
    natural_width, natural_height = REPORT_IMAGE_SIZE
    rendered = None
    for scale in _IMAGE_SCALE_STEPS:
        buffer = io.BytesIO()
        doc = _report_doc(buffer)
        story = _build_report_story(
            meta, calib, img_path, ch_data, channel,
            (natural_width * scale, natural_height * scale), ramp, run_id)
        doc.build(story)
        rendered = buffer.getvalue()
        if doc.page == 1:
            break
    with open(pdf_path, "wb") as handle:
        handle.write(rendered)


def _qa_status_text(calib):
    """Return concise instrument QA-state text for a channel report."""
    if not calib.loaded:
        return "QA calibration configuration not loaded."
    if getattr(calib, "qa_attention", None):
        att = calib.qa_attention
        return ("ATTENTION REQUIRED - a reference-standard run was out of tolerance "
                f"on {att.get('raised_on', 'an earlier date')} "
                f"(TOC {att.get('temperature_offset_correction_C')} °C, "
                f"tol +/-{att.get('tolerance_C')} °C). The previous fit is retained "
                "for reference only and must not be relied on until reviewed.")
    if calib.curve_status == "measured" and calib.calib_valid:
        return ("Instrument is within its QA-calibration validity period "
                "(vanillin / phenacetin / caffeine standards).")
    if calib.curve_status == "measured":
        return "Instrument QA calibration present but validity period unset/expired."
    return ("Instrument QA calibration has not yet been performed on this unit; "
            "instrument-indicated readings are reported without correction.")


# ====================== Main processing workflow ======================
class FileHandler(FileSystemEventHandler):
    def __init__(self):
        self.calibrators = {}

    @staticmethod
    def _instrument_id(meta):
        raw = str(meta.get("Instrument serial number", "UNKNOWN")).strip()
        return re.sub(r"[^A-Za-z0-9._-]", "_", raw) or "UNKNOWN"

    def _get_calibrator(self, meta):
        """Return an instrument-scoped QA state; never share status across serials."""
        instrument_id = self._instrument_id(meta)
        if instrument_id in self.calibrators:
            return self.calibrators[instrument_id]
        state_dir = os.path.join(QA_STATE_ROOT, instrument_id)
        os.makedirs(state_dir, exist_ok=True)
        config_path = os.path.join(state_dir, "qa_state.json")
        log_path = os.path.join(state_dir, "qa_history.jsonl")
        if not os.path.exists(config_path):
            _atomic_write_json(
                config_path, _blank_instrument_config(CALIBRATION_FILE, instrument_id))
        calibrator = MeltingPointCalibrator(config_path, log_path)
        calibrator = self._recover_qa_transaction(calibrator)
        self.calibrators[instrument_id] = calibrator
        return calibrator

    @staticmethod
    def _file_sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _find_duplicate(source_sha256):
        root = Path(OUTPUT_FOLDER)
        if not root.is_dir():
            return None
        for manifest in root.glob("*/run_manifest.json"):
            # Staging directories are hidden and carry a manifest before the
            # run is published; only a committed directory counts as duplicate.
            if manifest.parent.name.startswith("."):
                continue
            try:
                with manifest.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
                if record.get("source_sha256") == source_sha256:
                    return str(manifest.parent)
            except (OSError, json.JSONDecodeError):
                continue
        return None

    @staticmethod
    def _transaction_path(calibrator):
        return Path(calibrator.calib_file).parent / "qa_transaction.json"

    @staticmethod
    def _audit_has_transaction(calibrator, transaction_id):
        path = Path(calibrator.log_file)
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("transaction_id") == transaction_id and (
                        record.get("event") == "standard_run"):
                    return True
        return False

    @staticmethod
    def _safe_pending_output(path):
        try:
            candidate = Path(path).resolve()
            output_root = Path(OUTPUT_FOLDER).resolve()
            return candidate.parent == output_root and candidate.name.startswith(".")
        except Exception:
            return False

    @staticmethod
    def _safe_final_output(path):
        try:
            candidate = Path(path).resolve()
            output_root = Path(OUTPUT_FOLDER).resolve()
            return candidate.parent == output_root and not candidate.name.startswith(".")
        except Exception:
            return False

    def _recover_qa_transaction(self, calibrator):
        """Finish or roll back a QA transaction left by process interruption."""
        journal_path = self._transaction_path(calibrator)
        if not journal_path.exists():
            return calibrator
        try:
            with journal_path.open("r", encoding="utf-8") as handle:
                journal = json.load(handle)
            transaction_id = journal["transaction_id"]
            pending_dir = journal["pending_report_dir"]
            final_dir = journal["final_report_dir"]
            if (not self._safe_pending_output(pending_dir) or
                    not self._safe_final_output(final_dir)):
                raise QAStatePersistenceError(
                    "Unsafe report path in QA transaction journal")
            committed = (calibrator.config.get("transaction", {}).get(
                "last_committed_id") == transaction_id)
            if committed and not self._audit_has_transaction(
                    calibrator, transaction_id):
                audit_entry = journal.get("audit_entry")
                if not isinstance(audit_entry, dict):
                    raise QAStatePersistenceError(
                        "Committed QA transaction is missing its audit event")
                calibrator._append_audit_log(audit_entry)
            if committed and os.path.isdir(pending_dir) and not os.path.exists(final_dir):
                os.replace(pending_dir, final_dir)
                print(f"Recovered QA report transaction {transaction_id}")
            elif committed and not os.path.exists(final_dir):
                before = journal.get("before_config")
                if not isinstance(before, dict):
                    raise QAStatePersistenceError(
                        "Committed QA transaction has no report and no rollback snapshot")
                _atomic_write_json(calibrator.calib_file, before)
                calibrator = MeltingPointCalibrator(
                    calibrator.calib_file, calibrator.log_file)
                print(f"Rolled back incomplete QA transaction {transaction_id}")
            elif not committed and os.path.isdir(pending_dir):
                shutil.rmtree(pending_dir)
            elif not committed and os.path.exists(final_dir):
                raise QAStatePersistenceError(
                    "A QA report exists but its state transaction is not committed")
            journal_path.unlink()
            return calibrator
        except Exception as exc:
            raise QAStatePersistenceError(
                f"Could not recover instrument QA transaction: {exc}") from exc

    def recover_all_qa_transactions(self):
        """Settle QA journals for every instrument before the watcher starts.

        An instrument whose journal cannot be settled is left uncached, so its
        next run still fails closed through the normal routing path.
        """
        root = Path(QA_STATE_ROOT)
        if not root.is_dir():
            return {"recovered": 0, "failed": []}
        recovered, failed = 0, []
        for state_dir in sorted(root.iterdir()):
            if not (state_dir / "qa_transaction.json").exists():
                continue
            config_path = state_dir / "qa_state.json"
            if not config_path.exists():
                continue
            try:
                calibrator = MeltingPointCalibrator(
                    str(config_path), str(state_dir / "qa_history.jsonl"))
                self.calibrators[state_dir.name] = self._recover_qa_transaction(
                    calibrator)
                recovered += 1
            except Exception as exc:
                failed.append(state_dir.name)
                print(f"ERROR: QA transaction recovery failed for "
                      f"{state_dir.name}: {exc}")
        return {"recovered": recovered, "failed": failed}

    def _prepare_qa_transaction(self, calibrator, run_id, staging_dir, final_dir):
        if calibrator._pending_snapshot is None or calibrator._pending_state is None:
            raise QAStatePersistenceError("QA run produced no pending state transaction")
        transaction_id = f"qa-{run_id}"
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "created_at": _pipeline_timestamp(),
            "pending_report_dir": str(Path(staging_dir).resolve()),
            "final_report_dir": str(Path(final_dir).resolve()),
            "before_config": calibrator._pending_snapshot["config"],
            "audit_entry": {
                **copy.deepcopy(calibrator._pending_state),
                "transaction_id": transaction_id,
            },
        }
        _atomic_write_json(self._transaction_path(calibrator), journal)
        return transaction_id, journal

    def on_created(self, event):
        if event.is_directory:
            return
        self._ingest(event.src_path)

    def on_moved(self, event):
        # MeltView and most backup tools publish a finished file by renaming it
        # into the watched folder, which arrives as a move rather than a create.
        if event.is_directory:
            return
        self._ingest(event.dest_path)

    def _ingest(self, src_path):
        ext = os.path.splitext(src_path)[1].lower()
        if ext not in (".txt", ".opm"):
            return
        run_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        source_format = ext.lstrip(".").upper()
        try:
            append_pipeline_log({
                "event": "started", "run_id": run_id,
                "source_file": os.path.basename(src_path),
                "source_format": source_format, "decision_path": "pending",
            })
        except Exception as exc:
            print(f"ERROR: Audit log unavailable; file not processed: {exc}")
            return
        print(f"\nNew file detected; waiting for I/O: {os.path.basename(src_path)}")
        if not wait_for_file_ready(src_path):
            print(f"ERROR: File timeout: {src_path}")
            append_pipeline_log({
                "event": "terminal", "run_id": run_id,
                "source_file": os.path.basename(src_path),
                "source_format": source_format,
                "decision_path": "rejected_before_routing",
                "outcome": "failure", "failure_stage": "file_readiness",
                "reason": "File did not become stable before timeout",
            })
            return
        self.process_file(src_path, run_id=run_id, start_logged=True)

    @staticmethod
    def _has_qa_intent(meta):
        """True when the operator labelled the run with the QA_ prefix, whether
        or not the remainder resolves to a valid reserved standard."""
        raw = str(meta.get("Chemical name", "")).strip()
        return bool(_QA_INTENT_RE.match(raw))

    @staticmethod
    def _is_qa_run(path, meta):
        return FileHandler._qa_standard_name(meta) is not None

    @staticmethod
    def _qa_standard_name(meta):
        raw = str(meta.get("Chemical name", "")).strip()
        match = re.fullmatch(r"QA[_\-\s]+([A-Za-z]+)", raw, flags=re.IGNORECASE)
        if not match:
            return None
        standard = match.group(1).lower()
        return standard if standard in QA_RESERVED_NAMES else None

    @staticmethod
    def _unique_output_path(base, name):
        candidate = os.path.join(base, name)
        counter = 2
        while os.path.exists(candidate):
            candidate = os.path.join(base, f"{name}_{counter}")
            counter += 1
        return candidate

    def process_file(self, path, run_id=None, start_logged=False):
        staging_dir = None
        calibrator = None
        transaction_id = None
        qa_state_committed = False
        source_sha256 = None
        run_id = run_id or f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        source_file = os.path.basename(path)
        source_format = os.path.splitext(path)[1].lstrip(".").upper() or "UNKNOWN"
        failure_stage = "audit_logging"
        decision_path = "pending"
        instrument_serial = None
        if not start_logged:
            try:
                append_pipeline_log({
                    "event": "started", "run_id": run_id,
                    "source_file": source_file, "source_format": source_format,
                    "decision_path": "pending",
                })
            except Exception as exc:
                print(f"ERROR: Audit log unavailable; file not processed: {exc}")
                return False
        try:
            failure_stage = "input_parsing"
            ext = os.path.splitext(path)[1].lower()
            if ext == ".opm":
                meta, df = load_opm_input(path)
                print(f"OPM parsed directly: {len(df)} measurement frames")
            else:
                meta, df = load_txt_input(path)
                print(f"TXT parsed: {len(df)} measurement rows")
            failure_stage = "metadata_validation"
            ramp = validate_required_run_data(meta, df)
            instrument_serial = str(meta["Instrument serial number"]).strip()
            source_sha256 = self._file_sha256(path)
            duplicate_dir = self._find_duplicate(source_sha256)
            if duplicate_dir:
                append_pipeline_log({
                    "event": "terminal", "run_id": run_id,
                    "source_file": source_file, "source_format": source_format,
                    "instrument_serial_number": instrument_serial,
                    "source_sha256": source_sha256,
                    "decision_path": "duplicate", "outcome": "duplicate_ignored",
                    "failure_stage": None,
                    "reason": f"Identical source already committed at {duplicate_dir}",
                })
                print(f"Duplicate source ignored: {source_file}")
                return True
            failure_stage = "routing"
            # Fail-closed on QA intent that does not resolve to a reserved
            # standard, so a mistyped QA label ("QA_VANILIN", "QA_XYZ", ...) is
            # rejected rather than silently processed as a teaching run.
            if self._has_qa_intent(meta) and self._qa_standard_name(meta) is None:
                decision_path = "qa_standard"
                failure_stage = "qa_validation"
                raise QANameError(
                    "Run name declares QA intent ('QA_' prefix) but does not match a "
                    f"configured reserved standard {sorted(QA_RESERVED_NAMES)}: "
                    f"{str(meta.get('Chemical name', '')).strip()!r}. "
                    "Rejected fail-closed; no report produced.")
            decision_path = "qa_standard" if self._is_qa_run(path, meta) else "student"
            calibrator = self._get_calibrator(meta)
            date = parse_acquired_time(meta.get("Acquired on"))
            chem = meta.get('Chemical name', 'Unknown')
            name = clean_filename(f"{date}_{chem}")
            os.makedirs(OUTPUT_FOLDER, exist_ok=True)
            final_dir = self._unique_output_path(OUTPUT_FOLDER, name)
            staging_dir = tempfile.mkdtemp(prefix=f".{name}.pending-", dir=OUTPUT_FOLDER)
            out_dir = staging_dir

            # ===== Reference-standard QA path =====
            if self._is_qa_run(path, meta):
                failure_stage = "qa_validation"
                qa_standard_name = self._qa_standard_name(meta)
                standard, match_info = calibrator.match_standard_with_info(qa_standard_name)
                if standard is None:
                    raise ValueError(f"QA run does not match a configured standard: {qa_standard_name}")
                if match_info.get("method") in {"alias", "fuzzy"}:
                    print(f"WARNING: QA name matched by {match_info['method']}: '{chem}' -> "
                          f"'{standard.get('name')}' (score={match_info['score']:.3f})")
                    calibrator._append_audit_log({
                        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "event": "standard_name_match",
                        **match_info,
                    })
                self._process_standard(path, meta, df, qa_standard_name, ramp,
                                       out_dir, name, calibrator, run_id,
                                       source_sha256)
                failure_stage = "output_commit"
                transaction_id, journal = self._prepare_qa_transaction(
                    calibrator, run_id, staging_dir, final_dir)
                calibrator.commit_pending_state(transaction_id)
                qa_state_committed = True
                journal["phase"] = "state_committed"
                journal["state_committed_at"] = _pipeline_timestamp()
                _atomic_write_json(self._transaction_path(calibrator), journal)
                os.replace(staging_dir, final_dir)
                staging_dir = None
                calibrator.finalize_pending_state()
                # The report is published and the state is committed. Everything
                # below is bookkeeping and must not be reported as a failed run;
                # a surviving journal is settled on the next startup.
                try:
                    self._transaction_path(calibrator).unlink()
                except OSError as exc:
                    print(f"WARNING: QA transaction journal not removed: {exc}")
                try:
                    append_pipeline_log({
                        "event": "terminal", "run_id": run_id,
                        "source_file": source_file, "source_format": source_format,
                        "instrument_serial_number": instrument_serial,
                        "source_sha256": source_sha256,
                        "qa_transaction_id": transaction_id,
                        "decision_path": "qa_standard", "outcome": "success",
                        "failure_stage": None, "reason": None,
                    })
                except Exception as extra:
                    print(f"WARNING: QA run published but its terminal audit "
                          f"record could not be written: {extra}")
                return True

            # ===== Teaching-channel path: instrument-indicated values only =====
            failure_stage = "output_generation"
            channel_data = {}
            img_paths = {}
            results_for_csv = []

            for ch in ["Left", "Center", "Right"]:
                o = get_temp_value(meta, f"Onset point ({ch.lower()})")
                s = get_temp_value(meta, f"Single point ({ch.lower()})")
                c = get_temp_value(meta, f"Clear point ({ch.lower()})")

                # Package the measured value with the manufacturer specification.
                def pack(t):
                    if t is None:
                        return None
                    accuracy = calibrator.student_sample_accuracy(t)
                    return {"measured": round(t, 2),
                            "instrument_accuracy_C": round(accuracy, 2)}

                channel_data[ch] = {
                    "onset": o, "single": s, "clear": c,
                    "onset_reported": pack(o), "single_reported": pack(s),
                    "clear_reported": pack(c),
                }

                for label, t in [("Onset", o), ("Single", s), ("Clear", c)]:
                    if t is None:
                        results_for_csv.append({
                            "Channel": ch, "Point": label,
                            "Measured(C)": "N/A", "InstrumentAccuracy(C)": "N/A",
                            "HeatingRate(C/min)": ramp,
                        })
                    else:
                        results_for_csv.append({
                            "Channel": ch, "Point": label,
                            "Measured(C)": round(t, 2),
                            "InstrumentAccuracy(C)": round(
                                calibrator.student_sample_accuracy(t), 2),
                            "HeatingRate(C/min)": ramp,
                        })

                img = os.path.join(out_dir, f"{name}_{ch}.png")
                plot_single_channel(df, img, meta, ch)
                img_paths[ch] = img

            df.to_csv(os.path.join(out_dir, f"{name}_RawData.csv"),
                      index=False, encoding="utf-8")
            res_df = pd.DataFrame(results_for_csv)
            res_df.insert(0, "PipelineRunID", run_id)
            res_df.insert(1, "SourceFile", source_file)
            res_df.insert(2, "SourceFormat", source_format)
            res_df.insert(3, "SourceSHA256", source_sha256)
            res_df.insert(4, "InstrumentSerialNumber", instrument_serial)
            res_df.insert(5, "FileLevelLabel", str(chem))
            res_df.insert(6, "ChannelIdentityStatus",
                          "not verified from source file")
            res_df.to_csv(os.path.join(out_dir, f"{name}_Results.csv"),
                          index=False, encoding="utf-8")

            _atomic_write_json(os.path.join(out_dir, "run_manifest.json"), {
                "schema_version": 1,
                "pipeline_run_id": run_id,
                "decision_path": "student",
                "source_file": source_file,
                "source_format": source_format,
                "source_sha256": source_sha256,
                "instrument_serial_number": instrument_serial,
                "file_level_label": str(chem),
                "channel_identity_status": "not verified from source file",
                "heating_rate_C_min": ramp,
                "created_at": _pipeline_timestamp(),
            })

            for ch in ["Left", "Center", "Right"]:
                pdf_path = os.path.join(out_dir, f"{name}_Report_{ch}.pdf")
                generate_channel_pdf(pdf_path, meta, calibrator,
                                     img_paths[ch], channel_data[ch], ch, ramp,
                                     run_id=run_id)
                print(f"PDF generated: {os.path.basename(pdf_path)}")

            failure_stage = "output_commit"
            os.replace(staging_dir, final_dir)
            staging_dir = None
            try:
                append_pipeline_log({
                    "event": "terminal", "run_id": run_id,
                    "source_file": source_file, "source_format": source_format,
                    "instrument_serial_number": instrument_serial,
                    "source_sha256": source_sha256,
                    "decision_path": "student", "outcome": "success",
                    "failure_stage": None, "reason": None,
                })
            except Exception as extra:
                print(f"WARNING: Teaching run published but its terminal audit "
                      f"record could not be written: {extra}")
            print(f"Success: {name} processed as channel-level teaching output.")
            return True
        except Exception as e:
            rollback_ok = True
            if calibrator is not None and calibrator._pending_state is not None:
                try:
                    calibrator.abort_pending_state(
                        restore_disk=qa_state_committed)
                except Exception as rollback_exc:
                    rollback_ok = False
                    print(f"ERROR: QA state rollback failed: {rollback_exc}")
            if rollback_ok and staging_dir and os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir)
            if rollback_ok and calibrator is not None:
                journal_path = self._transaction_path(calibrator)
                if journal_path.exists():
                    try:
                        journal_path.unlink()
                    except OSError:
                        pass
            outcome = ("qa_rejected" if decision_path == "qa_standard" and
                       failure_stage == "qa_validation" else "failure")
            rejected_path = (decision_path if decision_path != "pending"
                             else "rejected_before_routing")
            try:
                append_pipeline_log({
                    "event": "terminal", "run_id": run_id,
                    "source_file": source_file, "source_format": source_format,
                    "instrument_serial_number": instrument_serial,
                    "decision_path": rejected_path, "outcome": outcome,
                    "failure_stage": failure_stage,
                    "reason": f"{type(e).__name__}: {e}",
                })
            except Exception as log_exc:
                print(f"ERROR: Could not persist terminal pipeline log: {log_exc}")
            print(f"ERROR: Could not process {path}: {str(e)}")
            return False

    def _process_standard(self, path, meta, df, chem, ramp, out_dir, name,
                          calibrator, run_id, source_sha256):
        """Process a reference-standard run for instrument QA.

        This path writes a QA record and history entry rather than a teaching-
        channel report.
        """
        standard = calibrator.match_standard(chem)
        if standard is None:
            raise ValueError(f"No configured QA standard definition for {chem!r}")
        qa_checks = self._validate_standard_run(meta, ramp, chem, standard)
        clears = qa_checks["clear_points_C"]

        mean_clear = round(sum(clears) / len(clears), 2)
        operator = meta.get("Operator") or meta.get("Technician") or meta.get("Performed by")
        result = calibrator.record_standard_run(
            chem, mean_clear, ramp_rate=ramp, operator=operator, persist=False)

        df.to_csv(os.path.join(out_dir, f"{name}_RawData.csv"),
                  index=False, encoding="utf-8")

        rec = {
            "pipeline_run_id": run_id,
            "source_file": os.path.basename(path),
            "source_format": meta.get("Source format"),
            "source_sha256": source_sha256,
            "instrument_serial_number": meta.get("Instrument serial number"),
            "standard": chem,
            "measured_clear_mean_C": mean_clear,
            "n_tubes": len(clears),
            "ramp_C_min": ramp,
            "curve_status_after": calibrator.curve_status,
            "slope": calibrator.curve["slope"],
            "intercept": calibrator.curve["intercept"],
            "r_squared": calibrator.curve["r_squared"],
            "three_capillary_spread_C": qa_checks["clear_spread_C"],
            "melting_ranges_C": qa_checks["melting_ranges_C"],
            "observed_start_C": qa_checks["observed_start_C"],
            "expected_start_C": qa_checks["expected_start_C"],
            "observed_stop_C": qa_checks["observed_stop_C"],
            "expected_stop_C": qa_checks["expected_stop_C"],
            "run_conditions_passed": True,
            "capillary_agreement_passed": True,
            "melting_range_passed": True,
        }
        # Store the drift result as a separate component of the overall outcome.
        drift = result.get("drift") if isinstance(result, dict) else None
        if drift:
            rec.update({
                "temperature_offset_correction_C": drift["temperature_offset_correction_C"],
                "drift_tolerance_C": drift["tolerance_C"],
                "drift_within_tolerance": drift["within_tolerance"],
                "drift_recommendation": drift["recommendation"],
            })
        rec["overall_qa_outcome"] = (
            "pass" if drift and drift.get("within_tolerance")
            else "attention_required")
        pd.DataFrame([rec]).to_csv(
            os.path.join(out_dir, f"{name}_CalibrationRecord.csv"),
            index=False, encoding="utf-8")

        _atomic_write_json(os.path.join(out_dir, "run_manifest.json"), {
            "schema_version": 1,
            "pipeline_run_id": run_id,
            "decision_path": "qa_standard",
            "source_file": os.path.basename(path),
            "source_format": meta.get("Source format"),
            "source_sha256": source_sha256,
            "instrument_serial_number": meta.get("Instrument serial number"),
            "standard": chem,
            "heating_rate_C_min": ramp,
            "overall_qa_outcome": rec["overall_qa_outcome"],
            "created_at": _pipeline_timestamp(),
        })

        if calibrator.curve_status == "measured":
            print(f"QA run '{chem}': mean clear {mean_clear}°C recorded. "
                  f"Curve -> slope={calibrator.curve['slope']:.4f}, "
                  f"intercept={calibrator.curve['intercept']:.3f}, "
                  f"R²={calibrator.curve['r_squared']}.")
        else:
            done = sum(1 for s in calibrator.standards
                       if s.get("measured_mean_C") is not None)
            print(f"QA run '{chem}': mean clear {mean_clear}°C recorded. "
                  f"{done}/3 standards measured; need all 3 to fit the QA curve.")

    @staticmethod
    def _validate_standard_run(meta, ramp, chemical="standard", standard=None):
        """Apply the OptiMelt per-CRS Acceptability Test checks."""
        standard = standard or {}
        expected_ramp = float(standard.get("ramp_rate_C_min", 1.0))
        tolerance = float(standard.get("temperature_tolerance_C", 0.11))
        if ramp is None or abs(float(ramp) - expected_ramp) > 0.05:
            raise ValueError(
                f"QA run '{chemical}' must use {expected_ramp:.1f} °C/min; "
                f"observed {ramp if ramp is not None else 'missing'}")
        start = get_temp_value(meta, "Start temperature")
        stop = get_temp_value(meta, "Stop temperature")
        expected_start = standard.get("expected_start_C")
        expected_stop = standard.get("expected_stop_C")
        if start is None or stop is None:
            raise ValueError(
                f"QA run '{chemical}' requires numeric start and stop temperatures")
        if expected_start is None or expected_stop is None:
            raise ValueError(
                f"QA configuration for '{chemical}' lacks expected start/stop temperatures")
        if abs(start - float(expected_start)) > tolerance:
            raise ValueError(
                f"QA run '{chemical}' must start at {float(expected_start):.1f} °C; "
                f"observed {start:.1f} °C")
        if abs(stop - float(expected_stop)) > tolerance:
            raise ValueError(
                f"QA run '{chemical}' must stop at {float(expected_stop):.1f} °C; "
                f"observed {stop:.1f} °C")
        if stop <= start:
            raise ValueError(f"QA run '{chemical}' has stop temperature <= start temperature")
        onsets, clears = [], []
        for ch in ["Left", "Center", "Right"]:
            onset = get_temp_value(meta, f"Onset point ({ch.lower()})")
            clear = get_temp_value(meta, f"Clear point ({ch.lower()})")
            if onset is None or clear is None:
                raise ValueError(
                    f"QA run '{chemical}' requires onset and clear points for all three channels")
            if clear < onset:
                raise ValueError(f"QA run '{chemical}' has clear point below onset in {ch}")
            onsets.append(float(onset)); clears.append(float(clear))
        if start > min(onsets) or stop < max(clears):
            raise ValueError(
                f"QA run '{chemical}' start/stop range does not cover all detected melts")
        spread = max(clears) - min(clears)
        max_spread = float(standard.get("three_capillary_max_spread_C", 0.3))
        if spread > max_spread + 1e-9:
            raise ValueError(
                f"QA run '{chemical}' fails three-capillary agreement: "
                f"clear-point spread {spread:.2f} °C exceeds {max_spread:.1f} °C")
        ranges = [c - o for o, c in zip(onsets, clears)]
        max_range = float(standard.get("max_melting_range_C", 2.0))
        if any(value >= max_range - 1e-9 for value in ranges):
            raise ValueError(
                f"QA run '{chemical}' fails melting-range check: "
                f"ranges {[round(v, 2) for v in ranges]} °C; "
                f"each must be <{max_range:.1f} °C")
        return {"onset_points_C": onsets, "clear_points_C": clears,
                "clear_spread_C": round(spread, 3),
                "melting_ranges_C": [round(v, 3) for v in ranges],
                "observed_start_C": start, "expected_start_C": expected_start,
                "observed_stop_C": stop, "expected_stop_C": expected_stop}


def configure_observer(observer, handler):
    """Register MeltView's single automatic-save folder with the watcher."""
    observer.schedule(handler, MONITOR_FOLDER, recursive=False)
    return observer


# ====================== Watcher entry point ======================
def main():
    """Run the folder watcher until interrupted."""
    os.makedirs(MONITOR_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    os.makedirs(PIPELINE_LOG_ROOT, exist_ok=True)
    handler = FileHandler()
    # QA journals first: recovery may still promote a staging directory that
    # the sweep below would otherwise delete.
    qa_recovery = handler.recover_all_qa_transactions()
    discarded = discard_orphaned_staging_dirs()
    recovery = recover_incomplete_pipeline_runs()

    print("=" * 60)
    print("MPA100 OPM Data Pipeline")
    print("   Teaching channels: indicated value + heating rate + instrument accuracy")
    print("   Standards (vanillin/phenacetin/caffeine): QA + drift monitoring")
    print(f"Monitoring folder: {MONITOR_FOLDER}")
    print("QA labels: QA_VANILLIN / QA_PHENACETIN / QA_CAFFEINE")
    print(f"Output folder: {OUTPUT_FOLDER}")
    print(f"Legacy calibration log: {CALIBRATION_LOG}")
    print(f"Per-instrument QA root: {QA_STATE_ROOT}")
    print(f"Pipeline log root: {PIPELINE_LOG_ROOT}")
    print(f"Recovered interrupted runs: {recovery['interrupted']}")
    if qa_recovery["recovered"]:
        print(f"Settled QA transactions: {qa_recovery['recovered']}")
    if qa_recovery["failed"]:
        print("Instruments blocked by unrecoverable QA journals: "
              f"{', '.join(qa_recovery['failed'])}")
    if discarded:
        print(f"Discarded incomplete report staging directories: {discarded}")
    if recovery.get("integrity_unknown"):
        print(f"Runs needing integrity review: {recovery['integrity_unknown']} "
              f"(log corruption detected on {recovery['malformed_lines']} line(s))")
    print("=" * 60)

    observer = Observer()
    configure_observer(observer, handler)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
