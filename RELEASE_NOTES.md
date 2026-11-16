<!-- release: v2.12.1724 -->

## Keep FoxESS solar export holds active

Profit Max solar export holds could end at the next planning cycle after PowerSync read back the zero charge limit it had applied. FoxESS Modbus and entity connections now recognise their own verified hold, allowing it to continue while the plan still calls for solar export.

Missing or invalid charge-limit readings still prevent a hold, and the previous charge limit is restored when the hold ends. Update through HACS and restart Home Assistant.
