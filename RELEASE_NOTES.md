<!-- release: v2.12.1592 -->

## Automatic solar forecast fallback

When Solcast reaches its daily API limit, PowerSync now switches to Open-Meteo on the next forecast read or optimizer cycle, even if cached Solcast data remains available. It automatically returns to Solcast when the daily quota resets. The optimizer shows which fallback is active, or explains when no fallback forecast is available.

Install and configure the **Open-Meteo Solar Forecast** integration in Home Assistant for your PV arrays and keep **Solcast** selected in PowerSync’s **Weather & solar forecast** settings. The ordinary Open-Meteo weather integration does not provide this solar forecast. If Open-Meteo has no usable data, PowerSync retains usable cached Solcast forecasts.

## Solcast polling resumes after the daily reset

PowerSync’s built-in Solcast client now clears an exhausted quota before checking its cache on a new UTC day, allowing normal polling to resume automatically.

Update through HACS and restart Home Assistant.
