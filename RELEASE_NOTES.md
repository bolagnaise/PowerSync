<!-- release: v2.12.1318 -->

**OpenRouter reasoning-model compatibility**
AI plan explanations no longer require the optional temperature parameter on
OpenRouter. Reasoning routes that support structured output but not temperature
can now receive the request. Strict JSON output, response limits and provider
capability requirements remain in place. Model-only saves preserve the stored key.

**Tesla VINs masked in action-discovery logs**
The action logger now masks VINs at the emitting logger, including discovery
messages, device names, mismatch diagnostics and nested formatting arguments.
This closes the gap where a parent logger's redaction filter did not apply to
child logger messages. Reloads do not add duplicate filters.

**DC Solar distinguishes command acknowledgement from export confirmation**
The dashboard no longer says export has stopped solely because a curtailment
command was acknowledged. Command-only routes show that export is unverified.
The confirmed label requires the sensor's explicit physical-effect confirmation
and describes export below the supported threshold rather than exactly zero.
This corrects the status claim; it does not change inverter controls or establish
why an individual system continued exporting.

Verified with 453 focused and adjacent tests, including regressions that fail
before each correction. Customer installation and physical retests remain separate.
