<!-- release: v2.12.1559 -->

## Redesigned Home Assistant dashboard

The dashboard PowerSync automatically creates in Home Assistant now uses the approved redesign previewed in v2.12.1557, with the cloud app’s look in both Home Assistant light and dark themes.

**Right now** and **Optimizer plan** span the top of the dashboard. The plan combines 12 hours of measured history with the next 24 hours planned, with chart scrubbing, upcoming charge and export windows, and reserve details.

The remaining cards adapt to one, two or three columns. Use **Customize layout** to move or hide cards, show hidden cards again, or reset the layout. Layouts are saved in each browser for each column count; layouts customized on the previous dashboard start fresh.

Battery action buttons require a second tap to confirm. Custom discharge time applies only while the discharge duration is set to **Custom**. The `button-card` dependency is now needed only for Solar curtailment, AC inverter and Amber battery control cards. If the new dashboard module cannot load, PowerSync falls back to the previous layout.

Update through HACS and restart Home Assistant. Hard-refresh your browser if the new layout does not appear. See the [dashboard setup guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1559/docs/wiki/Dashboard-Setup.md) for details.
