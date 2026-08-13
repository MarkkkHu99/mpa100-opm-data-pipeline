# Testing and evaluation

## Public regression suite

Install the development dependencies and run from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The suite contains 32 data-independent regression tests covering reserved-name
routing, fail-closed rejection, deferred state, rollback, persistence failure,
instrument-state initialisation, configured run conditions, duplicate handling,
report/state recovery and log-integrity recovery.

## Data-dependent robustness evaluation

The evaluation runner requires a known-valid OPM version 4 file. A paired TXT
export is optional but is required for the cross-format consistency case.

```bash
python evaluation/run_robustness_suite.py data-private/run.opm \
  --txt data-private/run.txt
```

The runner derives malformed cases only inside a temporary directory. It checks
parser rejection, file-stability handling, required metadata, QA policy,
instrument isolation, atomic output staging and end-to-end artefact completeness.

## Direct cross-format consistency check

```bash
python evaluation/cross_validate_opm_txt.py \
  data-private/run.opm data-private/run.txt \
  --redact-identifiers
```

Predefined tolerances are:

| Comparison | Tolerance |
|---|---:|
| Time and temperature series | 0.001 |
| Signal series | 0.00001 |
| Numeric metadata and detection points | 0.05 °C |
| Text identifiers | Exact match |

Because OPM and TXT are two outputs of the same MeltView workflow, agreement is
reported as cross-format consistency. It does not independently validate the
underlying physical measurement.

## Saved release evidence

- Regression suite: 32/32 passed.
- Robustness and integration evaluation: 28/28 passed.
- Malformed inputs: 9/9 correctly rejected.
- No silent errors were detected in the predefined and executed cases.
- One paired OPM v4/TXT run: 205/205 rows and no differences exceeding the
  predefined tolerances.

The tests were run by the author/developer. The evidence does not establish
universal OPM compatibility, independent verification or real-instrument QA
acceptability.

