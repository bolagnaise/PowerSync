<!-- release: v2.12.1639 -->

## Smoother Solcast plan refreshes

PowerSync now processes Solcast's full cached forecast in the background when refreshing a plan, including after a manual force command. This removes one source of brief interface and automation delays on smaller Home Assistant hosts.

Update through HACS and restart Home Assistant. No forecast settings need to change.
