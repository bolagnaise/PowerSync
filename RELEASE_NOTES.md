<!-- release: v2.12.302 -->

## What's Changed

**Weather setup no longer blocks on missing HA entities**
The Weather & solar forecast options screen now handles installs that do not expose any `weather.*` entities. Users can save an OpenWeatherMap API key and postcode without the Home Assistant entity selector failing with `Entity None is neither a valid entity ID nor a valid UUID`.

**Weather entity field is clearer when available**
When a Home Assistant weather entity does exist, the optional selector now has a proper label and description instead of showing the raw `weather_entity` key. Blank, `None`, and stale unset values are normalized before they are stored or rendered.

Update available via HACS
