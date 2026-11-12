# HA-wide read backend

`custom_components/power_sync/ha_read/reader.py` provides the default-off backend
and read-only MCP dispatcher for the broader diagnostic service. It is a library,
not a public HTTP endpoint or a replacement for authentication.

## Implemented backend

- Search and read current cached states across every HA entity domain, rather
  than PowerSync's seven semantic energy roles.
- Page large installations without serializing all attributes at once. Cursors
  bind actor, client, installation, consent epoch, query and entity inventory,
  and expire after 60 seconds.
- Require a caller-supplied, freshly verified full-HA read context before every
  operation and again before disclosure after awaited work. Verifier failure,
  expiry or revocation denies the read.
- Return original HA state timestamps, explicit per-page cache coherence and
  an untrusted-content marker. Cache reads do not refresh hardware.
- Project bounded JSON/text attributes, redact known credential fields and
  credential-bearing URLs, and avoid arbitrary object formatters.
- Publish only installed and granted tools, with closed argument schemas and
  read-only annotations. Mutation/service/restart/event-fire tool names are
  rejected by the dispatcher itself.
- Validate argument shapes, finite numeric bounds and the encoded request budget
  before any reader runs. Oversized integers and malformed values return the
  closed `invalid_query` error without invoking formatters or adapters.

The caller must construct `ReadContext` from a verified owner-approved grant.
Never accept that context or a verifier from request JSON, and never substitute
an old ticket/session grant. This context represents HA-wide operation scopes;
it does not implement entity-subset permissions. The existing support protocol
and its narrower field projection remain unchanged.

## Capability negotiation

`ha_get_capabilities` reports implemented readers, unavailable categories and
`complete_profile`. The backend currently includes cached state discovery and
state reads. Registry, Recorder history/statistics/logbook, automation/script
definitions and traces, integration configuration, dashboards, configuration
files, raw HA logs and installation/Supervisor readers have closed interface
contracts but still require their audited adapters. They are reported as
unavailable and are not published as callable tools without those adapters.

Do not describe this checkpoint as completed full HA access. It establishes
the all-domain state, permission and pagination boundary for that access.

## Remaining activation work

The new service still needs the separate ongoing grant authority, owner consent
UI, callback/policy/processor configuration, outbound transport and the remaining
read adapters. No MCP server registration, customer enrolment, bearer grant,
network route or existing session permission is enabled by importing this module.

Keep the read-only operation set fixed and versioned. Adapters must validate
their inputs, stay inside the approved data roots and source APIs, avoid device
refresh or configuration mutation, and preserve unavailable/redacted markers.
Broad diagnostics must remain separate from regression uploads and their
pseudonymous numeric replay contract.

## Validation

`tests/test_ha_read_backend.py` exercises 1,200 entities across four domains,
opaque cursor constraints, revocation during an awaited read, mutation-name
refusal, explicit missing capabilities, bounded text, known-secret redaction,
closed errors (including oversized numeric arguments) and avoiding unsafe
attribute formatters. These are isolated
backend tests, not an installation or connected-MCP receipt.
