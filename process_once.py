"""Process one or more OPM/TXT files without starting the folder watcher."""
from __future__ import annotations

import argparse
from pathlib import Path

import mpa_pipeline as mp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--qa-state-root", type=Path)
    parser.add_argument("--log-root", type=Path)
    parser.add_argument("--qa-template", type=Path)
    args = parser.parse_args()
    if args.output:
        mp.OUTPUT_FOLDER = str(args.output.resolve())
    if args.qa_state_root:
        mp.QA_STATE_ROOT = str(args.qa_state_root.resolve())
    if args.log_root:
        mp.PIPELINE_LOG_ROOT = str(args.log_root.resolve())
    if args.qa_template:
        mp.CALIBRATION_FILE = str(args.qa_template.resolve())

    handler = mp.FileHandler()
    failures = []
    for source in args.files:
        if not source.is_file():
            print(f"Not a file: {source}")
            failures.append(str(source))
            continue
        if not handler.process_file(str(source.resolve())):
            failures.append(str(source))
    if failures:
        print(f"Failed files: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
