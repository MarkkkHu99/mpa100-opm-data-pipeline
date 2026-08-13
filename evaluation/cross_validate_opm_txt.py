"""Check shared fields in a MeltView OPM/TXT pair from the same run.

This is a cross-format consistency check between two vendor outputs, not an
independent validation of the physical measurement or instrument accuracy.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from pathlib import Path
import re
import sys
import types

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_pipeline(source: Path):
    try:
        import watchdog  # noqa: F401
    except ImportError:
        watchdog = types.ModuleType("watchdog")
        observers = types.ModuleType("watchdog.observers")
        events = types.ModuleType("watchdog.events")
        observers.Observer = type("Observer", (), {})
        events.FileSystemEventHandler = type("FileSystemEventHandler", (), {})
        sys.modules.update({"watchdog": watchdog, "watchdog.observers": observers,
                            "watchdog.events": events})
    spec = importlib.util.spec_from_file_location("mpa_pipeline_crossvalidation", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def numeric(value):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("opm", type=Path)
    ap.add_argument("txt", type=Path)
    ap.add_argument("--main", type=Path, default=REPO_ROOT / "mpa_pipeline.py")
    ap.add_argument("--json", type=Path,
                    default=REPO_ROOT / "evaluation/results/crossvalidation_results.json")
    ap.add_argument("--csv", type=Path,
                    default=REPO_ROOT / "evaluation/results/crossvalidation_results.csv")
    ap.add_argument("--markdown", type=Path,
                    default=REPO_ROOT / "evaluation/results/crossvalidation_summary.md")
    ap.add_argument("--point-tolerance", type=float, default=0.05)
    ap.add_argument("--time-temp-tolerance", type=float, default=0.001)
    ap.add_argument("--signal-tolerance", type=float, default=0.00001)
    ap.add_argument(
        "--redact-identifiers", action="store_true",
        help="redact source filenames and measurement values in saved outputs")
    args = ap.parse_args()

    pipeline = load_pipeline(args.main.resolve())
    opm_meta, opm_df = pipeline.load_opm_input(args.opm)
    txt_meta, txt_df = pipeline.load_txt_input(args.txt)

    details = []

    def add(category, field, opm_value, txt_value, abs_diff, tolerance, matched):
        details.append({"category": category, "field": field,
                        "opm_value": opm_value, "txt_value": txt_value,
                        "absolute_difference": abs_diff, "tolerance": tolerance,
                        "matched": bool(matched)})

    text_fields = ["Chemical name", "Batch number", "Instrument serial number"]
    for field in text_fields:
        left = str(opm_meta.get(field, "")).strip()
        right = str(txt_meta.get(field, "")).strip()
        add("metadata", field, left, right, None, "exact", left == right)

    numeric_fields = ["Start temperature", "Stop temperature", "Heating rate",
                      "Onset point threshold", "Single point threshold",
                      "Clear point threshold"]
    for field in numeric_fields:
        left, right = numeric(opm_meta.get(field)), numeric(txt_meta.get(field))
        diff = None if left is None or right is None else abs(left - right)
        add("metadata", field, left, right, diff, args.point_tolerance,
            diff is not None and diff <= args.point_tolerance)

    point_fields = [f"{stage} point ({channel})"
                    for stage in ("Onset", "Single", "Clear")
                    for channel in ("left", "center", "right")]
    for field in point_fields:
        left, right = numeric(opm_meta.get(field)), numeric(txt_meta.get(field))
        diff = None if left is None or right is None else abs(left - right)
        add("detection_point", field, left, right, diff, args.point_tolerance,
            diff is not None and diff <= args.point_tolerance)

    expected_columns = ["Time(s)", "Temp(C)", "Left", "Center", "Right"]
    column_order_match = list(opm_df.columns) == expected_columns == list(txt_df.columns)
    row_count_match = len(opm_df) == len(txt_df)
    series = {}
    for column in expected_columns:
        if row_count_match and column in opm_df and column in txt_df:
            delta = np.abs(opm_df[column].to_numpy() - txt_df[column].to_numpy())
            tolerance = (args.time_temp_tolerance if column in ("Time(s)", "Temp(C)")
                         else args.signal_tolerance)
            series[column] = {"max_absolute_difference": float(delta.max()),
                              "mismatch_count": int((delta > tolerance).sum()),
                              "tolerance": tolerance}
        else:
            series[column] = {"max_absolute_difference": None,
                              "mismatch_count": None, "tolerance": None}

    field_mismatches = sum(not row["matched"] for row in details)
    series_mismatches = sum((item["mismatch_count"] or 0) for item in series.values())
    point_rows = [row for row in details if row["category"] == "detection_point"]
    summary = {
        "opm_file": args.opm.name, "txt_file": args.txt.name,
        "opm_rows": len(opm_df), "txt_rows": len(txt_df),
        "row_count_match": row_count_match,
        "channel_column_order_match": column_order_match,
        "metadata_fields_compared": len(text_fields) + len(numeric_fields),
        "metadata_mismatches": sum(not r["matched"] for r in details
                                   if r["category"] == "metadata"),
        "detection_points_compared": len(point_rows),
        "detection_point_mismatches": sum(not r["matched"] for r in point_rows),
        "time_series": series,
        "field_mismatches": field_mismatches,
        "time_series_mismatched_cells": series_mismatches,
        "silent_numerical_errors": field_mismatches + series_mismatches,
        "passed": bool(row_count_match and column_order_match and
                       field_mismatches == 0 and series_mismatches == 0),
        "details": details,
    }
    if args.redact_identifiers:
        summary["opm_file"] = "[redacted].opm"
        summary["txt_file"] = "[redacted].txt"
        for row in summary["details"]:
            row["opm_value"] = "[redacted]"
            row["txt_value"] = "[redacted]"
    args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(details[0]))
        writer.writeheader(); writer.writerows(details)
    lines = ["# OPM–TXT cross-format consistency check", "",
             f"- Result: {'PASS' if summary['passed'] else 'FAIL'}",
             f"- Rows: OPM {len(opm_df)}, TXT {len(txt_df)}",
             f"- Metadata mismatches: {summary['metadata_mismatches']}",
             f"- Detection-point mismatches: {summary['detection_point_mismatches']}",
             f"- Time-series mismatched cells: {series_mismatches}",
             f"- Channel order matched: {column_order_match}", "",
             ("Both files were produced by the same vendor workflow. This result "
              "supports consistency of shared fields for the examined pair; it is "
              "not independent measurement validation."), "",
             "| Column | Maximum absolute difference | Tolerance | Mismatched cells |",
             "|---|---:|---:|---:|"]
    for column, item in series.items():
        lines.append(f"| {column} | {item['max_absolute_difference']} | "
                     f"{item['tolerance']} | {item['mismatch_count']} |")
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
