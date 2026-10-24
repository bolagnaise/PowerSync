<!-- release: v2.12.1493 -->

## What's Changed

**Use the live Octopus price in charging plans**
When PowerSync reads rates from BottlecapDave's public entities, an older daily rate could replace the live price and make a charge window appear cheaper than it was. Charging plans and their price labels now use the live rate for its stated interval, while keeping later tariff prices intact.

Update available via HACS
