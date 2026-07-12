<!-- release: v2.12.828 -->

## What's Changed

**Correct Fronius GEN24 load-following limits**
Fronius model names such as `Primo GEN24 10.0 Plus` were being read as 24 kW because the capacity detector mistook the GEN24 family name for the inverter rating. Load-following curtailment now uses the actual rating after GEN24, so a 10 kW inverter receives the intended percentage limit instead of being over-curtailed and importing from the grid.

Update available via HACS
