<!-- release: v2.12.1469 -->
### Paired Powerwall power controls
Paired local Tesla systems now show the Force Power kW slider on the Home Assistant dashboard. The slider and force switches also work when Home Assistant has named or renamed the entity from your device name.

### Respect gateway dispatch limits
Some gateways accept a lower timed battery power than the system rating. PowerSync now respects the gateway's reported ceiling and retries safely, fixing discharge requests that previously fell back to a tariff. Discharge kW targets grid export; home use and the gateway battery limit can reduce the achieved export. A failed explicit kW request is reported instead of switching to an uncontrolled rate.

Thanks to **Scotty (`cool.scotty`)** and **Brett (Teslemetry)** for their help investigating local Powerwall controls.
