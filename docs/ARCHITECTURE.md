# Architecture and evidence boundaries

## Processing paths

The watcher observes one MeltView automatic-save directory. After file-stability
and format checks, the pipeline calculates a source SHA-256, checks for a prior
committed manifest and evaluates the file-level label.

- A recognised reserved QA label enters the reference-standard path.
- A label that declares `QA_` intent but is not recognised is rejected.
- All other supported runs enter the teaching-channel path.

The teaching path produces three channel reports, three plots and two CSV files.
It preserves the file-level label but explicitly states that the label is not a
verified channel identity.

The QA path validates the configured start temperature, stop temperature,
heating rate, three-capillary agreement and melting ranges before comparing the
mean measured clear point with the assigned value. QA state is isolated by
instrument serial number.

## Transaction and recovery model

For a QA run, the software stages report files, snapshots in-memory state and
writes a transaction journal. It then commits state and publishes the report
directory using atomic replacement operations supported by the host file system.
Expected exceptions trigger compensating rollback. Startup recovery uses the
transaction identifier to complete publication or restore prior state.

This protocol was tested under the predefined write-failure and interruption
cases. It is not a proof of atomicity under every storage, kernel or power-loss
condition.

## Information model

| Level | Established by the current pipeline | Not established by OPM alone |
|---|---|---|
| Run | Time, instrument serial, settings, source file and source hash | Which students participated |
| Channel | Left/Center/Right, three points per channel and recorded curve | Student identity or physical material identity |
| Report | Channel output and run conditions | Final recipient or student record ownership |

