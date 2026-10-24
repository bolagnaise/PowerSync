<!-- release: v2.12.1491 -->

## What's Changed

**Choose how Tesla batteries are controlled**
Tesla systems now have a Preferred battery control selector in Home Assistant, with Tariff as the default for existing and new installations. Choose Local V1R for fast local control within 10 kW or a lower gateway limit, or Automatic to choose charge and discharge methods from home load, solar and site limits.

**Safer changes between control methods**
Changing the preference stops the current force action and restores normal settings before saving. The optimiser then replans; stopped manual actions are not restarted. Explicit kW targets remain local and do not silently become unrestricted tariff actions.

Update available via HACS
