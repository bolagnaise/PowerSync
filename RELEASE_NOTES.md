<!-- release: v2.12.1628 -->

## Faster Intelligent Octopus rate updates

PowerSync now picks up BottlecapDave rate changes during an existing half-hour, including revised Intelligent Go prices. It refreshes the charging plan and enabled tariff sync without waiting for the next half-hour boundary. Upstream Octopus polling delays still apply, and charging remains subject to your normal battery limits and permissions.

## Choose the correct Octopus meters

Octopus settings now let you bind the household import and export meters, use the published tariff API, or clear an export selection. Live prices retain their adjustment information, replaced meters and expired agreements are excluded, and missing export prices no longer create assumed earnings. Select the appropriate meters if your Octopus integration has more than one source.

## Octopus source and event visibility

The new Octopus source status sensor shows the selected source, original and adjusted prices, dispatch details, Power Up/Down events and available account energy entities. Current Power Down joins are supported alongside legacy Saving Sessions, with membership confirmed from the upstream update. Baseline-dependent credits and missing rewards are kept separate from retail prices.

Intelligent Flux remains under Octopus battery control, including during optimizer startup. Update through HACS and restart Home Assistant; check the selected source after updating. This release does not establish that a particular battery accepted a command or physically charged.
