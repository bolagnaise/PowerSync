/* HA-only support consent. No mobile settings, model calls or MCP credentials. */
class PowerSyncSupportCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }); this._entries = []; this._busy = false; }
  set hass(value) { this._hass = value; if (!this._loaded && value?.user?.is_admin) { this._loaded = true; this.load(); } }
  get hass() { return this._hass; }
  set config(value) { this._config = value; }
  async api(action, extra = {}) {
    return this._hass.callApi(action ? 'POST' : 'GET', 'power_sync/support', action ? { action, entry_id: this._entry, ...extra } : undefined);
  }
  async load() {
    if (this._busy) return;
    this._busy = true;
    try {
      const value = await this.api(); this._entries = value.entries || [];
      this._entry ||= this._entries[0]?.entry_id;
      this._status = this._entries.find(x => x.entry_id === this._entry)?.status;
    } catch { this._message = 'Support diagnostics are unavailable.'; }
    finally { this._busy = false; this.render(); }
  }
  node(tag, text, parent) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; parent?.append(e); return e; }
  async run(action, data = {}) {
    if (this._busy) return;
    this._busy = true;
    this.shadowRoot.querySelectorAll('button,select,input').forEach(e => { e.disabled = true; });
    try {
      const result = await this.api(action, data);
      if (action === 'bundle') {
        const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob), a = document.createElement('a');
        a.href = url; a.download = 'powersync-diagnostics.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else this._status = result;
      this._message = '';
    } catch (e) { this._message = e?.body?.error || 'Support request failed. Refresh the status before retrying.'; }
    finally { this._busy = false; await this.load(); }
  }
  render() {
    const root = this.shadowRoot; root.replaceChildren();
    const style = this.node('style', ':host{display:block;color:var(--primary-text-color,#eee);font:14px system-ui}section{padding:16px}h3{margin-top:0}label{display:block;margin:10px 0}button,select{font:inherit;padding:8px;margin:4px;border:1px solid #7898;background:transparent;color:inherit;border-radius:6px}p{line-height:1.5}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto}input{margin-right:8px}button{cursor:pointer}small{display:block;line-height:1.5}', root);
    const box = this.node('section', undefined, root);
    this.node('h3', 'Support', box);
    this.node('p', 'Share approved diagnostics with PowerSync support. Read-only access cannot change settings, control devices or provide a chat service.', box);
    if (!this._hass?.user?.is_admin) { this.node('p', 'Home Assistant administrator access is required.', box); return; }
    const entries = this.node('select', undefined, box); entries.setAttribute('aria-label', 'PowerSync installation');
    for (let i = 0; i < this._entries.length; i++) { const row = this._entries[i], o = this.node('option', `PowerSync installation ${i + 1}`, entries); o.value = row.entry_id; o.selected = row.entry_id === this._entry; }
    entries.onchange = () => { this._entry = entries.value; this._status = this._entries.find(x => x.entry_id === this._entry)?.status; this.render(); };
    const status = this._status || {}; this.node('p', `Access: ${status.state || 'closed'}`, box);
    if (this._message || status.error) this.node('p', this._message || status.error, box).setAttribute('role', 'status');
    const button = (text, action) => { const b = this.node('button', text, box); b.disabled = this._busy || !this._entry; b.onclick = action; return b; };
    button('Refresh status', () => this.load()); button('Download local diagnostics', () => this.run('bundle'));
    const checkbox = (label, checked = false) => { const l = this.node('label', undefined, box), c = this.node('input', undefined, l); c.type = 'checkbox'; c.checked = checked; this.node('span', label, l); return c; };
    const recording = checkbox('Record diagnostic history locally (up to 24 hours, subject to storage limits)', status.recording);
    const persistent = checkbox('Keep local history through restarts; HA backups may include these records', status.persistent);
    button('Save recording preference', () => this.run('recording', { recording: recording.checked, persistent: persistent.checked }));
    this.node('small', 'Local recording is separate from remote sharing. Stopping recording clears the retained support history. Existing HA Recorder data is managed by Home Assistant.', box);
    if (!['pending', 'activating', 'active'].includes(status.state)) {
      this.node('h4', 'Prepare a support session', box);
      const invitation = this.node('input', undefined, box); invitation.placeholder = 'Invitation from your private support ticket'; invitation.maxLength = 128; invitation.setAttribute('aria-label', 'Support invitation');
      const cats = {};
      for (const [category, label] of [['system','Versions and selected safe settings'],['entities','Energy readings and timestamps'],['optimizer','Optimizer plan and constraints'],['control_trace','Recorded control observations'],['logs','Structured diagnostic events'],['history','Selected diagnostic history']]) cats[category] = checkbox(label, ['system','entities'].includes(category));
      const duration = this.node('select', undefined, box); duration.setAttribute('aria-label', 'Support access duration');
      for (const [value,label] of [[3600,'1 hour'],[7200,'2 hours'],[28800,'8 hours']]) { const o = this.node('option',label,duration); o.value=String(value); }
      const history = this.node('select', undefined, box); history.setAttribute('aria-label', 'Historical sharing window');
      for (const [value,label] of [[0,'No past history'],[600,'Previous 10 minutes'],[3600,'Previous hour'],[86400,'Previous 24 hours']]) { const o=this.node('option',label,history);o.value=String(value); }
      const haRecorder = checkbox('Also allow approved historical readings already retained in HA Recorder');
      this.node('small', 'A pairing code prepares access only. You must review the investigator, ticket and processing policy and confirm below before any diagnostic query is permitted.', box);
      button('Prepare invitation', () => this.run('start', { invitation_token: invitation.value.trim(), categories: Object.keys(cats).filter(k => cats[k].checked), duration: Number(duration.value), history_seconds: Number(history.value), ha_recorder: haRecorder.checked }));
    }
    if (status.code) this.node('pre', `Pairing code: ${status.code}\nGive this code only to your investigator in your private support ticket. It expires after five minutes.`, box);
    if (status.invitation) {
      this.node('h4', 'Investigator and ticket', box);
      this.node('pre', JSON.stringify(status.invitation, null, 2), box);
    }
    if (status.policy?.disclosure) this.node('p', status.policy.disclosure, box);
    if (status.scope?.categories) this.node('p', 'Approved categories: '+status.scope.categories.join(', ')+'. History begins '+new Date(status.scope.history_start*1000).toLocaleString()+'. HA Recorder import: '+(status.scope.ha_recorder?'approved':'not approved')+'.', box);
    if (['pending', 'activating'].includes(status.state) && status.invitation) {
      const consent = checkbox('I checked the investigator, ticket and matching phrase, and approve this scope and processing policy.');
      const b = button('Approve read-only access', () => this.run('confirm', { descriptor_digest: status.descriptor_digest })); b.disabled = true; consent.onchange = () => { b.disabled = !consent.checked || this._busy; };
    }
    if (['pending','activating','active'].includes(status.state)) button('Revoke support access', () => this.run('revoke'));
    if (status.state === 'active' && status.scope?.categories?.includes('history')) {
      this.node('h4', 'Share a retained incident archive', box);
      const approved = checkbox('I also approve retaining the approved previous-hour history for this investigator for 24 hours, including while HA is offline. AI/transcript copies follow the displayed processing policy.');
      const share = button('Share approved archive for 24 hours', () => this.run('archive', { duration: 86400 })); share.disabled = true; approved.onchange = () => { share.disabled = !approved.checked || this._busy; };
    }
    if (status.archive_uncertain) this.node('p', 'Archive upload is uncertain. Retry the identical upload during this session or revoke retained archives.', box);
    if (status.archive_expires_at) {
      this.node('p', `Shared archive deadline: ${new Date(status.archive_expires_at * 1000).toLocaleString()}`, box);
    }
    if (status.archive_present || status.archive_pending || status.archive_expires_at) button('Revoke shared archive', () => this.run('archive_revoke'));
    if (status.scope?.expires_at) this.node('p', `Session deadline: ${new Date(status.scope.expires_at * 1000).toLocaleString()}`, box);
    if (status.access_receipt?.length) { this.node('h4', 'Recent access receipt', box); this.node('pre', JSON.stringify(status.access_receipt, null, 2), box); }
    if (status.audit?.length) { this.node('h4', 'Recent diagnostic queries', box); this.node('pre', JSON.stringify(status.audit, null, 2), box); }
  }
}
if (!customElements.get('ps-support-card')) customElements.define('ps-support-card', PowerSyncSupportCard);
