// Admin-only ongoing diagnostic access, including private offline testing.
class PowerSyncDiagnostics extends HTMLElement {
  set hass(value) { this._hass = value; if (!this.ready) { this.ready = true; this.load(); } }
  setConfig() {}
  getCardSize() { return 5; }
  node(tag, text, parent = this) { const n = document.createElement(tag); n.textContent = text; parent.append(n); return n; }
  async request(value) { return this._hass.callApi(value ? 'POST' : 'GET', 'power_sync/diagnostics', value); }
  async load() {
    this.replaceChildren();
    if (!this._hass.user?.is_admin) return;
    const card = this.node('ha-card', ''); card.style.padding = '20px';
    this.node('h2', 'Ongoing HA diagnostics', card);
    this.node('p', 'Approve ongoing read-only access for support and private offline testing of optimizer updates. Approval stays enabled across restarts until you revoke it. Test captures are replayed, then deleted; bounded summaries expire after seven days. No settings or hardware controls can be changed through this access.', card);
    try {
      const state = await this.request();
      if (state.identity) this.node('pre', JSON.stringify(state.identity, null, 2), card);
      this.button(card, 'Create setup identity', async () => { await this.request({action: 'identity'}); this.load(); });
      const code = this.node('input', '', card); code.placeholder = 'Invitation code'; code.autocomplete = 'off'; code.type = 'password';
      const key = this.node('input', '', card); key.placeholder = 'Operator-provided authority public key'; key.autocomplete = 'off';
      this.button(card, 'Review invitation', async () => {
        const review = await this.request({action: 'review', code: code.value, authority_key: key.value}); code.value = '';
        const area = this.node('section', '', card);
        this.node('h3', review.combined ? 'Enable ongoing support and offline testing' : 'Review this existing invitation', area);
        const descriptors = review.descriptors || [review.descriptor];
        for (const descriptor of descriptors) this.node('p', descriptor.disclosure, area);
        const scope = descriptors[0].ceiling;
        this.node('p', `Connected application: ${scope.processor}`, area);
        this.node('p', review.combined ? 'Includes HA diagnostic reads for support and fresh optimizer captures for private offline replay. Both remain approved until revoked.' : scope.kind === 'ha_read' ? 'Purpose: ongoing HA diagnostic reads only' : 'Purpose: fresh optimizer replay and deletion only', area);
        this.node('p', `Registered operator: ${scope.actor}`, area);
        this.button(area, review.combined ? 'Enable ongoing access' : 'Approve this access', async () => { await this.request({action: 'confirm', challenge: review.challenge}); this.load(); });
      });
      const shown = new Set();
      for (const grant of state.grants) {
        const group = grant.approval_set || grant.id;
        if (shown.has(group)) continue;
        shown.add(group);
        const grants = state.grants.filter(item => grant.approval_set ? item.approval_set === group : item.id === group);
        const row = this.node('section', '', card);
        const purpose = grant.approval_set ? 'Support and offline testing' : grant.ceiling.kind === 'ha_read' ? 'HA diagnostic reads only' : 'Optimizer contribution only';
        const connected = grants.every(item => item.status === 'connected');
        const status = connected ? 'connected' : grants.some(item => item.status === 'revoking_locally_blocked') ? 'revoking · cleanup confirmation may be pending' : 'connecting';
        this.node('p', `${grant.ceiling.processor} · ${purpose} · ${status}`, row);
        for (const item of grants) {
          if (item.activity?.last_capture_sent_at) this.node('p', `Last encrypted capture sent: ${new Date(item.activity.last_capture_sent_at * 1000).toLocaleString()} · ${item.activity.capture_bytes} bytes · ${item.activity.capture_ms} ms capture overhead`, row);
        }
        this.button(row, grant.approval_set ? 'Revoke ongoing access' : 'Revoke this grant', async () => { await this.request({action: 'revoke', grant: grant.id}); this.load(); });
      }
      this.node('p', state.root_revocation || state.connection || '', card);
      if (state.identity) this.button(card, 'Revoke all installation access', async () => { await this.request({action: 'revoke'}); this.load(); });
    } catch { this.node('p', 'Diagnostics is unavailable. Reload to retry.', card); }
  }
  button(parent, text, action) { const b = this.node('button', text, parent); b.onclick = async () => { b.disabled = true; try { await action(); } catch { this.node('p', 'Request could not be confirmed.', parent); } finally { b.disabled = false; } }; }
}
if (!customElements.get('ps-diagnostics-card')) customElements.define('ps-diagnostics-card', PowerSyncDiagnostics);
