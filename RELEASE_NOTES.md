<!-- release: v2.12.1664 -->

## More time to connect read-only support

Support invitations and pairing now allow 30 minutes, giving you more time to share the code and review the investigator and consent details. Your selected session deadline still applies, and diagnostics remain unavailable until you approve access in Home Assistant.

Update through HACS and restart Home Assistant to use the longer setup window.

## Power readings in support diagnostics

Read-only diagnostics now retain valid power readings that could previously appear unavailable when Home Assistant supplied their standard units. This improves the evidence shared with support; a Home Load sensor showing `unknown` still needs its own diagnosis.
