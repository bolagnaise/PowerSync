<!-- release: v2.12.1159 -->

## Profit Max solar export switched itself off a minute after switching itself on

**Update if you have Profit Max enabled and have watched a Solar Export block appear in the Action Plan and vanish again a few minutes later — especially on a Fronius GEN24 or Neovolt system, where the battery's storage control mode flipped back and forth for the whole window.**

When Profit Max decides your solar is worth more sold than stored, it does not just label the slot — it holds the battery's charging at zero so the solar has nowhere to go except out to the grid. On a Fronius GEN24 that means writing `Block Charging` to the storage control mode; on Neovolt it means `No Battery Charge`. PowerSync applies that hold, reads the value back to confirm the inverter took it, and only then reports the block as active.

The next optimization cycle then threw it away.

### The hold was reported as evidence against itself

Before planning any solar export, PowerSync asks a simple question: *can this site hold charge at zero right now?* If the answer is no, it refuses solar export outright rather than promising something the hardware cannot deliver. That check is deliberately fail-closed, and it stays that way.

Two parts of it were answering "no" for a hold that was working perfectly.

The first treated *any* hold in progress as cleanup in flight, including a hold that had already applied and verified. The second was the hardware readback itself: it required the storage mode to read `Auto` (or `Normal` on Neovolt), and the mode it actually read was `Block Charging` — the value PowerSync had written a moment earlier and already confirmed.

So the strongest possible proof that a site *can* hold charge at zero was being read as proof that it cannot.

### What that looked like on your system

The refusal is not limited to the current slot. An unsupported answer removes solar export from **every** slot in the 48-hour plan at once, so the whole plan collapsed to self-consumption. Seeing no solar export left in it, PowerSync then did the correct thing for that plan and cleared the hold — restoring `Auto`.

With the hold gone, the readback was `Auto` again, the check passed, solar export came back, and the hold was re-applied. On one reported Fronius GEN24 site this cycled continuously: `Block Charging` and `Auto` written to the inverter in turn, every few minutes, for the entire export window, with `sensor.power_sync_optimization_status` alternating between a healthy `supported` with 26–38 planned slots and `cleanup_pending` with zero.

Nothing was broken in the inverter, and every command was accepted — the plan was being rebuilt from a wrong answer each cycle.

### What changed

A hold that this PowerSync entry owns and has verified is now recognised as its own baseline. The mode readback accepts the hold value it wrote itself, and an active verified hold no longer reports as cleanup in progress.

The fail-closed behaviour that protects other owners is unchanged, and this is the part deliberately kept narrow:

- A **foreign** mode still refuses. If the storage control mode reads `Manual`, `Charge from Grid`, `Discharge to Grid` or anything else PowerSync did not put there, solar export is still declined — PowerSync will not take a control that something or someone else is holding.
- `Block Charging` set **by you**, with no PowerSync hold behind it, is still a foreign mode and still refuses. Nothing changed for the manual workaround.
- Cleanup genuinely in progress still blocks planning, as does an unloaded upstream integration, a missing control entity or an unreadable mode.

**What you will notice:** a Solar Export block that PowerSync plans should now stay for as long as the economics hold, instead of dropping out on the next cycle — and your inverter's storage control mode should settle on one value for the duration of the window rather than being rewritten every few minutes.
