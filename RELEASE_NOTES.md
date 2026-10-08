<!-- release: v2.12.1360 -->

## What's Changed

**Retry failed FoxESS startup cleanup**
When Home Assistant restarted after a timed manual charge or discharge had
expired, a transient FoxESS restore failure could leave the expired control
marked active and suppress later optimizer actions indefinitely. PowerSync now
keeps that ownership fail-closed and retries the normal-operation restore with
bounded backoff, while cancelling the retry if a newer command or unload takes
over.

Update available via HACS
