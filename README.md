# MPA100 OPM Data Pipeline

Research software for converting Stanford Research Systems MPA100 OptiMelt
MeltView files into traceable, channel-level teaching outputs and for translating
the manufacturer's reference-standard procedure into instrument-scoped QA checks.

This repository is an English, review-ready snapshot (version 0.9.1, 2026-08-13)
prepared for a Durham University data-science dissertation. It is not vendor
software and is not a replacement for the instrument manual, laboratory SOPs or
authorised maintenance.

## What the software does

- reads supported MeltView OPM version 4 files directly;
- preserves source-file, source-format, SHA-256, instrument-serial and pipeline-run provenance;
- generates three single-page channel PDFs, three channel plots, `RawData.csv` and `Results.csv`;
- keeps teaching and reference-standard runs on separate decision paths;
- rejects unrecognised `QA_` names fail-closed;
- maintains independent QA state for each instrument serial number;
- coordinates QA-state persistence with report publication and recovery; and
- records QA history in append-only, hash-chained JSONL.

## Evidence boundary

An OPM file establishes run metadata, the instrument serial number, configured
conditions, three channels, nine detection points and the recorded series. It
does **not** establish which student owns a channel or prove that the file-level
chemical label identifies the material in each channel. Reports therefore label
that field as:

> File-level label — not verified as channel identity

Teaching-channel results are instrument-indicated values. The pipeline does not
apply a reference-standard fit, a heating-rate correction or a software
temperature correction to them. The reported ± value is the manufacturer's
accuracy specification for the relevant temperature zone; it is not labelled as
expanded uncertainty.

The QA path is activated only by the reserved labels `QA_VANILLIN`,
`QA_PHENACETIN` and `QA_CAFFEINE`. It supports instrument-condition review only.
The software does not control heating or write offsets back to the MPA100.

## Repository layout

| Path | Purpose |
|---|---|
| `mpa_pipeline.py` | File watcher, routing, outputs, QA state and recovery |
| `opm_parser.py` | Defensive OPM version 4 binary parser |
| `process_once.py` | Process one or more files without starting the watcher |
| `qa_admin.py` | Inspect state, refresh TXT metadata and clear an alert after authorised review |
| `config/qa_template.json` | Definitions-only reference-standard and manufacturer-specification template |
| `tests/` | Data-independent regression tests |
| `evaluation/` | Data-dependent robustness and OPM–TXT consistency tools, plus saved results |
| `data-private/` | Local-only input location; contents are ignored by Git |
| `docs/` | Architecture, testing, release and data-availability notes |

## Installation

Python 3.12 or 3.13. The 0.9.1 regression run used Python 3.13.5 on Windows.

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

### Linux or macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
```

## Run the public test suite

```bash
python -m pytest -q
```

The current result is 50/50 regression tests passed on Python 3.13.5 (0.9.1).
The 0.9.0 snapshot recorded 32/32 before the recovery, ingestion and
report-layout fixes added further cases. These are author/developer-run tests,
not independent validation.

## Process files once

```bash
python process_once.py path/to/run.opm \
  --output output \
  --qa-state-root runtime/qa_by_instrument \
  --log-root runtime/pipeline_logs
```

An identical source file is ignored when its SHA-256 is already present in a
committed `run_manifest.json`.

## Run the folder watcher

The watcher observes exactly one folder: the location MeltView is configured to
auto-save to. That path is site-specific and is fixed when the instrument is
commissioned, so it must be supplied at deployment. **The paths below are
development-stage examples and are not prescribed values.**

```powershell
$env:MPA100_MONITOR_FOLDER = "E:\MPA100txt"
$env:MPA100_OUTPUT_FOLDER = "E:\MPA100txt\Auto_Converted_Results"
$env:MPA100_QA_STATE_ROOT = "E:\MPA100_state\qa_by_instrument"
$env:MPA100_PIPELINE_LOG_ROOT = "E:\MPA100_state\pipeline_logs"
py mpa_pipeline.py
```

Two constraints apply when these are replaced with real instrument paths:

- **The monitored folder is not scanned recursively.** Only files written
  directly into it are ingested; anything saved into a subfolder is ignored.
- **`MPA100_MONITOR_FOLDER` falls back to a development default.** If it is not
  set, the watcher creates that default path and observes it rather than
  refusing to start, so it reports a healthy startup while processing nothing.
  Before leaving the watcher running, confirm that the folder printed in the
  startup banner is the instrument's auto-save folder.

`MPA100_QA_TEMPLATE` can point to an approved local copy of the definitions-only
template. Record the deployed paths and configuration version in the laboratory
deployment record.

## QA administration

```bash
python qa_admin.py refresh-instrument-metadata path/to/export.txt
python qa_admin.py show-status SN-001
python qa_admin.py clear-attention SN-001 \
  --actor "authorised-role" \
  --reason "documented review reason"
```

A later passing standard run does not silently clear a previous
`attention_required` state. Clearing requires an actor and reason, and a new QA
cycle is still needed to establish current evidence.

## Evaluation evidence

The saved snapshot contains three separately labelled evidence layers:

| Evidence | Saved result | Supported conclusion |
|---|---:|---|
| Regression tests | 50/50 passed | The specified routing, transaction, state-lifecycle, recovery and report-layout cases behaved as designed |
| Robustness and integration evaluation | 28/28 passed; 9/9 malformed cases rejected | No silent errors were detected in the predefined and executed cases |
| One OPM v4/TXT pair | 205/205 rows; no differences exceeded the predefined tolerances | Shared fields in the two vendor output paths were consistent for the examined run |

The OPM and TXT were generated by the same vendor software, so their comparison
is a **cross-format consistency check**, not independent measurement validation.
The real pair is not included in the public repository because laboratory
release permission has not been established. The 28/28 and 205-row figures are
the saved 0.9.0 results; they were not re-run for 0.9.1. See
[Testing](docs/TESTING.md) and [Data availability](docs/DATA_AVAILABILITY.md).

## Current limitations

- direct-format evidence is limited to one paired OPM version 4/TXT run;
- cross-instrument and cross-version compatibility has not been demonstrated;
- the QA logic has not yet been evaluated with a complete set of real vanillin,
  phenacetin and caffeine runs from each instrument;
- channel-to-student mapping, registration, access control and report distribution
  are outside this repository;
- hash chaining can reveal some corruption or editing but is not an external
  signature, WORM store or tamper-proof audit service; and
- extreme operating-system and storage failures have not been exhaustively tested.

## Reference procedure

The definitions-only template is based on the *SRS MPA100 OptiMelt Operation and
Service Manual*, Rev. 3.4 (January 2026), Chapter 4 and Appendix B:
[MPA100 manual](https://www.thinksrs.com/downloads/pdfs/manuals/MPA100m.pdf).
Laboratory-approved values and SOPs remain authoritative for deployment.

## Citation and licence status

Citation metadata are provided in `CITATION.cff`. The software licence is pending
confirmation of ownership and institutional requirements. Until that decision is
recorded, this repository grants no permission to copy, modify or redistribute
the software beyond rights provided by law. See `LICENSE.md`.

