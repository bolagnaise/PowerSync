<!-- release: v2.12.1532 -->

## Smart Optimization after a restart

Smart Optimization now recovers when startup battery restoration is cancelled, instead of silently stopping future planning until you toggle it off and on. Plan refreshes also keep the shared startup restoration running; pending force cleanup and control permissions still apply.

Update through HACS and restart Home Assistant, then check that a new plan appears without toggling Smart Optimization.
