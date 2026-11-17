<!-- release: v2.12.1734 -->

## Preserve battery energy for peak household use

Smart Optimization now keeps a planned battery hold when the charge level is just above the optimiser reserve and the energy is needed for household use at a later, higher electricity price. Previously, execution could switch that hold to self-consumption and use the saved energy early. This includes Tesla Tariff and Local V1R control; Disable Idle, demand protection and Monitoring Mode still take precedence.

Update through HACS and restart Home Assistant. There is no need to lower your reserve setting for this fix.
