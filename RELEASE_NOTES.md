<!-- release: v2.12.1843 -->

## Clearer logs when Tesla rejects a tariff

Sometimes Tesla rejects a tariff upload with only `Invalid tariff provided`. That can happen when a Powerwall restore puts your saved tariff back. The Home Assistant log now has a second line, starting `Rejected Tesla tariff structure`. It lists the tariff's seasons, time periods and rate names, and flags periods with no rate, rates with no period and empty seasons. The line contains no prices, and upload and restore work exactly as before. This makes it possible to see which part Tesla refused.

Update through HACS and restart Home Assistant.
