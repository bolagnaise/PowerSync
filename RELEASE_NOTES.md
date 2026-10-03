<!-- release: v2.12.1347 -->

## What's Changed

**Keep Cost Neutral actions aligned with native solar charging**

Cost Neutral periods now retain executable slot resolution and a mode-aware
solver witness, so natural self-consumption charging is modeled through
capacity saturation before the emitted action is settled. This fixes the
reported morning surplus mismatch without adding a new export control or
changing Profit Max behavior.

**Reconcile EV source reporting after export settlement**

EV source metadata now follows the final physical dispatch: battery export
clipped by the Cost Neutral settlement cap is not counted as EV supply. EV
delivery, source-policy permissions, reserve handling, and existing hardware
safety boundaries remain unchanged.

Update available via HACS
