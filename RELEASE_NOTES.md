<!-- release: v2.12.1321 -->

## What's Changed

**Powerwall local pairing: complete cleanup when Home Assistant has already removed a listener**

Reloading or unpairing a locally paired Powerwall could raise `KeyError` when
Home Assistant had already removed the local polling listener. That exception
stopped the remaining background-task cleanup and could interrupt unpairing
before its runtime references were cleared.

Local shutdown now tolerates that already-removed listener and continues
cancelling notification and diagnostic tasks. Repeated and concurrent shutdown
and the unpair runtime-clear path are covered by regression tests.

This corrects a reproduced cleanup failure. It does not establish a fix for
persistent Unknown grid-state entities after polling has recovered. Unknown or
transitional gateway states still require a recognised live readback; this
release does not infer a grid connection from a command acknowledgement.

Validation: 125 focused and adjacent Powerwall tests passed, including the
reported removed-listener variant, with independent patch review. No customer
installation or physical Powerwall behavior has been verified.

Update available via HACS
