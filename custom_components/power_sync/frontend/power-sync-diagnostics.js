// Admin-only, independent ongoing diagnostic and contribution consent.
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
    this.node('p', 'Approve Codex and Claude separately. Diagnostic access is read-only. Helping test optimizer updates requires a separate approval. Captures are replayed, then deleted; bounded summaries expire after seven days.', card);
    try {
      const state = await this.request();
      if (state.identity) this.node('pre', JSON.stringify(state.identity, null, 2), card);
      this.button(card, 'Create setup identity', async () => { await this.request({action: 'identity'}); this.load(); });
      const code = this.node('input', '', card); code.placeholder = 'Invitation code'; code.autocomplete = 'off'; code.type = 'password';
      const key = this.node('input', '', card); key.placeholder = 'Operator-provided authority public key'; key.autocomplete = 'off';
      this.button(card, 'Review invitation', async () => {
        const review = await this.request({action: 'review', code: code.value, authority_key: key.value}); code.value = '';
        const area = this.node('section', '', card);
        this.node('h3', 'Review this exact access', area);
        this.node('p', review.descriptor.disclosure, area);
        const scope = review.descriptor.ceiling;
        this.node('p', `Client: ${scope.processor}`, area);
        this.node('p', scope.kind === 'ha_read' ? 'Purpose: ongoing HA diagnostic reads' : 'Purpose: fresh optimizer replay and deletion', area);
        this.node('p', `Registered operator: ${scope.actor}`, area);
        this.button(area, 'Approve this access', async () => { await this.request({action: 'confirm', challenge: review.challenge}); this.load(); });
      });
      for (const grant of state.grants) {
        const row = this.node('section', '', card);
        this.node('p', `${grant.ceiling.processor} · ${grant.ceiling.kind === 'ha_read' ? 'HA diagnostic reads' : 'Optimizer contribution'} · ${grant.status}`, row);
        if (grant.activity?.last_capture_sent_at) this.node('p', `Last encrypted capture sent: ${new Date(grant.activity.last_capture_sent_at * 1000).toLocaleString()} · ${grant.activity.capture_bytes} bytes · ${grant.activity.capture_ms} ms capture overhead`, row);
        this.button(row, 'Revoke this grant', async () => { await this.request({action: 'revoke', grant: grant.id}); this.load(); });
      }
      this.node('p', state.root_revocation || state.connection || '', card);
      if (state.identity) this.button(card, 'Revoke all installation access', async () => { await this.request({action: 'revoke'}); this.load(); });
    } catch { this.node('p', 'Diagnostics is unavailable. Reload to retry.', card); }
  }
  button(parent, text, action) { const b = this.node('button', text, parent); b.onclick = async () => { b.disabled = true; try { await action(); } catch { this.node('p', 'Request could not be confirmed.', parent); } finally { b.disabled = false; } }; }
}
if (!customElements.get('ps-diagnostics-card')) customElements.define('ps-diagnostics-card', PowerSyncDiagnostics);
