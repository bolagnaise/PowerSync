<!-- release: v2.12.1272 -->

## What's Changed

**SolarEdge curtailment permission changes no longer create a false control block**
If automatic curtailment loses permission before its inverter call, PowerSync now records a rejected request without marking the write as uncertain. Previously, disabling permission during a queued curtail or restore request could leave SolarEdge control requiring reconciliation even though the inverter was never called. The permission check runs under the shared control lock, after the control journal is saved, so a settings change during that save is also handled correctly.

**Uncertain writes retain their recovery safeguards**
Failed or uncertain inverter calls still block further control and require supervised recovery. This update does not clear an existing reconciliation block or replay a previous command. Battery Integration Details distinguishes the rejected request with `possibly_transmitted: false`. The SolarEdge recovery guide also explains how to find the PowerSync configuration entry ID.

Update available via HACS
