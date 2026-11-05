<!-- release: v2.12.1633 -->

## Tesla Price-Level charging status

Price-Level charging now shows a pending start and its recovery reason when Tesla transport recovery is holding the session, instead of reporting that charging has started.

This corrects status reporting only. Uncertain commands remain on hold, and Stop remains available. Update through HACS and restart Home Assistant; leave recovery storage unchanged.
