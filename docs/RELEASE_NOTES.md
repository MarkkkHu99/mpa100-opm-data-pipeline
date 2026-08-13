# Release notes

## 0.9.1 — 2026-08-13

Recovery, ingestion and report-layout fixes on top of 0.9.0. Behaviour under
normal operation is unchanged; every change concerns what happens after an
interrupted run, how files arrive in the watched folder, and how a channel
report is laid out.

### Recovery and durability

- Log recovery indexes every daily file before deciding, so a run whose terminal
  record was written to a later day is no longer re-recovered on each restart.
- A staging directory is no longer mistaken for a committed run during
  duplicate detection, so an interrupted publication can be reprocessed.
- Startup settles every instrument's QA journal, then discards staging
  directories left by an interrupted publication. The order is enforced by test:
  sweeping first would delete a report whose state transaction had committed.
- Bookkeeping that runs after a report is published can no longer record the run
  as failed; a surviving journal is settled at the next startup instead.

### Report layout

- A channel report is now a single page. Report height varies with the QA
  status text and label lengths, so the plot size is chosen by rendering the
  page and reading back the page count rather than by estimating heights. The
  full plot size is kept whenever it fits, which is every case the current code
  can produce.
- The longest parameter names no longer overflow the information table's label
  column.

### Ingestion and dates

- Files published into the watched folder by rename are ingested; previously
  only creation events were observed.
- Recalibration expiry clamps to the last valid day of the target month instead
  of raising on month-end dates.

### Evaluation

- 50/50 regression tests passed on Windows 10 with Python 3.13.5 (author run,
  2026-08-13). The suite grew from the 32/32 recorded for 0.9.0 because the
  recovery, ingestion and report-layout fixes added cases.
- The robustness, malformed-input and OPM/TXT figures below are unchanged from
  0.9.0. They were not re-run: the laboratory OPM/TXT pair is not in the public
  repository, and this machine does not hold a local copy.

## 0.9.0 — 2026-08-11

This review snapshot restructures the dissertation code as an English,
GitHub-ready repository and preserves the tested v9 behaviour.

### Safety and state changes

- Unknown names declaring `QA_` intent are rejected at the production entry point.
- Deferred QA updates use runtime snapshots and can be aborted without leaking
  failed observations into a later commit.
- QA state persistence propagates write failures instead of reporting success.
- QA reports and instrument state use a transaction journal, staging and recovery.
- A passing run does not silently clear an earlier `attention_required` state.
- A new instrument receives definitions only, never another instrument's measured
  points, fit parameters, dates or trusted state.

### Reporting and provenance

- File-level chemical labels are no longer presented as verified channel identities.
- Channel PDFs include source file, source format, instrument serial and run ID.
- Duplicate sources are detected by SHA-256 and committed manifests.
- QA history is append-only hash-chained JSONL and blocks commits after detected
  corruption or chain failure.

### Evaluation

- 32/32 regression tests passed.
- 28/28 robustness and integration cases passed, including 9/9 expected rejections.
- One paired OPM v4/TXT run contained 205 rows; no differences exceeded the
  predefined tolerances.

These results are bounded to the executed cases and examined file format.
