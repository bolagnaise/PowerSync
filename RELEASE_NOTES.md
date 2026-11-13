<!-- release: v2.12.1692 -->

## Ongoing MCP diagnostics

Ongoing diagnostic setup and reads now handle streamed replies reliably and identify their connections correctly to the service. This fixes connection failures during setup and diagnostic reads.

Initial enrollment is arranged through a support ticket. Once the owner approves ongoing access, it can support private offline testing of code changes and bug investigations with an active ticket. Approval stays enabled until revoked; raw test captures are replayed and deleted.

## Sigenergy EV solar charging

Solar surplus charging can now resume after price-level or scheduled charging stops on a Sigenergy charger. PowerSync now uses fresh stopped-charger readings to confirm the previous stop, while retaining checks that prevent taking over an externally controlled session.

Update through HACS, restart Home Assistant and reload the dashboard.
