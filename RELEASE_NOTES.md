<!-- release: v2.12.1825 -->

## Optimiser settings now say what they do

The optimiser settings card on the PowerSync dashboard opens with a status line that says what the optimiser is doing right now: off, planning only in Monitoring mode, or controlling the battery. Below it are Smart Optimization and Monitoring only. Profit Max and Cost Neutral used to be two separate switches even though only one can be on at a time. They are now a single **Battery goal** choice of Standard, Profit Max or Cost Neutral, and each option has a short description. The selected goal shows what is holding it back, such as a Profit Max solar-export blocker, or today's Cost Neutral progress. Charge by time shows its target, and the remaining switches are grouped under Fine tuning and Updates.

Smart Optimization is greyed out only when turning it on would be refused, for example when HA optimiser access has expired, been revoked or is unpaired, or while controls are being restored. The card names the fix, and you can always turn Smart Optimization off. On a read-only Sungrow iHomeManager connection, Monitoring only is shown as locked on, because no battery commands can be sent. The car charging inputs in dashboard settings are hidden until a vehicle or charger is set up. Standalone OCPP and Zaptec chargers count.

Plan chips, the Right now pill, the classic optimiser card and the integration options now also say when Monitoring mode means the optimiser only plans. Your entities, services, automations and saved dashboard layouts are unchanged. Update through HACS, restart Home Assistant, then refresh the dashboard.
