# Octopus Energy and BottlecapDave

PowerSync can use the electricity rates already loaded by the
HomeAssistant-OctopusEnergy integration. Rate events and entity changes are
consumed during a half-hour, including Intelligent Go adjustments. The normal
half-hour sync remains a fallback. Octopus/API detection latency still applies.

In PowerSync's Octopus settings, select the BottlecapDave import and export
meters when more than one is available. The selected source's actual tariff
overrides the fallback product preset, so published Cosy, Snug, Tracker,
fixed/variable and other interval schedules can be consumed without inventing
new hardcoded product codes. Select **Published tariff API (no Intelligent dispatches)**
for the direct API path, or **No export tariff (unknown)** to clear an export binding.
Export must belong to the selected account/site;
an unavailable export rate is unknown and earns no assumed revenue in planning.

The **Octopus source status** sensor exposes the binding, original/effective
prices, adjustment/cap flags, upstream timestamps, dispatch information,
Power Up/Down events and related account entities. Those related entities include
available gas, standing-charge, usage/cost, Home Mini/Home Pro, target and reward
monitoring. Their values retain their upstream timestamps and units; delayed
meter history is not live inverter telemetry or a reconciled bill. Large
collections are excluded from recorder attributes.

Intelligent prices follow BottlecapDave's planned/started policy. A planned
dispatch or dispatching binary sensor does not prove actual EV charging. Bump,
away, cancelled and provider-limited dispatches cannot independently establish
a household discount. Use the upstream integration's documented source policy
and diagnostics; PowerSync does not silently change those settings or control
the Octopus-managed EV. Qualifying household billing periods and actual charging
times are kept separate. Mixed five-minute planning cells use conservative prices.

The optimizer replans material changes within the same billing period, queues
the newest revision behind an in-flight solve and rejects superseded execution.
An economical allowed charge can start after that solve; a cheap rate does not
override SOC limits, grid-charge permissions, manual force modes, monitoring or
external control. Intelligent Flux retains Octopus battery ownership and is
monitoring only, including tariff uploads. Existing restoration/readback guards
still apply.

Current Power Down entities and their join service are supported alongside
legacy Saving Sessions. Existing legacy entity references are resolved to the
same account's renamed Power Down entity when available. Auto-join remains
opt-in; accepted joins stay pending until upstream joined state is reported.
Power Up/Free Electricity events are monitored separately. Baseline-dependent
credits do not turn the retail import price into zero, and missing reward rates
do not create an assumed optimizer payment.

Without BottlecapDave, the existing REST tariff path remains available. It
supplies published tariff rates, not personal Intelligent dispatch adjustments.
Update through HACS and restart Home Assistant. Inspect the source-status sensor
and selected meter after updating. Source/test/publication validation does not
establish installation or physical charging on an individual system.
