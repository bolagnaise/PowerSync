SolarEdge control now stops after an uncertain write, retains the prior settings, and blocks further commands until supervised reconciliation. The block survives restart. Battery operations and curtailment writes share a lock, and stale timers cannot restore a later command.

Dispatch validates required controls before writing. Restore changes only fields PowerSync changed; a self-consumption baseline with a non-zero command timeout remains valid. Failed controls now reach service callers and optimizer status instead of being recorded as successful.

See `docs/wiki/SolarEdge-Control-Recovery.md` for the read-only recovery service and its limits.
