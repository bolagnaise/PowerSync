<!-- release: v2.12.1823 -->

## ZeroHero Super Export no longer skipped after a low-battery morning

If your battery started the day below your reserve, the optimizer could drop the evening Super Export (or other paid export window) completely, even though the free midday window would refill the battery well before the export started. PowerSync now counts that free charging when deciding whether the battery can get back above your reserve in time, so the evening export is planned as it would be on a day that started above the reserve. Forced export still stops at your reserve. Update through HACS and restart Home Assistant.
