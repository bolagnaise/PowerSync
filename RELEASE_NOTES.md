<!-- release: v2.12.1674 -->

## More accurate charging-source tracking

Battery SOC readings that advance in uneven steps could gradually move measured charging energy into unknown inventory, affecting the stored-energy cost used by optimization. Continuous charging now retains its measured sources across those steps.

Previously accumulated unknown energy is retained. Update through HACS and restart Home Assistant.
