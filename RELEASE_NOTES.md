<!-- release: v2.12.1826 -->

## Premium evening export restored after a free midday charge

When a plan was made while the battery was below your export floor, PowerSync could still drop the evening premium export, even though the free midday import window refills the battery first. This affected free-import plans such as CovaU SolarMax. It also happened when PowerSync priced stored energy at your normal import rate. Plans now count the free charge at its real cost and schedule the evening export when it pays. Forced export still stops at your export floor. Update through HACS and restart Home Assistant.
