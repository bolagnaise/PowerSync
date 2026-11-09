/* HA-only support consent. No mobile settings, model calls or MCP credentials. */
class PowerSyncSupportCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }); this._entries = []; this._busy = false; }
  set hass(value) { this._hass = value; if (!this._loaded && value?.user?.is_admin) { this._loaded = true; this.load(); } }
  get hass() { return this._hass; }
  set config(value) { this._config = value; }
  connectedCallback() { this._connected = true; this.scheduleRefresh(); }
  disconnectedCallback() { this._connected = false; clearTimeout(this._refresh); }
  scheduleRefresh() {
    clearTimeout(this._refresh);
    if (this._connected && this._hass?.user?.is_admin && ['pending', 'activating', 'active'].includes(this._status?.state)) {
      this._refresh = setTimeout(() => this.load(true), 5000);
    }
  }
  async api(action, extra = {}) {
    return this._hass.callApi(action ? 'POST' : 'GET', 'power_sync/support', action ? { action, entry_id: this._entry, ...extra } : undefined);
  }
  viewState() {
    // Recorder counters and access receipts change independently of the consent form.
    const { recorder_bytes, dropped_records, audit, access_receipt, ...status } = this._status || {};
    return JSON.stringify([this._entry, status, this._message, this._refreshError]);
  }
  async load(background = false) {
    if (this._busy) { this.scheduleRefresh(); return; }
    this._busy = true;
    const before = this.viewState();
    try {
      const value = await this.api(); this._entries = value.entries || [];
      if (!this._entries.some(x => x.entry_id === this._entry)) this._entry = this._entries[0]?.entry_id;
      this._status = this._entries.find(x => x.entry_id === this._entry)?.status;
      this._refreshError = false;
    } catch { this._refreshError = true; }
    finally {
      this._busy = false;
      // Unchanged background reads must not replace the consent checkbox or input focus.
      if (!background || before !== this.viewState()) this.render();
      this.scheduleRefresh();
    }
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
    this.node('style', `
      :host{display:block;color:var(--primary-text-color,#202124);font:14px system-ui;line-height:1.5}
      *{box-sizing:border-box}section{padding:20px;max-width:720px;margin:auto}
      h3{font-size:22px;margin:0}h4{font-size:17px;margin:0 0 8px}p{margin:8px 0 16px}
      .muted,small{color:var(--secondary-text-color,#616161)}small{display:block;line-height:1.5}
      .status{display:inline-block;margin:12px 0;padding:4px 10px;border-radius:20px;background:var(--secondary-background-color,#f1f3f4);font-weight:600}
      .step{padding:18px 0;border-top:1px solid var(--divider-color,#dadce0)}
      label{display:block;margin:12px 0}label span{vertical-align:middle}
      button,input,select{font:inherit;color:inherit;border:1px solid var(--divider-color,#9aa0a6);border-radius:8px}
      input:not([type=checkbox]),select{display:block;width:100%;padding:12px;background:var(--card-background-color,#fff);margin-top:6px;min-height:46px}
      input[type=checkbox]{width:18px;height:18px;vertical-align:middle;margin:0 10px 0 0;accent-color:var(--primary-color,#03a9f4)}
      button{min-height:44px;padding:10px 16px;background:transparent;cursor:pointer;margin:8px 8px 0 0}
      button.primary{background:var(--primary-color,#03a9f4);color:var(--text-primary-color,#fff);border-color:transparent;font-weight:600}
      button.danger{color:var(--error-color,#b00020)}button:disabled{opacity:.5;cursor:default}
      button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--primary-color,#03a9f4);outline-offset:3px}
      .code{font:600 clamp(22px,5vw,30px) ui-monospace,monospace;letter-spacing:.06em;text-align:center;user-select:all}
      details{margin:16px 0;border-top:1px solid var(--divider-color,#dadce0);padding-top:14px}summary{cursor:pointer;font-weight:600;min-height:32px}
      dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 16px;margin:14px 0}dt{color:var(--secondary-text-color,#616161)}dd{margin:0;overflow-wrap:anywhere}
      .notice{padding:12px;border-radius:8px;background:var(--secondary-background-color,#f1f3f4)}
      pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;font-size:12px}
      @media(max-width:420px){section{padding:16px}dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:8px}button.primary{width:100%}}
    `, root);
    const box = this.node('section', undefined, root);
    this.node('h3', 'Support', box);
    this.node('p', 'Share diagnostics with PowerSync support. You choose what to share and can stop access at any time.', box);
    if (!this._hass?.user?.is_admin) { this.node('p', 'Home Assistant administrator access is required.', box); return; }
    const status = this._status || {}, state = status.state || 'closed';
    const live = ['pending', 'activating', 'active'].includes(state);
    const claimed = !!status.invitation;
    const labels = {system:'Versions and safe settings',entities:'Energy readings and timestamps',optimizer:'Optimizer plan and constraints',control_trace:'Recorded control observations',logs:'Structured diagnostic events',history:'Selected diagnostic history'};
    const date = value => new Date(value * 1000).toLocaleString();
    const button = (text, action, parent = box, kind = '') => {
      const b = this.node('button', text, parent); b.className = kind;
      b.disabled = this._busy || !this._entry; b.onclick = action; return b;
    };
    const checkbox = (label, checked = false, parent = box) => {
      const l = this.node('label', undefined, parent), c = this.node('input', undefined, l);
      c.type = 'checkbox'; c.checked = checked; this.node('span', label, l); return c;
    };
    const details = (title, parent = box) => { const d = this.node('details', undefined, parent); this.node('summary', title, d); return d; };
    const field = (label, tag, parent) => { const l = this.node('label', label, parent), e = this.node(tag, undefined, l); e.setAttribute('aria-label', label); return e; };
    const row = (list, label, value) => { this.node('dt', label, list); this.node('dd', value, list); };
    if (this._entries.length > 1) {
      const entries = field('PowerSync installation', 'select', box);
      this._entries.forEach((entry, i) => { const o = this.node('option', `PowerSync installation ${i + 1}`, entries); o.value = entry.entry_id; o.selected = entry.entry_id === this._entry; });
      entries.onchange = () => { this._entry = entries.value; this._status = this._entries.find(x => x.entry_id === this._entry)?.status; this._message = ''; this.render(); this.scheduleRefresh(); };
    }
    const badge = this.node('span', state === 'active' ? 'Read-only access active' : live ? (claimed ? 'Ready for your approval' : 'Waiting for support') : 'No live access', box);
    badge.className = 'status'; badge.setAttribute('role', 'status');
    if (this._message || status.error || this._refreshError) {
      const message = this.node('p', this._message || status.error || 'Could not refresh support status. Check your connection and try again.', box);
      message.className = 'notice'; message.setAttribute('role', 'alert');
    }
    if (!this._entry) this.node('p', 'No PowerSync installation is available. Refresh to try again.', box);
    if (!live) {
      const step = this.node('div', undefined, box); step.className = 'step';
      this.node('h4', '1. Paste your support invitation', step);
      this.node('p', 'Ask PowerSync support for an invitation in your private Discord ticket, then paste it below.', step);
      const invitation = field('Support invitation', 'input', step); invitation.type = 'text'; invitation.value = ''; invitation.placeholder = 'Paste invitation from your private ticket'; invitation.maxLength = 128; invitation.autocomplete = 'off'; invitation.spellcheck = false;
      const summary = this.node('p', 'Sharing: versions, safe settings and energy readings · 1 hour · no past history.', step); summary.className = 'muted';
      const options = details('Choose what to share', step), cats = {};
      for (const [category, label] of Object.entries(labels)) cats[category] = checkbox(label, ['system','entities'].includes(category), options);
      const duration = field('Support access duration', 'select', options);
      for (const [value,label] of [[3600,'1 hour'],[7200,'2 hours'],[28800,'8 hours']]) { const o = this.node('option', label, duration); o.value = String(value); }
      const history = field('Historical sharing window', 'select', options);
      for (const [value,label] of [[0,'No past history'],[600,'Previous 10 minutes'],[3600,'Previous hour'],[86400,'Previous 24 hours']]) { const o = this.node('option', label, history); o.value = String(value); }
      const haRecorder = checkbox('Also share approved historical readings from HA Recorder', false, options);
      const prepare = button('Get pairing code', () => this.run('start', { invitation_token: invitation.value.trim(), categories: Object.keys(cats).filter(k => cats[k].checked), duration: Number(duration.value), history_seconds: Number(history.value), ha_recorder: haRecorder.checked }), step, 'primary');
      const update = () => {
        const selected = Object.keys(cats).filter(k => cats[k].checked);
        summary.textContent = `Sharing: ${selected.map(k => labels[k].toLowerCase()).join(', ') || 'nothing selected'} · ${Number(duration.value) / 3600} hour(s) · ${Number(history.value) ? 'selected past history' : 'no past history'}${haRecorder.checked ? ' · HA Recorder included' : ''}.`;
        prepare.disabled = this._busy || !this._entry || !invitation.value.trim() || !selected.length;
      };
      prepare.disabled = true; invitation.oninput = update;
      Object.values(cats).forEach(c => { c.onchange = update; }); duration.onchange = history.onchange = haRecorder.onchange = update;
      this.node('small', 'Getting a code does not grant access. You will review and approve the investigator in the final step.', step);
      if (status.reason === 'pairing_expired' || status.reason === 'expired') this.node('p', 'This session expired. Ask support for a fresh invitation to start again.', step);
    } else if (state !== 'active') {
      const step = this.node('div', undefined, box); step.className = 'step';
      this.node('h4', claimed ? '3. Review and approve' : '2. Send your pairing code', step);
      if (!claimed && status.code) {
        this.node('p', 'Send this code in the same private Discord ticket.', step);
        const code = field('Pairing code', 'input', step); code.type = 'text'; code.value = status.code; code.readOnly = true; code.className = 'code'; code.autocomplete = 'off'; code.spellcheck = false;
        button('Copy pairing code', async () => {
          try { await navigator.clipboard.writeText(status.code); copyStatus.textContent = 'Pairing code copied.'; }
          catch { code.focus(); code.select(); copyStatus.textContent = 'Code selected. Copy it and paste it into your private ticket.'; }
        }, step, 'primary');
        const copyStatus = this.node('small', '', step); copyStatus.setAttribute('role', 'status');
        this.node('p', 'Waiting for support to claim your code. This card checks automatically; no access is granted yet.', step);
      } else if (!claimed) this.node('p', 'Preparing your code. Refresh status if it does not appear.', step);
      this.node('small', 'Complete pairing and approval within 30 minutes of getting your code. This is a time limit, not a waiting period.', step);
    } else {
      this.node('h4', 'Support is connected', box);
      this.node('p', 'Support can read only the diagnostics you approved. It cannot change settings or control your devices.', box);
    }
    if (live && claimed) {
      const identity = this.node('dl', undefined, box);
      row(identity, 'Investigator', status.invitation.actor);
      row(identity, 'Ticket', status.invitation.ticket);
      row(identity, 'Reporter', status.invitation.reporter);
      row(identity, 'Matching phrase', status.invitation.phrase);
      if (state !== 'active') this.node('p', 'Check these details against your private ticket before approving.', box);
    }
    if (live && status.scope?.categories) {
      const scope = this.node('dl', undefined, box);
      row(scope, state === 'active' ? 'Approved sharing' : 'Requested sharing', status.scope.categories.map(k => labels[k] || k).join(', '));
      row(scope, 'History starts', date(status.scope.history_start));
      row(scope, 'HA Recorder', status.scope.ha_recorder ? 'Included' : 'Not included');
      row(scope, 'Access ends', date(status.scope.expires_at));
    }
    if (live && status.policy?.disclosure) {
      const policy = details('Privacy and processing policy');
      // Visible before confirmation so informed consent does not depend on opening a disclosure.
      if (state !== 'active' && claimed) policy.open = true;
      this.node('p', status.policy.disclosure, policy);
    }
    if (['pending', 'activating'].includes(state) && claimed) {
      const consent = checkbox('I checked the investigator, ticket and matching phrase, and approve this sharing scope and processing policy.');
      const approve = button('Approve read-only access', () => this.run('confirm', { descriptor_digest: status.descriptor_digest }), box, 'primary');
      approve.disabled = true;
      consent.onchange = () => { approve.disabled = !consent.checked || this._busy || !this._entry; };
    }
    if (live) button(state === 'active' ? 'Stop support access' : 'Cancel setup', () => this.run('revoke'), box, 'danger');
    const refresh = button('Refresh status', () => this.load());
    refresh.disabled = this._busy; // Retrying a failed initial load needs no selected entry.
    if (state === 'active' && status.scope?.categories?.includes('history')) {
      const archive = details('Share an incident archive');
      this.node('p', 'An archive remains available for 24 hours, including while HA is offline. Stopping live access does not revoke a shared archive.', archive);
      const approved = checkbox('I also approve retaining the approved previous-hour history for this investigator for 24 hours. AI and transcript copies follow the processing policy above.', false, archive);
      const share = button('Share approved archive for 24 hours', () => this.run('archive', { duration: 86400 }), archive); share.disabled = true;
      approved.onchange = () => { share.disabled = !approved.checked || this._busy || !this._entry; };
    }
    if (status.archive_uncertain) this.node('p', 'Archive upload is uncertain. Retry the identical upload during this session or revoke retained archives.', box);
    if (status.archive_expires_at) this.node('p', `Shared archive available until ${date(status.archive_expires_at)}.`, box);
    if (status.archive_present || status.archive_pending || status.archive_expires_at) button('Revoke shared archive', () => this.run('archive_revoke'), box, 'danger');
    const local = details('Local diagnostics and recording');
    this.node('p', 'Download diagnostics without granting remote access. Recording is optional and stays local until you approve sharing.', local);
    button('Download local diagnostics', () => this.run('bundle'), local);
    const recording = checkbox('Record diagnostic history locally (up to 24 hours, subject to storage limits)', status.recording, local);
    const persistent = checkbox('Keep local history through restarts; HA backups may include these records', status.persistent, local);
    button('Save recording preference', () => this.run('recording', { recording: recording.checked, persistent: persistent.checked }), local);
    this.node('small', 'Stopping recording clears retained support history. Existing HA Recorder data is managed by Home Assistant.', local);
    if (status.access_receipt?.length || status.audit?.length) {
      const activity = details('Recent support activity');
      if (status.access_receipt?.length) { this.node('h4', 'Access receipts', activity); this.node('pre', JSON.stringify(status.access_receipt, null, 2), activity); }
      if (status.audit?.length) { this.node('h4', 'Diagnostic queries', activity); this.node('pre', JSON.stringify(status.audit, null, 2), activity); }
    }
  }
}
if (!customElements.get('ps-support-card')) customElements.define('ps-support-card', PowerSyncSupportCard);
