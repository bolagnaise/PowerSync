<!-- release: v2.12.1848 -->

## Amber forecast accuracy card now shows straight after updating

The **Amber forecast accuracy** dashboard card stayed hidden until the first forecast export spike had settled, because the **Amber Forecast Accuracy** sensor reads `unknown` until then. The card now appears as soon as the sensor exists and shows its "still collecting" view with zero counts until there is enough data.

Update through HACS, restart Home Assistant and refresh the dashboard.
