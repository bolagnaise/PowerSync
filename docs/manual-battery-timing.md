# Manual charge and discharge timing

In **Battery controls**, enable **Custom charge/discharge time**. Choose **Run for** and type the hours and minutes, or choose **Until** and enter a finish time such as `18:30`. Both **Charge** and **Discharge** use this timing. The duration + and − buttons change it by one minute. Tap your action twice to confirm; changing timing or power requires confirmation again.

Quick duration buttons return Charge and Discharge to preset timing. **Hold charge** and **Power the home** continue to use the preset Run for duration. Custom duration values initialize from the existing discharge Custom selection; existing entity IDs and saved discharge settings remain compatible.

Until uses the timezone configured in Home Assistant, even when your browser uses another timezone. The preview says today or tomorrow. It selects the next real occurrence of that time: a skipped daylight-saving time is skipped, and a repeated time uses its next occurrence. The remaining duration rounds up to whole minutes, so the timer can finish less than one minute after the entered time. A next occurrence beyond 24 elapsed hours is rejected.

## Service calls

Both `power_sync.force_charge` and `power_sync.force_discharge` accept `duration` as a whole number of minutes from 1 to 1440, or `until` as an `HH:mm` string. Supply only one timing field. Omitting both retains the 30-minute default.

```yaml
action: power_sync.force_charge
data:
  duration: 77
```

```yaml
action: power_sync.force_discharge
data:
  until: "18:30"
```

Existing `duration: Custom` discharge automations still resolve the saved custom duration. The same value is accepted for charge. Provider restrictions still apply: Amber overrides support at most 240 minutes; FoxESS supports at most 1092 minutes; SolaX and Neovolt must support the requested native duration. SolaX native charge schedules cannot represent an exact 24-hour window. Unsupported requests fail before taking ownership or changing an existing timer.

The classic dashboard provides an editable custom duration and finish-at controls too. A request or countdown confirms command handling; fresh battery/grid telemetry is needed to establish the physical result. Safety limits, power limits, and restoration rules still apply.
