<!-- release: v2.12.1726 -->

## Keep ongoing support connected after interruptions

A temporary diagnostic connection failure could leave ongoing support unable to reconnect. PowerSync now waits for the previous connection to finish before reconnecting, checks that connection state was saved successfully, and lets other approved connections continue independently.

Update through HACS and restart Home Assistant. If support has already confirmed that an authorization is inactive, review a fresh invitation in HA; updating alone cannot reactivate that permission. This change does not clear retained EV recovery history.
