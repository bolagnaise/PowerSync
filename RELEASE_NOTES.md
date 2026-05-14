<!-- release: v2.12.406 -->

## What's Changed

**Spread battery export across eligible windows**
Smart Optimization now includes an opt-in Spread Export Across Window mode for batteries that support target export power. When enabled, PowerSync keeps the LP optimizer's planned battery-export energy but distributes it evenly across each eligible export window instead of dispatching the battery at maximum discharge power for a shorter burst.

**Works in normal Smart Optimization and Profit Max**
The new mode applies after the LP schedule is produced, so it works with both normal Smart Optimization and Profit Max. Profit Max still controls how willing the optimizer is to spend stored energy and which export windows are attractive; Spread Export only reshapes the planned export energy and does not create extra export.

**Capability-gated control and API support**
The setting is available in setup/options, the Home Assistant config switch, and the optimization API. It is shown only for target-power-capable batteries including GoodWe, Sigenergy, Sungrow, FoxESS, AlphaESS, Solax, Fronius Reserva, and Neovolt / Bytewatt. Tesla, ESY Sunhome, and SAJ H2/HS2 keep the existing max-rate export behaviour.

Update available via HACS
