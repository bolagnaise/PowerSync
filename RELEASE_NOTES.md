<!-- release: v2.12.1869 -->

## Smart Schedule uses free import periods at full rate

During a free (0¢) import period, Smart Schedule could charge the car only from spare solar when its plan had marked that hour as a solar hour. The car then charged slowly, for example at 2 kW with almost no grid import, while free grid power went unused. This mostly affected Prefer Solar and Solar Only.

Smart Schedule now uses a free import period in every strategy, charging up to your charger limit with whatever site import headroom remains after the home battery. If **Limit grid import** is on, or grid charging is blocked during a demand window, it still charges from solar only. Update through HACS and restart Home Assistant.
