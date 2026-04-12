## What's Changed

**New: Off-grid control via Tesla device_command**
PowerSync can now send the exact same off-grid/reconnect commands the Tesla mobile app uses — captured via mitmproxy from a live PW3 session. The off-grid button sends a base64 protobuf payload via `POST /device_command` through the PowerSync cloud proxy, which Tesla's Powergate service relays to the gateway. Confirmed working on PW3 firmware 26.2.1: `island_status: off_grid_intentional`, `grid_status: Inactive`, solar stays on, no gateway reboot. Reconnect uses a different protobuf payload also captured from the Tesla app.

**New: Curtailment via local config.json write (backup mode)**
For automated curtailment (negative pricing, demand charge windows), PowerSync now writes `default_real_mode: backup` + `backup_reserve_percent: 100` directly into the gateway's config.json via the TEDAPI v1r transport. No contactor cycling, no inverter restart, no solar dropout — the gateway applies the config within ~90 seconds and stops all grid export. On restore, the user's original operation mode and backup reserve are written back exactly as they were. This mechanism is specifically designed for frequent toggling (5-minute Amber pricing cycles) where physical islanding would cause unacceptable solar production loss.

**New: Three-screen Battery Setup in mobile app**
Battery Setup is no longer one long scrolling page. It's now a menu hub with three dedicated sub-screens: Gateway Connection (WiFi, password, IP), Local Control (pairing, off-grid, curtailment), and Battery Health (capacity scan, per-pack data). Each screen is self-contained with its own state management and pull-to-refresh.

**Fix: Customer password derived from WiFi password, not serial**
The Powerwall REST login at `/api/login/Basic` expects the last 5 characters of the gateway WiFi AP password — not the serial number as previously documented. The pairing flow now auto-derives the customer password from the WiFi password the user already provides for battery health scanning. Confirmed live: WiFi password `UPHHTLVBLH` → customer password `LVBLH` → 200 OK.

**Fix: Pairing always requires physical switch toggle**
Tesla's Fleet API sometimes auto-verifies a new RSA key with state=VERIFIED if the user toggled the DC isolator recently. But cloud-verified does not mean the gateway itself accepted the key — live testing showed "client authorization not verified" on the first TEDAPI command. The auto-verify shortcut is removed; the wizard always shows the toggle countdown.

**Fix: Full gateway DIN used for TEDAPI signing**
The TEDAPI v1r transport requires the full `{part_number}--{serial_number}` DIN for TLV signature personalization. The pairing flow was storing only the serial portion, causing MESSAGEFAULT_ERROR_WRONG_PERSONALIZATION on signed commands. Now always fetches the full DIN from Tesla's products endpoint.

Update available via HACS
