<!-- release: v2.12.364 -->

## What's Changed

**Neovolt capacity entry fix**
Fixed the Neovolt setup validation that could show `neovolt_capacity_invalid` when entering comma-separated stack sizes such as `20.1, 30.2`. PowerSync now accepts comma-separated kWh values, optional `kWh` suffixes, and handles single-entry Neovolt setups by summing multiple stack capacities into the selected integration.

**Clearer capacity setup guidance**
Updated the Neovolt setup and Configure text to explain how stack capacities are applied for one or multiple selected Neovolt integrations, and added regression coverage for the parser cases reported by users.

Update available via HACS
