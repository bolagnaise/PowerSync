<!-- release: v2.12.1800 -->

## Smart Schedule charging after Solar Surplus

When Smart Schedule takes over a Tesla that was waiting for surplus solar, PowerSync can now start the handover without sending an extra Stop command. It checks fresh vehicle readings first. If charging is active or the readings are uncertain, it keeps the existing Stop and confirmation checks.

Update through HACS and restart Home Assistant.
