# SolarEdge AC telemetry

The **AC Inverter** dashboard card can show the combined AC output of a SolarEdge leader and its followers. In the PowerSync integration's **AC Inverter** configuration, choose SolarEdge and select **SolarEdge AC power sensors (optional)**. Select each inverter's local AC power sensor from your upstream Home Assistant integration, including the leader once.

Only select independent inverters at the same site. Do not include a site total alongside its individual inverters, or select DC, battery or meter power. Repeated entity IDs count once. PowerSync converts W and kW readings to watts and uses the selected list as the entire card total; it does not add the configured Modbus inverter a second time. Leaving the list empty preserves the existing single-inverter reading.

Every selected sensor must have a finite reading in W or kW, reported within the last two minutes. A fresh report of an unchanged value is accepted. Missing, unavailable, invalid or older readings withhold the total and show **Telemetry incomplete**. The Inverter Status sensor's `ac_power_members` attribute identifies each selected sensor, its reading, timestamp and any error. A fresh complete set restores the total on the next status poll, normally within 30 seconds.

This selection changes the card's power telemetry only. Raw AC and DC attributes still describe the configured inverter. Battery telemetry, optimizer inputs and inverter control targets retain their existing scope. The combined reading does not establish that every follower received a curtailment command.
