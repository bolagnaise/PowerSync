<!-- release: v2.12.1661 -->

## Cost Neutral with EV charging

Fixes a planning error when Cost Neutral and EV charging are enabled together. Solar absorption is now calculated using the available surplus after EV demand and the battery's charging-power and remaining-capacity limits.

## Faster planning and clearer diagnostics

Reduces setup work for EV plans and optional free-charge preferences, and keeps the primary plan if a preference solve fails. Diagnostics now show all solve attempts and why a fallback plan was selected.

Complex Cost Neutral plans can still reach the existing 30-second allowance for an individual solve; a complete cycle can take longer.

Update through HACS and restart Home Assistant.
