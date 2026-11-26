<!-- release: v2.12.1847 -->

## Profit Max solar export now works on Sungrow inverters that reject a 0 W charge limit

On some Sungrow inverters, every Profit Max solar-export hold failed with `Modbus write error at register 33046` and `Failed to set charge rate limit`, then fell back to normal self-consumption. These inverters reject a 0 W charge limit but accept their 10 W minimum. So Profit Max could never hold battery charging to export solar at a good feed-in price and refill the battery from cheaper solar later.

PowerSync now uses the 10 W minimum when 0 W is rejected, and uses it directly for later holds. A 10 W reading is treated as PowerSync's own hold, so the normal charge limit is still restored when the hold ends.

Profit Max solar export also needs **Maximum grid export** set under Smart Optimization → Grid & site constraints. Update through HACS and restart Home Assistant.
