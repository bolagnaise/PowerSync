<!-- release: v2.12.1500 -->

### GloBird export earnings

The energy dashboard and Daily Cost Tracking now include your configured Super Export rate for the first eligible export quota, then the base feed-in rate. A configured total rate of 15c/kWh for 15 kWh now shows $2.25; other plans use their own configured rates and limits.

### Daily cost position

The new Estimated Net Cost Today entity combines import cost, export earnings and your configured daily supply charge. Daily and monthly average costs also include supply charges. These remain estimates and exclude conditional plan credits, demand charges and bill adjustments.

Update through HACS and restart Home Assistant. Historical bonus earnings are not backfilled: incomplete quota history can leave daily or monthly costs unknown until a complete new period is observed.
