# Storm Watch notifications

When Tesla reports that Storm Watch is active, PowerSync creates a Home Assistant
notification and sends a push to registered PowerSync mobile devices. The message
explains that storm protection may charge the Powerwall from the grid or pause
normal battery discharge.

This uses Tesla's current Fleet/Teslemetry status. Turning on the Storm Watch
setting alone does not send an alert. Local gateway readings without cloud Storm
Watch status cannot establish activation.

PowerSync alerts once when activation is observed, including when the first
reading after Home Assistant starts is already active. Repeated readings and
integration reloads do not repeat the notification. Missing or invalid readings
do not clear or rearm it. When Tesla explicitly reports protection inactive,
PowerSync dismisses the Home Assistant notification and can alert on the next
activation. A Home Assistant restart can alert again if protection is still active.

The notification reports Tesla's status. It does not change Storm Watch, battery
settings or the optimiser plan, and does not confirm battery power flow. Check
Battery Power and Grid Power for measured charging or discharging.

Update through HACS and restart Home Assistant to receive these notifications.
