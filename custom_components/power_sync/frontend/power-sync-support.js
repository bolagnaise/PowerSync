/* HA-only support consent. No mobile settings, model calls or MCP credentials. */
class PowerSyncSupportCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }); this._entries = []; this._busy = false; }
  set hass(value) { this._hass = value; if (!this._loaded && value?.user?.is_admin) { this._loaded = true; this.load(); } }
  get hass() { return this._hass; }
  set config(value) { this._config = value; }
  setConfig(value) { this.config = value; }
  getCardSize() { return 5; }
  connectedCallback() { this._connected = true; this.scheduleRefresh(); }
  disconnectedCallback() { this._connected = false; clearTimeout(this._refresh); }
  scheduleRefresh() {
    clearTimeout(this._refresh);
    if (this._connected && this._hass?.user?.is_admin && (['pending', 'activating', 'active'].includes(this._status?.state) || this._diagnostics?.grants?.length || this._diagnostics?.root_revocation)) {
      this._refresh = setTimeout(() => this.load(true), 5000);
    }
  }
  async api(action, extra = {}) {
    return this._hass.callApi(action ? 'POST' : 'GET', 'power_sync/support', action ? { action, entry_id: this._entry, ...extra } : undefined);
  }
  viewState() {
    // Recorder counters and access receipts change independently of the consent form.
    const { recorder_bytes, dropped_records, audit, access_receipt, ...status } = this._status || {};
    const diagnostics = this._diagnostics && { ...this._diagnostics,
      grants: (this._diagnostics.grants || []).map(({ activity, ...grant }) => grant) };
    return JSON.stringify([this._entry, status, diagnostics, this._ongoing, this._review, this._message, this._refreshError, this._diagnosticsError]);
  }
  async load(background = false) {
    if (!this._hass?.user?.is_admin) { this.render(); return; }
    if (this._busy) { this.scheduleRefresh(); return; }
    this._busy = true;
    const before = this.viewState();
    try {
      const [session, ongoing] = await Promise.allSettled([this.api(), this.diagnosticsApi()]);
      if (session.status === 'fulfilled') {
        this._entries = session.value.entries || [];
        if (!this._entries.some(x => x.entry_id === this._entry)) this._entry = this._entries[0]?.entry_id;
        this._status = this._entries.find(x => x.entry_id === this._entry)?.status;
      }
      this._refreshError = session.status === 'rejected';
      if (ongoing.status === 'fulfilled') {
        this._diagnostics = ongoing.value;
        if (this._ongoing === undefined) this._ongoing = !!ongoing.value.grants?.length;
      }
      this._diagnosticsError = ongoing.status === 'rejected';
    } catch { this._refreshError = true; }
    finally {
      this._busy = false;
      // Unchanged background reads must not replace the consent checkbox or input focus.
      if (!background || before !== this.viewState()) this.render();
      this.scheduleRefresh();
    }
  }
  node(tag, text, parent) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; parent?.append(e); return e; }
  async diagnosticsApi(action, extra = {}) {
    return this._hass.callApi(action ? 'POST' : 'GET', 'power_sync/diagnostics', action ? { action, ...extra } : undefined);
  }
  async run(action, data = {}, ongoing = false) {
    if (this._busy || !this._hass?.user?.is_admin) return;
    this._busy = true;
    this.shadowRoot.querySelectorAll('button,select,input').forEach(e => { e.disabled = true; });
    try {
      const result = await (ongoing ? this.diagnosticsApi(action, data) : this.api(action, data));
      if (ongoing && action === 'review') { this._review = result; this._ongoingInvitation = ''; }
      else if (ongoing) { this._diagnostics = result; this._review = undefined; }
      else if (action === 'bundle') {
        const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob), a = document.createElement('a');
        a.href = url; a.download = 'powersync-diagnostics.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else this._status = result;
      this._message = '';
      if (ongoing) this._diagnosticsError = false;
    } catch (e) {
      this._message = ongoing ? 'Ongoing support request could not be confirmed. Refresh status before retrying.' : e?.body?.error || 'Support request failed. Refresh the status before retrying.';
    } finally {
      // A confirmation consumes its challenge, including uncertain responses.
      if (ongoing && action === 'confirm') this._review = undefined;
      this._busy = false;
      if (ongoing && action === 'review') this.render(); else await this.load();
    }
  }
  renderOngoingSetup(step, invitation, { button, field, details, checkbox }) {
    this.node('p', 'Ongoing access stays approved through Home Assistant restarts until revoked. Review the invitation for the exact diagnostic and private offline testing permissions.', step);
    const setup = details('Ongoing support setup', step);
    this.node('p', 'Share the setup identity in your private ticket so support can prepare an invitation. Creating an identity grants no access.', setup);
    if (this._diagnostics?.identity) this.node('pre', JSON.stringify(this._diagnostics.identity, null, 2), setup);
    else {
      setup.open = true;
      button('Create setup identity', () => this.run('identity', {}, true), setup, '', true);
    }
    const key = field('Authority public key', 'input', step);
    key.type = 'text'; key.autocomplete = 'off'; key.spellcheck = false;
    key.placeholder = 'Provided through your verified support ticket';
    invitation.type = 'password';
    invitation.value = this._ongoingInvitation || ''; key.value = this._authorityKey || '';
    const review = button('Review invitation', () => this.run('review', { code: invitation.value.trim(), authority_key: key.value.trim() }, true), step, 'primary', true);
    const update = () => {
      this._ongoingInvitation = invitation.value; this._authorityKey = key.value;
      // Editing the invitation invalidates the visible approval locally.
      this._review = undefined;
      approvalArea?.replaceChildren();
      review.disabled = this._busy || !invitation.value.trim() || !key.value.trim();
    };
    invitation.oninput = key.oninput = update;
    review.disabled = this._busy || !invitation.value.trim() || !key.value.trim();
    const approvalArea = this.node('div', undefined, step);
    if (this._review) {
      const reviewed = this._review;
      this.node('h4', 'Review and approve ongoing support', approvalArea);
      const descriptors = reviewed.descriptors || [reviewed.descriptor];
      const policy = details('Privacy and processing policy', approvalArea); policy.open = true;
      for (const descriptor of descriptors) {
        this.node('p', descriptor.disclosure, policy);
        const scope = descriptor.ceiling;
        this.node('p', `Connected application: ${scope.processor}`, policy);
        this.node('p', `Registered operator: ${scope.actor}`, policy);
        this.node('p', scope.kind === 'ha_read' ? 'Purpose: ongoing HA diagnostic reads for support' : 'Purpose: private offline optimizer testing; replay, then delete', policy);
      }
      const consent = checkbox('I reviewed and approve these ongoing permissions and the processing policy.', false, approvalArea);
      const approve = button('Enable ongoing access', () => this.run('confirm', { challenge: reviewed.challenge }, true), approvalArea, 'primary', true);
      approve.disabled = true;
      consent.onchange = () => { approve.disabled = !consent.checked || this._busy || this._review !== reviewed; };
    }
  }
  renderOngoingAccess(box, button) {
    const state = this._diagnostics;
    if (!state) return;
    const grants = state.grants || [], shown = new Set();
    for (const grant of grants) {
      const group = grant.approval_set || grant.id;
      if (shown.has(group)) continue;
      shown.add(group);
      const members = grants.filter(item => grant.approval_set ? item.approval_set === group : item.id === group);
      const row = this.node('div', undefined, box); row.className = 'step';
      const purpose = grant.approval_set ? 'Support and offline testing' : grant.ceiling.kind === 'ha_read' ? 'HA diagnostic reads only' : 'Optimizer contribution only';
      const status = members.every(item => item.status === 'connected') ? 'connected' : members.some(item => item.status === 'revoking_locally_blocked') ? 'revoking · cleanup confirmation may be pending' : 'connecting';
      this.node('h4', 'Ongoing support', row);
      this.node('p', `${grant.ceiling.processor} · ${purpose} · ${status}`, row);
      for (const item of members) if (item.activity?.last_capture_sent_at) this.node('p', `Last encrypted capture sent: ${new Date(item.activity.last_capture_sent_at * 1000).toLocaleString()} · ${item.activity.capture_bytes} bytes · ${item.activity.capture_ms} ms capture overhead`, row);
      button(grant.approval_set ? 'Revoke ongoing access' : 'Revoke this grant', () => this.run('revoke', { grant: grant.id }, true), row, 'danger', true);
    }
    if (state.root_revocation || state.connection) this.node('p', state.root_revocation || state.connection, box);
    if (state.identity) button('Revoke all ongoing access', () => this.run('revoke', {}, true), box, 'danger', true);
  }
  render() {
    const root = this.shadowRoot; root.replaceChildren();
    this.node('style', `
      :host{display:block;min-width:0;overflow-wrap:anywhere;color:var(--primary-text-color,#202124);font:14px system-ui;line-height:1.5}
      *{box-sizing:border-box}section{padding:20px;max-width:720px;margin:auto}
      h3{font-size:22px;margin:0}h4{font-size:17px;margin:0 0 8px}p{margin:8px 0 16px}
      .muted,small{color:var(--secondary-text-color,#616161)}small{display:block;line-height:1.5}
      .status{display:inline-block;margin:12px 0;padding:4px 10px;border-radius:20px;background:var(--secondary-background-color,#f1f3f4);font-weight:600}
      .step{padding:18px 0;border-top:1px solid var(--divider-color,#dadce0)}
      label{display:block;margin:12px 0}label span{vertical-align:middle}
      button,input,select{max-width:100%;font:inherit;color:inherit;border:1px solid var(--divider-color,#9aa0a6);border-radius:8px}
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
    const button = (text, action, parent = box, kind = '', installationWide = false) => {
      const b = this.node('button', text, parent); b.className = kind;
      b.disabled = this._busy || (!installationWide && !this._entry); b.onclick = action; return b;
    };
    const checkbox = (label, checked = false, parent = box) => {
      const l = this.node('label', undefined, parent), c = this.node('input', undefined, l);
      c.type = 'checkbox'; c.checked = checked; this.node('span', label, l); return c;
    };
    const details = (title, parent = box) => { const d = this.node('details', undefined, parent); this.node('summary', title, d); return d; };
    const field = (label, tag, parent) => { const l = this.node('label', label, parent), e = this.node(tag, undefined, l); e.setAttribute('aria-label', label); return e; };
    const row = (list, label, value) => { this.node('dt', label, list); this.node('dd', value, list); };
    const ongoing = checkbox('Allow ongoing support', !!this._ongoing);
    ongoing.disabled = this._busy;
    ongoing.onchange = () => { this._ongoing = ongoing.checked; this._review = undefined; this._message = ''; this.render(); };
    this.node('small', 'Leave off for a temporary support session. Enable to arrange ongoing read-only support and optional private offline testing. Choosing this option does not grant or revoke access.', box);
    this.renderOngoingAccess(box, button);
    if (this._diagnosticsError && (this._ongoing || this._diagnostics?.grants?.length || this._diagnostics?.root_revocation)) {
      const warning = this.node('p', 'Could not refresh ongoing support status. Refresh to retry; existing approvals may still be active.', box);
      warning.className = 'notice'; warning.setAttribute('role', 'alert');
    }
    if (this._entries.length > 1) {
      const entries = field('PowerSync installation', 'select', box);
      this._entries.forEach((entry, i) => { const o = this.node('option', `PowerSync installation ${i + 1}`, entries); o.value = entry.entry_id; o.selected = entry.entry_id === this._entry; });
      entries.onchange = () => { this._entry = entries.value; this._status = this._entries.find(x => x.entry_id === this._entry)?.status; this._message = ''; this.render(); this.scheduleRefresh(); };
    }
    if (live || !this._ongoing) {
      const badge = this.node('span', state === 'active' ? 'Temporary read-only access active' : live ? (claimed ? 'Ready for your approval' : 'Waiting for support') : 'No live access', box);
      badge.className = 'status'; badge.setAttribute('role', 'status');
    }
    if (this._message || status.error || this._refreshError) {
      const message = this.node('p', this._message || status.error || 'Could not refresh support status. Check your connection and try again.', box);
      message.className = 'notice'; message.setAttribute('role', 'alert');
    }
    if (!this._entry) this.node('p', 'No PowerSync installation is available. Refresh to try again.', box);
    if (!live || this._ongoing) {
      const step = this.node('div', undefined, box); step.className = 'step';
      this.node('h4', this._ongoing ? 'Set up ongoing support' : '1. Paste your support invitation', step);
      this.node('p', 'Ask PowerSync support for an invitation in your private Discord ticket, then paste it below.', step);
      const invitation = field('Support invitation', 'input', step); invitation.type = 'text'; invitation.value = ''; invitation.placeholder = 'Paste invitation from your private ticket'; invitation.maxLength = this._ongoing ? 6000 : 128; invitation.autocomplete = 'off'; invitation.spellcheck = false;
      if (this._ongoing) this.renderOngoingSetup(step, invitation, { button, field, details, checkbox });
      else {
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
      }
    }
    if (live && state !== 'active') {
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
    } else if (state === 'active') {
      this.node('h4', 'Temporary support is connected', box);
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
