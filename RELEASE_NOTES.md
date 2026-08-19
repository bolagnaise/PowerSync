<!-- release: v2.12.1158 -->

## A planned evening export window could disappear hours before it started

**Update if the Action Plan has shown you a battery export window — often the evening peak — that was still there one minute and gone the next, with the battery quietly sitting in self-consumption instead and no export command ever sent to your inverter.**

Before PowerSync will discharge your battery into the grid, it checks whether that stored energy is worth more to you later. If prices climb after the export window and your house is still drawing load, spending the battery now can mean buying the same kWh back dearer in a few hours. That check exists to stop exactly that, and it is doing real work on most sites.

It was asking the wrong question.

### The comparison used a price the battery was not paying

For each export slot, PowerSync walked the rest of the forecast and reserved stored energy for every later slot whose import price was higher than **the import price at the export slot itself**. But a battery that is exporting is not importing — it is not paying that price. The price it actually gives up by holding a kWh back is the feed-in value of the export it is refusing.

So a site with a 45 c/kWh evening feed-in and a 30 c/kWh import price would reserve energy for overnight load priced at 32 c/kWh, because 32 c beats the 30 c that was never going to be paid — while the 45 c/kWh export it was giving up did not enter the comparison at all. Holding that kWh was worth 32 c. Exporting it was worth 45 c.

### Why the whole window vanished at once, rather than shrinking

That comparison is a threshold: a later slot either counts in full or not at all. When a routine price refresh lifted a block of far-horizon slots past the threshold together, the amount reserved jumped from nothing to the entire usable capacity of the battery in a single solve.

PowerSync also caps total battery-to-grid across the whole plan at the energy already stored above the strongest protection floor. Once the reservation reached usable capacity, that cap became zero — every export slot in the plan lost its energy budget simultaneously, and the whole window was re-labelled `self_consumption`. In testing, a **0.06 c/kWh** move on import prices more than ten hours after the window removed a full 15 kW, 70-minute export window outright.

Nothing had gone wrong with your inverter when this happened, and no command was sent or cancelled — the window was removed at the planning stage, before there was anything to command. That is why the inverter simply stayed in its normal mode and the plan looked like it had changed its mind.

### What changed

The reservation now compares later load against **both** the export slot's feed-in value and its import price, and reserves only for load that beats them both. The threshold can only move upward, so the amount reserved can only shrink — this release cannot introduce a new export block anywhere.

The genuine case is untouched. Where a later import price really does exceed what you would be paid to export, PowerSync still reserves the energy and still holds the export at zero. Both the main solver and the fallback solver use the same rule.

**What you will notice:** evening and peak export windows that PowerSync has planned should now stay in the plan through ordinary price refreshes, instead of disappearing a few hours ahead. Where later prices genuinely beat your feed-in, you will still see the battery held back — and where a partial hold is the right answer, you now get a partial hold rather than the window being removed entirely.
