"""Administrative commands for instrument-scoped MPA100 QA metadata/state."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import mpa_pipeline as mp


def _instrument_paths(serial: str) -> tuple[Path, Path]:
    instrument_id = re.sub(r"[^A-Za-z0-9._-]", "_", serial.strip())
    if not instrument_id:
        raise ValueError("instrument serial number is required")
    root = Path(mp.QA_STATE_ROOT) / instrument_id
    return root / "qa_state.json", root / "qa_history.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser(
        "refresh-instrument-metadata",
        help="read instrument-recorded calibration dates from a MeltView TXT export")
    refresh.add_argument("txt", type=Path)
    refresh.add_argument("--review-window-days", type=int, default=183)

    status = sub.add_parser("show-status", help="show one instrument's project QA state")
    status.add_argument("serial")

    clear = sub.add_parser(
        "clear-attention",
        help="explicitly clear attention_required after authorised review")
    clear.add_argument("serial")
    clear.add_argument("--actor", required=True)
    clear.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "refresh-instrument-metadata":
        record = mp.refresh_instrument_calibration_metadata_from_txt(
            args.txt, review_window_days=args.review_window_days)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    state_path, history_path = _instrument_paths(args.serial)
    if not state_path.exists():
        raise SystemExit(f"No QA state exists for instrument {args.serial!r}")
    calibrator = mp.MeltingPointCalibrator(str(state_path), str(history_path))
    if args.command == "show-status":
        print(json.dumps({
            "instrument_serial_number": args.serial,
            "status_text": mp._qa_status_text(calibrator),
            "qa_attention": calibrator.qa_attention,
            "curve_status": calibrator.curve_status,
            "calibration_valid": calibrator.calib_valid,
        }, ensure_ascii=False, indent=2))
        return 0

    changed = calibrator.clear_qa_attention(args.actor, args.reason)
    print(json.dumps({"cleared": changed,
                      "instrument_serial_number": args.serial}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
