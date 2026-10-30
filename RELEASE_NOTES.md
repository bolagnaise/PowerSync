<!-- release: v2.12.1553 -->
## Tesla Grid Charging across firmware versions

Removes the unnecessary firmware restriction introduced in v2.12.1552. When Grid Charging is On, Tesla's native configuration normally omits the false-valued disallow flag; PowerSync now handles that default across firmware versions.

The correction covers manual Local V1R, native Max, scheduled charging and permission restoration. Update through HACS and restart Home Assistant before retrying.
