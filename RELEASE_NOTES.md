<!-- release: v2.12.1690 -->

## Ongoing MCP diagnostics with one approval

Home Assistant owners can now approve ongoing read-only MCP diagnostics for support and private offline optimizer testing together. Access stays enabled across restarts until revoked, and revoking the combined approval stops both uses. Raw test captures are replayed and deleted.

Existing read-only approvals keep their original scope; review a combined invitation to include offline testing. Participation still requires operator setup and owner approval, and remains disabled after updating alone. Update through HACS, restart Home Assistant and reload the dashboard.
