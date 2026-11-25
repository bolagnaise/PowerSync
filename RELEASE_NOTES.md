<!-- release: v2.12.1836 -->

## Clearer EV headroom recovery failures

When **Recover EV charging headroom** returns `fresh_readback_unavailable`, the result now says which check failed (`failed_check`) and in which reading (`round`). For a car reading, it also gives a short reason, such as a provider error, a timeout or a reading that was not new enough. This makes it possible to tell a sleeping car apart from a provider or meter problem. Recovery still keeps the reservation until fresh readings show the car has stopped, and nothing else about how it works has changed.

Update through HACS and restart Home Assistant.
