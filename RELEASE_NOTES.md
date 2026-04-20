## What's Changed

**FoxESS Force Discharge: Precise Export Targeting**
Force discharge on FoxESS H3-Pro and H3-Smart now uses Grid CT target mode (`REMOTE_CONTROL_GRID`) instead of AC output target mode. In AC mode, the inverter commanded battery output directly — when solar was also generating, the combined feed-in exceeded the target setpoint, making export imprecise during tariff events (e.g. GloBird bonus export windows). Grid mode instructs the inverter to hold total grid export at exactly the requested power, automatically reducing battery discharge to account for active PV generation. Force charge continues to use AC mode, since Grid mode would have the EV's house load reduce the battery charge rate.

**Load Forecast Sensors Showing ~0 kWh (Fix)**
The Load Forecast Today (Remaining) and Load Forecast Tomorrow sensors introduced in v2.12.80 were displaying near-zero values (e.g. 0.01 kWh instead of ~6 kWh). The internal forecast values are stored in kW after conversion from the raw watt output, but the summarise function was dividing by 1000 a second time — a 1000× underread. All three affected calculations are corrected; hourly breakdown attributes and the peak kW figure are also fixed.

*Update available via HACS*
