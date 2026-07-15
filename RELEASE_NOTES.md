<!-- release: v2.12.856 -->

## What's Changed

**Optimizer status now matches the active five-minute slot**
The Current Action status now updates after PowerSync applies each cached or freshly solved battery command, so same-mode charge and export power changes no longer display the previous slot's target. If a hardware write fails, the newly computed plan still remains visible for diagnosis.

Update available via HACS
