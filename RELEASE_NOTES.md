<!-- release: v2.12.1728 -->

## BYD battery percentage with Zaptec

The built-in energy-flow card now shows the available BYD vehicle battery percentage beside a standalone Zaptec charger when the vehicle association is unambiguous. Missing readings and ambiguous vehicle associations stay unknown instead of displaying an old charging-session percentage.

Update through HACS and restart Home Assistant. This uses the existing BYD Home Assistant integration; no additional vehicle controls are required.

## Better solar headroom decisions in Profit Max

Profit Max now recognizes forecast solar above a known site export limit as replacement energy that cannot earn feed-in income. It can use that limited surplus to justify an earlier solar-export hold, even when the later feed-in rate is higher, while keeping its existing deadline and full-plan checks.

This improves an existing Profit Max feature. It does not add a separate hybrid inverter AC limit or allow battery charging during an export hold. It still needs a forecast that includes the available solar. Update through HACS and restart Home Assistant.
