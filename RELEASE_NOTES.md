<!-- release: v2.12.1545 -->

## Powerwall manual controls

Manual battery commands could wait indefinitely when the optimizer was sending a command at the same time. Manual controls now wait for that command to finish, then take control normally. Update through HACS and restart Home Assistant.
