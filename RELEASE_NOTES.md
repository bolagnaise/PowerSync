<!-- release: v2.12.1519 -->
## Battery export cost tracking

Repeated optimiser updates using the same battery reading no longer turn known solar energy into unknown energy during charging or discharge. This prevents an artificially increased stored-energy cost from blocking otherwise eligible battery export.

Update through HACS and restart Home Assistant. Previously stored unknown energy is retained, and missing measurements or long telemetry gaps still receive a conservative valuation.
