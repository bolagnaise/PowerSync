<!-- release: v2.12.1665 -->

## Better Cost Neutral export choices in longer plans

Cost Neutral now correctly favours higher-rate battery exports in full-day and two-day plans when they can cover the same daily costs using less stored energy. An earlier-slot preference could otherwise make the main planner sell extra battery energy at low rates.

Small low-rate exports can still be useful for solar headroom or when the higher-rate window cannot cover that day's costs. Update through HACS and restart Home Assistant.

## More time to connect read-only support

Support invitations and pairing now allow 30 minutes, giving you more time to share the code and review the investigator and consent details. Your selected session deadline still applies, and diagnostics remain unavailable until you approve access in Home Assistant.

Update through HACS and restart Home Assistant to use the longer setup window.

## Power readings in support diagnostics

Read-only diagnostics now retain valid power readings that could previously appear unavailable when Home Assistant supplied their standard units. This improves the evidence shared with support; a Home Load sensor showing `unknown` still needs its own diagnosis.
