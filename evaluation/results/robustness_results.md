# Robustness test results

- Tests passed: 28/28
- Correct rejection rate: 9/9 (100.0%)
- Silent errors: 0

| Test | Expected | Observed | Pass | Detail |
|---|---|---|---:|---|
| valid_opm | success | success | Yes | 205 frames |
| opm_bad_magic | rejected | rejected | Yes | OPMFormatError: Not an SRS OptiMelt data file |
| opm_unsupported_version | rejected | rejected | Yes | OPMFormatError: Unsupported OPM format version 99; supported: [4] |
| opm_truncated_half | rejected | rejected | Yes | OPMFormatError: Invalid image size at frame 102 |
| opm_truncated_tail | rejected | rejected | Yes | OPMFormatError: Invalid image size at frame 204 |
| opm_invalid_image_size | rejected | rejected | Yes | OPMFormatError: Invalid image size at frame 0 |
| opm_frame_count_mismatch | rejected | rejected | Yes | OPMFormatError: Truncated frame header at frame 205 |
| txt_empty | rejected | rejected | Yes | ValueError: TXT contains no supported measurement table |
| txt_non_numeric | rejected | rejected | Yes | ValueError: could not convert string to float: 'BAD' |
| txt_wrong_column_count | rejected | rejected | Yes | ValueError: TXT contains no supported measurement table |
| txt_minimal_valid | success | success | Yes | 1 row |
| file_ready_stable | success | success | Yes | True |
| file_ready_growing | success | success | Yes | continued waiting until timeout |
| file_ready_timeout | success | success | Yes | timeout returned False |
| student_output_naming_policy | success | success | Yes | no k=2 CSV label or *_corr keys |
| standard_alias_and_false_match | success | success | Yes | explicit alias accepted; unrelated name rejected |
| drift_failure_freezes_calibration | success | success | Yes | event logged; fit data and expiry unchanged |
| missing_certified_value_fail_closed | success | success | Yes | configuration error rejected fail-closed; state unchanged |
| explicit_qa_routing_policy | success | success | Yes | plain caffeine remained student; reserved QA label accepted |
| single_folder_monitor_registration | success | success | Yes | one MeltView automatic-save folder registered |
| manufacturer_three_standard_completion | success | success | Yes | two standards rejected; complete three-standard set fitted |
| manufacturer_per_crs_run_checks | success | success | Yes | valid run accepted; ramp, spread and melting-range failures rejected |
| per_instrument_qa_state_isolation | success | success | Yes | SN-A and SN-B received separate state and history paths |
| pipeline_rejects_malformed_without_outputs | success | success | Yes | pipeline returned failure and left zero output files |
| missing_heating_rate_fail_closed | success | success | Yes | missing required heating rate rejected without outputs |
| atomic_output_on_write_failure | success | success | Yes | simulated write failure left zero committed files |
| opm_txt_crossvalidation | success | success | Yes | rows=205; metadata mismatches=0; point mismatches=0; series mismatches=0 |
| opm_end_to_end_outputs | success | success | Yes | PDF=3, CSV=2, PNG=3 |
