<!-- release: v2.12.1530 -->
## Powerwall export permission readback

Native Max discharge can now use Tesla cloud to read the exact original export permission when V1R omits it. PowerSync keeps track of the source and confirms restoration through the same path. Missing or conflicting permission readings still prevent an unsafe start, and an unconfirmed cloud change remains pending across a restart.

Cloud confirmation also recognises a valid top-level export permission when the nested field is null. Grid-charging permission remains a separate requirement; this update does not assume that a missing permission is enabled.

Update through HACS and restart Home Assistant.
