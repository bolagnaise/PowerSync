<!-- release: v2.12.1842 -->

## Tesla EV headroom recovery explains a "not stopped" refusal

When **Recover EV charging headroom** refused with `vehicle_not_stopped`, it did not say why. The result now includes `vehicle_report`, which shows the charging state, charger current and charger power that the car reported, so you can see which value stopped recovery. Recovery still releases the reservation only after the car reports that it is stopped and drawing no current or power.

Update through HACS and restart Home Assistant.
