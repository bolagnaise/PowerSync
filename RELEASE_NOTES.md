<!-- release: v2.12.1751 -->

## Battery efficiency learning at full charge

Charging to 100% could erase a cycle before PowerSync could measure its efficiency. The learner now keeps counting energy through full charge and can use the cycle when the battery settles back at its starting charge level.

Update through HACS and restart Home Assistant. Learning uses new cycles from normal operation.
