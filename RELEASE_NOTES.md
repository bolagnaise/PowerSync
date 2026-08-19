<!-- release: v2.12.1152 -->

## Manual EV charging now has a controller, like every other charging mode

Manual charging was the last mode PowerSync started and then stopped watching. It now runs under the same controller as Smart Schedule, Solar Surplus, Boost, and the rest.

### What was happening

When you started charging manually, PowerSync recorded the session and set a rate — and that was the end of it. There was no periodic control loop attached, so the session held whatever current the charger happened to be left on until something else intervened.

For most people that was invisible. It mattered in two places:

- **With per-phase load management enabled** (added in 2.12.1148), a manual session occupied part of your phase budget that PowerSync had no way to reclaim. Another car starting later saw less headroom than actually existed, and the manual car itself could never be turned down when the house got busy.
- **After a restart**, a restored manual session was re-recorded with no controller, so nothing was tracking it.

### What changed

Manual starts take the same path as everything else. The rate you asked for is unchanged, but the session is now controller-managed: it re-applies that rate through the same command wrapper every other mode uses, which is where the per-phase clamp lives. With load management on, a manual car is turned down when the site is busy and brought back when headroom returns. With it off, the controller simply holds the rate.

The phase-management setting now controls only the *clamp*, not which code path runs. That removes the last place where manual charging behaved differently from every other mode.

### Rate is no longer rounded up to maximum

A manual session that didn't carry an explicit current used to fall back to your configured maximum. That was wrong in two cases this release fixes: a manual start on a car that was **already charging**, and a session **restored after a restart**. Both now adopt the rate the charger is actually delivering. Only a genuinely idle charger with no requested rate falls back to the maximum.

### What deliberately did not change

Taking over an already-charging session still sends **no command to the charger**. PowerSync adopts the session silently, exactly as before — it only reconciles the charger when there is a phase budget to enforce. If you use manual charging without load management, nothing about the physical behaviour of a takeover changes.

Manual sessions also keep their own identity in status payloads and the mobile app; they are not relabelled as a different mode.

One visible difference: the ownership record for a manual start now reads `start_manual` instead of `start`, matching how every other mode records itself. This is a diagnostic label only.
