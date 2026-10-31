<!-- release: v2.12.1580 -->

## Custom charge and discharge timing

Battery controls now let you type a custom duration in hours and minutes for both Charge and Discharge. The + and − buttons change the duration by one minute, and charging uses the entered value instead of falling back to 30 minutes.

Choose Until to enter a finish time such as 18:30 in Home Assistant’s timezone. The dashboard shows when this means tomorrow and asks you to confirm again after changing time or power. Timing is limited to 24 hours and your battery provider’s supported range; Until rounds up to whole minutes. Update through HACS and restart Home Assistant, then refresh the dashboard.
