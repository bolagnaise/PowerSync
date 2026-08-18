<!-- release: v2.12.1137 -->

## What's Changed

**CovaU free windows now honor solar-offset Charge By Time targets**
PowerSync now calculates quota usage and site grid import from net import—household load plus battery charging minus concurrent solar—when selecting CovaU free-window charge slots. Fixed-rate and target-power battery profiles no longer lose safe free slots merely because forecast solar was omitted from that calculation, allowing an enabled Charge By Time target to use the available free window while still respecting the remaining daily quota and configured site-import cap.

Update available via HACS
