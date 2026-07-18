<!-- release: v2.12.884 -->

## What's Changed

**Recognize safe CovaU quota baselines before the first daily window**

When CovaU SolarMax is selected before its first quota window, PowerSync now
recognizes that no eligible free-import or premium-export energy could already
have been used that tariff day. The configured cumulative meters can therefore
establish trustworthy balances immediately, allowing the 11:00–14:00 free
import and 18:00–21:00 premium export rates to appear in the forecast without
waiting for the next midnight reset.

PowerSync remains conservative after a relevant quota window has begun, or if
a meter reset, correction, telemetry gap, or estimated power source makes the
daily balance uncertain.

Update available via HACS
