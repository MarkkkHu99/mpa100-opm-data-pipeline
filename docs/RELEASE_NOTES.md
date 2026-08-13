# Release notes

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

