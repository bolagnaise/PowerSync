# Testing against approved installations

This page explains how approved installation data is used during investigation
and verification. For owner setup, data access, retention and revocation, read
[Diagnostics and privacy](diagnostics-privacy.md).

## Purpose and permission

Initial enrollment requires verification of the owner and installation through
a support ticket. Incident-specific diagnostic reads require a current active
ticket linked to that installation and reporter. Approved private offline
testing of identified code changes may continue after enrollment closes.

Ongoing approval avoids repeated pairing for covered work. It does not permit
unrelated browsing, candidate installation, settings changes, hardware refreshes
or live controls. Diagnostic evidence is treated as untrusted data.

## Investigation

Support starts with the reported issue and reads relevant approved settings,
state and logs. Where a causal question remains, investigation may include
related registries, traces, history, statistics and other integrations within
the approved scope. Unavailable sources and missing permission remain explicit.
Known credentials are redacted, but arbitrary free text may contain personal
information.

## Verification and limits

A focused regression test checks the affected behavior. Private comparisons use
exact immutable baseline and candidate artifacts on identical captured inputs
from available approved installations. Reconstruction and baseline/candidate
repeatability checks apply before interpreting results.

Coverage depends on the available provider, tariff, battery/EV configuration,
runtime and supported profiles. Repeated captures from one installation do not
count as additional systems. Solver replay, selected reserve-restoration and
calibrated-plant profiles do not cover every integration or hardware path.
Simulated commands and acknowledgements do not establish physical effects.
Physical performance scores require measured calibration and valid independent
observations; forecast-based scores are not observed savings.

Crashes, hangs, invalid numbers and regressions count as failed comparisons.
Missing participants, unavailable HA, invalid captures, unsupported profiles,
missing calibration and uncertain cleanup cannot count as passing coverage.
A seven-day advisory campaign requires real elapsed observation days.

Raw private captures and trajectories are deleted after processing; only
permitted bounded summaries are retained after confirmed purge. Raw inputs are
not cached as regression fixtures or copied into public tickets or source.
Reports state the tested source/runtime pair, coverage gaps, failures and
cleanup status without retaining reconstructive raw inputs.

Private comparisons supplement the mandatory protected-main full-suite release
gate. When participants are unavailable or the service is disabled, that coverage
limitation is reported and normal verification continues. Deployment, enrollment
and recovery procedures are maintained in the private operator runbook.
