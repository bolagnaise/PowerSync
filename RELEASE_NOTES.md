<!-- release: v2.12.902 -->

## What's Changed

**Tesla grid-charging commands now require confirmed state**
PowerSync now distinguishes Tesla accepting a grid-charging command from the Powerwall actually applying it. Cloud commands use bounded, uncached `site_info` readback without repeating an accepted write, while paired Powerwall V1R control confirms the real local configuration before reporting success or falling back to cloud control. Manual controls, PowerSync automations, demand-period enforcement, and charge-kick recovery now share the same confirmation contract.

**Force charge and discharge fail safely during Tesla API incidents**
Tesla force charge will no longer continue with reserve and tariff changes when grid charging did not verify, and force discharge will not upload its high-export tariff until grid charging is confirmed disabled. Failed setup triggers cleanup, failed restore keeps the saved state armed for retry, and stale confirmations cannot overwrite a newer force command. The optimizer also retains its previous action marker so a later cycle can retry instead of recording an unconfirmed force charge as executed.

**Delayed Tesla tariff readback is handled without duplicate uploads**
Force tariffs now use a longer bounded eventual-consistency window with direct readback, transient-error tolerance, and deadline-safe polling. PowerSync waits for Tesla propagation after an accepted upload without sending duplicate tariff commands.

Update available via HACS
