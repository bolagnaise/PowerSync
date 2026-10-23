"""Runtime regressions for AI card account connection controls."""

from pathlib import Path
import subprocess


STRATEGY_PATH = Path(__file__).resolve().parent.parent / "custom_components/power_sync/frontend/power-sync-strategy.js"


def test_chatgpt_account_controls_keep_credentials_in_memory_and_use_api_contract():
    """Exercise sign-in, errors, cancellation, account settings and permissions."""
    runtime_checks = r"""
      const fs = require('fs');
      const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const start = source.indexOf('class PowerSyncAIPlanExplanation');
      const end = source.indexOf("if (!customElements.get('power-sync-ai-plan-explanation'))", start);
      class Element {
        constructor(tag = 'div') {
          this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {};
          this.textContent = ''; this.value = ''; this.classList = { toggle() {} };
        }
        append(...nodes) { this.children.push(...nodes); }
        setAttribute(key, value) { this.attrs[key] = value; }
        addEventListener(name, listener) { this.listeners[name] = listener; }
        focus() { this.focused = true; }
        get childElementCount() { return this.children.length; }
        set innerHTML(value) {
          this.markup = value; this.children = [];
          for (const match of value.matchAll(/id="([^"]+)"/g)) {
            const element = new Element(); element.id = match[1]; this.append(element);
          }
        }
        getElementById(id) { return flatten(this).find(element => element.id === id); }
      }
      function flatten(node) { return [node, ...node.children.flatMap(flatten)]; }
      class Host extends Element { attachShadow() { return this.shadowRoot = new Element(); } }
      const context = {
        HTMLElement: Host, document: { createElement: tag => new Element(tag) },
        URL, console, setTimeout, clearTimeout,
      };
      vm.createContext(context);
      vm.runInContext(source.slice(start, end) + '\nglobalThis.Card = PowerSyncAIPlanExplanation;', context);
      const expect = (condition, message) => { if (!condition) throw new Error(message); };
      const nodes = card => flatten(card.shadowRoot);
      const text = card => nodes(card).map(node => node.textContent).join(' ');
      const button = (card, label) => nodes(card).find(node => node.tag === 'button' && node.textContent === label);
      let calls = [];
      let next = { success: true, connected: false, status: 'disconnected', account: null, settings: {} };
      const hass = { user: { is_admin: true }, callApi: async (method, route, body) => {
        calls.push({ method, route, body });
        if (route === 'power_sync/optimization') return { features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready' } };
        if (next instanceof Error) throw next;
        return next;
      } };
      async function run() {
        const card = new context.Card();
        card._hass = hass; card._state = 'not_configured'; card._render();
        expect(button(card, 'Continue with ChatGPT'), 'unconfigured admin cannot find sign-in');
        await card._loadConnectionOnce();
        expect(calls[0].method === 'GET' && calls[0].route === 'power_sync/optimization/chatgpt', 'wrong status endpoint');
        const authUrl = 'https://auth.openai.com/oauth/authorize?state=%22%3E%3Cscript%3E';
        next = { success: true, login_id: 'login-1', auth_url: authUrl, expires_at: '2026-10-09T13:00:00Z' };
        await card._connectionAction('start');
        expect(calls.at(-1).body.action === 'start', 'start payload missing');
        const link = card.shadowRoot.getElementById('openai-sign-in');
        expect(link.tag === 'a' && link.href === authUrl && link.target === '_blank' && link.rel.includes('noopener'), 'sign-in link unsafe');
        expect(text(card).includes('tab cannot connect'), 'callback recovery instructions missing');
        const input = card.shadowRoot.getElementById('chatgpt-callback');
        input.value = 'http://127.0.0.1:1455/auth/callback?code=secret&state=state'; input.listeners.input();
        expect(!button(card, 'Complete connection').disabled, 'callback input did not enable completion');
        let savedCallback = card._callbackUrl;
        let error = new Error('must not display secret provider response'); error.body = { code: 'invalid_callback', login_restart_required: false };
        next = error;
        await card._connectionAction('complete', { login_id: 'login-1', callback_url: savedCallback });
        const completionRequest = calls.findLast(call => call.method === 'POST' && call.route === 'power_sync/optimization/chatgpt' && call.body?.action === 'complete');
        expect(completionRequest && completionRequest.body.callback_url === savedCallback && completionRequest.body.login_id === 'login-1', 'completion payload wrong');
        expect(text(card).includes('Paste the full address') && !text(card).includes(error.message), 'unsafe/unhelpful error display');
        expect(card._callbackUrl === savedCallback, 'retry lost callback');
        next = { success: true, connected: true, status: 'connected', account: { email: '<img src=x onerror=alert(1)>' }, settings: { ai_summary_provider: 'chatgpt', ai_summary_openai_model: 'available-model' }, models: [{ id: 'available-model', name: '<script>model</script>' }] };
        await card._connectionAction('complete', { login_id: 'login-1', callback_url: savedCallback });
        expect(card._callbackUrl === '' && card._login === null && !card.shadowRoot.getElementById('chatgpt-callback'), 'completion retained callback');
        expect(text(card).includes('Using ChatGPT plan') && text(card).includes('<img src=x'), 'account or provider state missing');
        expect(!card.shadowRoot.markup.includes('<img src=x') && !card.shadowRoot.markup.includes('<script>model'), 'OAuth fields injected markup');
        expect(nodes(card).some(node => node.tag === 'a' && node.href === 'https://chatgpt.com/codex/settings/usage'), 'usage link missing');
        const select = card.shadowRoot.getElementById('chatgpt-model');
        select.value = 'available-model';
        select.listeners.change();
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(calls.some(call => call.body?.action === 'settings' && call.body.ai_summary_openai_model === 'available-model'), 'model selection not saved');
        expect(calls.some(call => call.route === 'power_sync/optimization'), 'summary status not reloaded');
        // The API alternative sends the key only on explicit submit.
        const provider = card.shadowRoot.getElementById('ai-provider');
        expect(provider.children.map(option => option.value).sort().join(',') === 'chatgpt,gemini,grok,local_openai_compatible,openai,openrouter', 'supported providers missing');
        provider.value = 'openai'; provider.listeners.change();
        const key = card.shadowRoot.getElementById('openai-key');
        expect(key.type === 'password' && key.autocomplete === 'off', 'key input unprotected');
        key.value = 'api-secret'; key.listeners.input();
        const model = card.shadowRoot.getElementById('openai-model'); model.value = 'chosen-api-model'; model.listeners.input();
        next = { success: true, connected: true, settings: { ai_summary_provider: 'openai', ai_summary_openai_model: 'chosen-api-model', ai_summary_api_key_configured: true } };
        const apiForm = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
        apiForm.listeners.submit({ preventDefault() {} });
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(calls.some(call => call.body?.ai_summary_provider === 'openai' && call.body.ai_summary_api_key === 'api-secret'), 'API settings payload wrong');
        expect(card._apiKey === '' && card.shadowRoot.getElementById('openai-key').value === '', 'saved API key retained in UI');
        // Every existing provider can be configured from the same selector.
        for (const providerId of ['gemini', 'grok', 'openrouter', 'local_openai_compatible']) {
          const choice = card.shadowRoot.getElementById('ai-provider'); choice.value = providerId; choice.listeners.change();
          expect(card._apiKey === '', 'switching provider retained a draft key');
          const fields = { 'provider-key': 'FAKE-key-' + providerId };
          if (providerId === 'openrouter') fields['openrouter-model'] = 'anthropic/example-model';
          if (providerId === 'local_openai_compatible') Object.assign(fields, { 'local-endpoint': 'http://192.168.1.10:11434/v1', 'local-model': 'example-model' });
          for (const [id, value] of Object.entries(fields)) { const input = card.shadowRoot.getElementById(id); expect(input, 'provider-specific input missing: ' + id); input.value = value; input.listeners.input(); }
          next = { success: true, connected: true, settings: { ai_summary_provider: providerId, ai_summary_api_key_configured: true } };
          const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'provider-key'));
          form.listeners.submit({ preventDefault() {} }); await new Promise(resolve => setTimeout(resolve, 0));
          const request = calls.findLast(call => call.body?.action === 'settings');
          expect(request.body.ai_summary_provider === providerId && request.body.ai_summary_api_key === fields['provider-key'], 'provider settings payload wrong');
          if (providerId === 'openrouter') expect(request.body.ai_summary_openrouter_model === fields['openrouter-model'], 'OpenRouter model was not saved');
          if (providerId === 'local_openai_compatible') expect(request.body.ai_summary_local_endpoint === fields['local-endpoint'] && request.body.ai_summary_local_model === fields['local-model'], 'local server settings were not saved');
          expect(card._apiKey === '' && !text(card).includes(fields['provider-key']), 'key retained in memory or activity after save');
        }
        // Cancel and disconnect clear secret fields; pending GET metadata cannot reconstruct the auth URL.
        next = { success: true, login_id: 'login-2', auth_url: authUrl };
        await card._connectionAction('start');
        card._callbackUrl = 'secret'; next = { success: true };
        await card._connectionAction('cancel', { login_id: 'login-2' });
        expect(card._callbackUrl === '' && card._login === null && calls.at(-1).body.action === 'cancel', 'cancel retained pending input');
        card._chatgpt = { connected: false, pending: { login_id: 'old', expires_at: 'later' } }; card._render();
        expect(text(card).includes('unfinished') && !card.shadowRoot.getElementById('openai-sign-in'), 'refresh fabricated an auth URL');
        next = { success: true, login_id: 'login-3', auth_url: authUrl };
        await card._connectionAction('start');
        card._callbackUrl = 'secret'; card.shadowRoot.getElementById('chatgpt-callback').value = 'secret';
        card.disconnectedCallback();
        expect(card._callbackUrl === '' && card._login === null && card.shadowRoot.getElementById('chatgpt-callback').value === '', 'disconnect retained DOM callback');
        // Responses resolving after removal cannot bring credentials back into memory.
        let release;
        card._hass = { user: { is_admin: true }, callApi: () => new Promise(resolve => { release = resolve; }) };
        const pending = card._connectionAction('start'); card.disconnectedCallback(); release(next); await pending;
        expect(card._login === null, 'stale response restored login after disconnect');
        card._hass = hass;
        for (const unsafeUrl of ['javascript:alert(1)', 'not a URL']) {
          next = { success: true, login_id: 'bad', auth_url: unsafeUrl };
          await card._connectionAction('start');
          expect(card._login === null && card._connectionError === 'invalid_auth_url', 'unsafe auth origin accepted');
          expect(text(card).includes('sign-in link could not be verified') && !text(card).includes(unsafeUrl), 'invalid sign-in link error is absent or exposes the URL');
        }
        card._callbackUrl = 'old'; card._apiKey = 'old-key';
        next = { success: true, connected: false, status: 'disconnected', settings: { ai_summary_provider: 'chatgpt' } };
        await card._connectionAction('logout');
        expect(card._callbackUrl === '' && card._apiKey === '' && !card._chatgpt.connected, 'logout retained credentials or connection');
        // Non-admins see no settings and never request account state.
        const reader = new context.Card(); reader._hass = { ...hass, user: { is_admin: false } }; reader._state = 'not_configured'; reader._render();
        const count = calls.length; await reader._loadConnectionOnce(); await reader._connectionAction('start');
        expect(calls.length === count && !button(reader, 'AI settings') && !button(reader, 'Continue with ChatGPT'), 'non-admin can use connection controls');
        expect(!/localStorage|sessionStorage/.test(source.slice(start, end)), 'connection persists secrets in browser storage');
      }
      run().catch(error => { console.error(error); process.exitCode = 1; });
    """
    subprocess.run(["node", "-e", runtime_checks, str(STRATEGY_PATH)], check=True)


def test_connection_attempt_shows_progress_and_safe_scrolling_activity():
    """The real submit path must explain an outstanding or failed request."""
    runtime_checks = r"""
      const fs = require('fs'); const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const start = source.indexOf('class PowerSyncAIPlanExplanation');
      const end = source.indexOf("if (!customElements.get('power-sync-ai-plan-explanation'))", start);
      class Element {
        constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {}; this.textContent = ''; this.value = ''; this.classList = { toggle() {} }; }
        append(...nodes) { this.children.push(...nodes); }
        setAttribute(key, value) { this.attrs[key] = value; }
        addEventListener(name, listener) { this.listeners[name] = listener; }
        focus() {}
        set innerHTML(value) { this.markup = value; this.children = []; for (const match of value.matchAll(/id="([^"]+)"/g)) { const e = new Element(); e.id = match[1]; this.append(e); } }
        getElementById(id) { return flatten(this).find(e => e.id === id); }
      }
      const flatten = node => [node, ...node.children.flatMap(flatten)];
      class Host extends Element { attachShadow() { return this.shadowRoot = new Element(); } }
      const context = { HTMLElement: Host, document: { createElement: tag => new Element(tag) }, URL, console, setTimeout, clearTimeout };
      vm.createContext(context); vm.runInContext(source.slice(start, end) + '\nglobalThis.Card = PowerSyncAIPlanExplanation;', context);
      const expect = (condition, message) => { if (!condition) throw new Error(message); };
      const nodes = card => flatten(card.shadowRoot);
      const text = card => nodes(card).map(e => e.textContent).join(' ');
      async function run() {
        const card = new context.Card();
        let reject; let calls = 0;
        card._hass = { user: { is_admin: true }, callApi: () => { calls++; return new Promise((resolve, failure) => { reject = failure; }); } };
        card._state = 'not_configured'; card._connectionOpen = true;
        card._login = { login_id: 'fake-login', auth_url: 'https://auth.openai.com/api/accounts/authorize?state=fake' };
        card._callbackUrl = 'http://127.0.0.1:1455/auth/callback?code=FAKE-SECRET&state=fake'; card._render();
        card._callbackUrl = 'https://auth.openai.com/api/accounts/authorize?state=FAKE-SECRET';
        await card._connectionAction('complete', { login_id: 'fake-login', callback_url: card._callbackUrl });
        expect(calls === 0 && text(card).includes('This is the sign-in link'), 'initial sign-in link sent for exchange');
        expect(!text(card).includes('FAKE-SECRET'), 'invalid address leaked into activity');
        card._callbackUrl = 'http://127.0.0.1:1455/auth/callback?code=FAKE-SECRET&state=fake'; card._render();
        const form = nodes(card).find(e => e.tag === 'form' && e.children.some(child => child.id === 'chatgpt-callback'));
        form.listeners.submit({ preventDefault() {} });
        expect(calls === 1, 'submit did not start the connection');
        expect(nodes(card).some(e => e.tag === 'button' && e.textContent === 'Connecting to ChatGPT…' && e.disabled), 'pending submit has no clear button progress');
        expect(nodes(card).some(e => e.attrs.role === 'log'), 'pending submit has no activity log');
        expect(text(card).includes('Sending your sign-in result to OpenAI'), 'pending submit does not explain its current work');
        expect(!text(card).includes('FAKE-SECRET'), 'activity leaked callback code');
        const failure = new Error('FAKE-SECRET must never appear'); failure.body = { code: 'network_error', login_restart_required: false }; reject(failure);
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(text(card).includes('could not reach OpenAI'), 'network failure is absent from activity');
        expect(!text(card).includes(failure.message), 'failure leaked provider details');
        expect(nodes(card).some(e => e.tag === 'button' && e.textContent === 'Complete connection' && !e.disabled), 'failed request did not restore retry');
        // A request which never resolves must release the controls and offer a status check.
        card._connectionTimeoutMs = 15;
        let release; card._hass.callApi = method => method === 'GET' ? Promise.reject(new Error('Status unavailable')) : new Promise(resolve => { release = resolve; });
        await card._connectionAction('complete', { login_id: 'fake-login', callback_url: card._callbackUrl });
        expect(!card._connectionBusy && text(card).includes('taking too long') && text(card).includes('Check connection again'), 'hung request never recovered');
        release({ success: true, connected: true, settings: { ai_summary_provider: 'chatgpt' } });
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(!card._chatgpt?.connected, 'late response applied after timeout');
        expect(card._activity.length <= 24, 'activity is unbounded');
        card.disconnectedCallback();
      }
      run().catch(error => { console.error(error); process.exitCode = 1; });
    """
    subprocess.run(["node", "-e", runtime_checks, str(STRATEGY_PATH)], check=True)


def test_settings_toggle_preserves_card_and_scopes_activity_to_open_sign_in():
    """Opening settings must not replace the card; idle history stays hidden."""
    runtime_checks = r"""      const fs = require('fs');
      const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const start = source.indexOf('class PowerSyncAIPlanExplanation');
      const end = source.indexOf("if (!customElements.get('power-sync-ai-plan-explanation'))", start);
      class Element {
        constructor(tag = 'div') {
          this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {};
          this.textContent = ''; this.value = ''; this.classList = { toggle() {} };
        }
        append(...nodes) { this.children.push(...nodes); }
        setAttribute(key, value) { this.attrs[key] = value; }
        addEventListener(name, listener) { this.listeners[name] = listener; }
        focus() { this.focused = true; }
        get childElementCount() { return this.children.length; }
        set innerHTML(value) {
          this.markup = value; this.children = [];
          for (const match of value.matchAll(/id="([^"]+)"/g)) {
            const element = new Element(); element.id = match[1]; this.append(element);
          }
        }
        getElementById(id) { return flatten(this).find(element => element.id === id); }
      }
      function flatten(node) { return [node, ...node.children.flatMap(flatten)]; }
      class Host extends Element { attachShadow() { return this.shadowRoot = new Element(); } }
      const context = {
        HTMLElement: Host, document: { createElement: tag => new Element(tag) },
        URL, console, setTimeout, clearTimeout,
      };
      vm.createContext(context);
      vm.runInContext(source.slice(start, end) + '\nglobalThis.Card = PowerSyncAIPlanExplanation;', context);
      const expect = (condition, message) => { if (!condition) throw new Error(message); };
      const nodes = card => flatten(card.shadowRoot);
      const text = card => nodes(card).map(node => node.textContent).join(' ');
      const button = (card, label) => nodes(card).find(node => node.tag === 'button' && node.textContent === label);

      async function run() {
        const card = new context.Card(); card._hass = { user: { is_admin: true }, callApi: async () => ({ success: true, connected: true, settings: { ai_summary_provider: 'chatgpt', ai_summary_key_configured: true } }) };
        card._state = 'ready'; card._render();
        const header = card.shadowRoot.getElementById('header');
        const disclosure = card.shadowRoot.getElementById('connection-toggle');
        disclosure.listeners.click();
        expect(card.shadowRoot.getElementById('header') === header && card.shadowRoot.getElementById('connection-toggle') === disclosure, 'settings toggle rebuilt the card and lost its position');
        expect(!card.shadowRoot.getElementById('connection-details').hidden, 'settings did not open');
        card._recordActivity('Old completed request.'); card._render();
        expect(!nodes(card).some(e => e.attrs.role === 'log'), 'idle settings show activity history');
        card._login = { login_id: 'fake-login', auth_url: 'https://auth.openai.com/api/accounts/authorize?state=fake' }; card._selectedProvider = 'chatgpt'; card._recordActivity('Waiting for your sign-in result.'); card._render();
        const details = card.shadowRoot.getElementById('connection-details');
        expect(flatten(details).some(e => e.attrs.role === 'log'), 'unfinished sign-in activity lives outside settings');
        card.shadowRoot.getElementById('connection-toggle').listeners.click();
        expect(details.hidden && !card._connectionOpen, 'closing settings left activity visible');
        card.shadowRoot.getElementById('connection-toggle').listeners.click();
        expect(!details.hidden && card._login, 'reopening lost pending sign-in');
        card._callbackUrl = 'http://127.0.0.1:1455/auth/callback?code=FAKE&state=fake';
        await card._connectionAction('complete', { login_id: 'fake-login', callback_url: card._callbackUrl });
        expect(!card._connectionOpen && card.shadowRoot.getElementById('connection-details').hidden, 'successful setup did not collapse');
        expect(!nodes(card).some(e => e.attrs.role === 'log') && card._activity.length === 0, 'completed setup kept activity history');
        card.shadowRoot.getElementById('connection-toggle').listeners.click();
        card._selectedProvider = 'gemini'; card._render();
        await card._connectionAction('settings', { ai_summary_provider: 'gemini' });
        expect(!card._connectionOpen && !nodes(card).some(e => e.attrs.role === 'log'), 'ordinary settings save left setup or activity open');
        card.disconnectedCallback();
      }
      run().catch(error => { console.error(error); process.exitCode = 1; });
    """
    subprocess.run(["node", "-e", runtime_checks, str(STRATEGY_PATH)], check=True)


_COMPONENT_RUNTIME = r"""
  const fs = require('fs'); const vm = require('vm');
  const source = fs.readFileSync(process.argv[1], 'utf8');
  const start = source.indexOf('class PowerSyncAIPlanExplanation');
  const end = source.indexOf("if (!customElements.get('power-sync-ai-plan-explanation'))", start);
  class Element {
    constructor(tag = 'div') {
      this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {};
      this.textContent = ''; this.value = ''; this.classList = { toggle() {} };
    }
    append(...nodes) { this.children.push(...nodes); }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    focus() { this.focused = true; }
    get childElementCount() { return this.children.length; }
    set innerHTML(value) {
      this.markup = value; this.children = [];
      for (const match of value.matchAll(/id="([^"]+)"/g)) {
        const element = new Element(); element.id = match[1]; this.append(element);
      }
    }
    getElementById(id) { return flatten(this).find(element => element.id === id); }
  }
  const flatten = node => [node, ...node.children.flatMap(flatten)];
  const visible = node => node.hidden ? [] : [node, ...node.children.flatMap(visible)];
  class Host extends Element { attachShadow() { return this.shadowRoot = new Element(); } }
  const timers = new Map(); let timerId = 0;
  const context = {
    HTMLElement: Host, document: { createElement: tag => new Element(tag) }, URL, console,
    AI_SUMMARY_CLIENT_TIMEOUT_MS: 180000,
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.Card = PowerSyncAIPlanExplanation;', context);
  const expect = (condition, message) => { if (!condition) throw new Error(message); };
  const nodes = card => flatten(card.shadowRoot);
  const text = card => visible(card.shadowRoot).map(node => node.textContent).join(' ');
  const button = (card, label) => visible(card.shadowRoot).find(node => node.tag === 'button' && node.textContent === label);
  const click = (card, label) => {
    const control = button(card, label); expect(control && !control.disabled, 'missing enabled control: ' + label);
    control.listeners.click();
  };
  const input = (card, id, value) => {
    const control = card.shadowRoot.getElementById(id); expect(control && !control.disabled, 'missing enabled input: ' + id);
    control.value = value; control.listeners.input();
  };
  const submitCallback = card => {
    const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'chatgpt-callback'));
    expect(form, 'callback form missing'); form.listeners.submit({ preventDefault() {} });
  };
  const chooseProvider = (card, value) => {
    const select = card.shadowRoot.getElementById('ai-provider');
    expect(select && !select.disabled, 'provider selector unavailable'); select.value = value; select.listeners.change();
  };
  const openSettings = card => {
    if (card.shadowRoot.getElementById('connection-details').hidden) card.shadowRoot.getElementById('connection-toggle').listeners.click();
  };
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); };
  const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
  const expireTimer = delay => {
    const entry = [...timers].find(([, timer]) => timer.delay === delay); expect(entry, 'expected request timer missing');
    timers.delete(entry[0]); entry[1].callback();
  };
  const disconnected = { success: true, connected: false, status: 'disconnected', pending: null, settings: { ai_summary_provider: 'chatgpt', ai_summary_key_configured: false } };
  const connected = { success: true, connected: true, status: 'connected', pending: null, account: { email: 'current@example.com' }, settings: { ai_summary_provider: 'chatgpt', ai_summary_key_configured: true } };
  const ready = { features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready' } };
  const notConfigured = { features: { ai_summary: true }, ai_summary: { configured: false, state: 'not_configured' } };
  const login = id => ({ success: true, login_id: id, auth_url: 'https://auth.openai.com/oauth/authorize?state=fixture-' + id });
  const callback = id => 'http://127.0.0.1:1455/auth/callback?code=FIXTURE-SECRET&state=fixture-' + id;
  const setup = async (handler, admin = true) => {
    const card = new context.Card();
    const hass = { user: { is_admin: admin }, callApi: handler };
    card.hass = hass; card.connectedCallback(); await settle(); return { card, hass };
  };
"""


def _run_component_runtime(checks):
    subprocess.run([
        "node", "-e", _COMPONENT_RUNTIME + "\nasync function run() {\n" + checks
        + "\n}\nrun().catch(error => { console.error(error); process.exitCode = 1; });",
        str(STRATEGY_PATH),
    ], check=True)


def test_consumed_sign_in_offers_fresh_continue_and_usable_callback_can_be_corrected():
    """Real completion controls distinguish consumed grants from usable pending input."""
    _run_component_runtime(r"""
      let attempt = 0, completion = 0;
      const { card } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return notConfigured;
        if (method === 'GET') return disconnected;
        if (body.action === 'start') return login('login-' + ++attempt);
        if (body.action === 'complete') {
          completion++;
          return completion === 1
            ? { success: false, code: 'network_error', login_restart_required: true }
            : { success: false, code: 'invalid_callback', login_restart_required: false };
        }
        throw new Error('Unexpected request');
      });
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('login-1')); submitCallback(card); await settle();
      expect(button(card, 'Continue with ChatGPT'), 'consumed grant hides fresh Continue');
      expect(!card.shadowRoot.getElementById('chatgpt-callback'), 'consumed callback remains reusable');
      expect(!text(card).includes('Complete connection again'), 'consumed grant still suggests reusing the code');
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('login-2')); submitCallback(card); await settle();
      expect(button(card, 'Complete connection') && !button(card, 'Complete connection').disabled, 'usable pending login lost correction controls');
      expect(card.shadowRoot.getElementById('chatgpt-callback').value === callback('login-2'), 'usable callback was discarded');
      input(card, 'chatgpt-callback', callback('login-2') + '&corrected=1'); submitCallback(card); await settle();
      expect(completion === 3, 'corrected callback could not be resubmitted');
      card.disconnectedCallback();
    """)


def test_timeout_reconciles_server_outcome_before_reusing_sign_in_controls():
    """A timed-out POST may still connect, so the card must consult status first."""
    _run_component_runtime(r"""
      const exchange = deferred(), reconciliation = deferred();
      let submitted = false, checked = 0;
      const { card } = await setup((method, route, body) => {
        if (route === 'power_sync/optimization') return Promise.resolve(submitted ? ready : notConfigured);
        if (method === 'GET') { checked++; return submitted ? reconciliation.promise : Promise.resolve(disconnected); }
        if (body.action === 'start') return Promise.resolve(login('timeout-login'));
        if (body.action === 'complete') { submitted = true; return exchange.promise; }
        throw new Error('Unexpected request');
      });
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('timeout-login')); submitCallback(card); await settle();
      expireTimer(90000); await settle();
      expect(checked === 2, 'timeout did not reconcile the server transaction outcome');
      expect(!button(card, 'Complete connection') || button(card, 'Complete connection').disabled, 'timeout allows code reuse before reconciliation');
      expect(text(card).includes('checking whether the sign-in finished') && button(card, 'Checking connection…'), 'timeout reconciliation is presented as another exchange');
      reconciliation.resolve(connected); await settle();
      openSettings(card);
      expect(text(card).includes('current@example.com'), 'server-completed sign-in was not shown after timeout');
      expect(!card.shadowRoot.getElementById('chatgpt-callback'), 'connected timeout outcome retained callback');
      exchange.resolve({ success: false, code: 'network_error', login_restart_required: true }); await settle();
      expect(text(card).includes('current@example.com') && !text(card).includes('could not reach OpenAI'), 'late POST changed reconciled connection');
      card.disconnectedCallback();
    """)


def test_invalidated_account_can_be_explicitly_reset_before_signing_in_again():
    """Reconnect-required binding has a visible administrator reset route."""
    _run_component_runtime(r"""
      let resets = 0;
      const invalidated = { ...disconnected, status: 'reconnect_required' };
      const { card } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return notConfigured;
        if (method === 'GET') return invalidated;
        if (body.action === 'logout') { resets++; return disconnected; }
        if (body.action === 'start') { expect(resets === 1, 'account was replaced without explicit reset'); return login('replacement'); }
        throw new Error('Unexpected request');
      });
      expect(resets === 0, 'invalidated account was reset automatically');
      click(card, 'Reset ChatGPT account'); await settle();
      expect(resets === 1 && button(card, 'Continue with ChatGPT'), 'explicit reset did not offer a fresh sign-in');
      click(card, 'Continue with ChatGPT'); await settle();
      expect(card.shadowRoot.getElementById('chatgpt-callback'), 'fresh sign-in unavailable after reset');
      card.disconnectedCallback();
    """)


def test_local_logout_keeps_allowlisted_remote_cleanup_notice_visible_until_resolved():
    """Remote cleanup remains actionable outside closed settings and status refreshes."""
    _run_component_runtime(r"""
      const warning = 'Local credentials were removed. Remote disconnection could not be confirmed; disconnect PowerSync in ChatGPT Settings.';
      let isConnected = true, logoutCount = 0;
      const { card } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return isConnected ? ready : notConfigured;
        if (method === 'GET') return isConnected ? connected : { ...disconnected, warning: logoutCount <= 2 ? warning : undefined };
        if (body.action === 'logout') {
          isConnected = false; logoutCount++;
          return { ...disconnected, warning: logoutCount === 3 ? 'RAW-PROVIDER-SECRET' : warning };
        }
        if (body.action === 'start') return login('notice-login');
        if (body.action === 'complete') { isConnected = true; return connected; }
        throw new Error('Unexpected request');
      });
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      if (!card.shadowRoot.getElementById('connection-details').hidden) card.shadowRoot.getElementById('connection-toggle').listeners.click();
      expect(text(card).includes(warning), 'local logout discarded remote cleanup notice when settings closed');
      expect(visible(card.shadowRoot).some(node => node.tag === 'a' && node.textContent === 'Open ChatGPT' && node.href === 'https://chatgpt.com/'), 'remote cleanup action missing');
      card.disconnectedCallback(); card.connectedCallback(); await settle();
      expect(text(card).includes(warning), 'status refresh discarded unresolved remote cleanup');
      openSettings(card); click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('notice-login')); submitCallback(card); await settle();
      expect(!text(card).includes(warning), 'successful new account transition kept an obsolete cleanup notice');
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      click(card, 'Dismiss disconnection notice'); await settle();
      card.disconnectedCallback(); card.connectedCallback(); await settle();
      expect(!text(card).includes(warning), 'acknowledged notice returned after status refresh');
      openSettings(card); click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('notice-login')); submitCallback(card); await settle();
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      expect(!text(card).includes('RAW-PROVIDER-SECRET') && !text(card).includes(warning), 'unrecognized warning payload was displayed');
      expect(!nodes(card).some(node => node.attrs.role === 'log'), 'logout cleanup warning became activity history');
      card.disconnectedCallback();
    """)


def test_generation_cannot_restore_explanations_after_account_settings_or_lifecycle_changes():
    """Deferred explanation HTTP responses cannot cross a visible context change."""
    _run_component_runtime(r"""
      for (const transition of ['logout', 'provider', 'settings', 'model', 'admin', 'identity', 'disconnect']) {
        const generation = deferred(); let active = true;
        const handler = (method, route, body) => {
          if (route === 'power_sync/optimization/ai_summary') return generation.promise;
          if (route === 'power_sync/optimization') return Promise.resolve(active ? ready : notConfigured);
          if (method === 'GET') return Promise.resolve({ ...(active ? connected : disconnected), models: [{ id: 'new-model', name: 'New model' }] });
          if (body.action === 'logout') { active = false; return Promise.resolve(disconnected); }
          if (body.action === 'settings') return Promise.resolve({ ...connected, settings: { ...connected.settings, ...body } });
          throw new Error('Unexpected request');
        };
        const { card, hass } = await setup(handler);
        click(card, 'Generate explanation'); await settle();
        openSettings(card);
        if (transition === 'logout') click(card, 'Log out of ChatGPT');
        if (transition === 'provider' || transition === 'settings') {
          chooseProvider(card, 'openai');
          if (transition === 'settings') {
            input(card, 'openai-key', 'FIXTURE-API-KEY');
            const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
            form.listeners.submit({ preventDefault() {} });
          }
        }
        if (transition === 'model') {
          const select = card.shadowRoot.getElementById('chatgpt-model');
          select.value = 'new-model'; select.listeners.change();
        }
        if (transition === 'admin') card.hass = { ...hass, user: { is_admin: false } };
        if (transition === 'identity') card.hass = { ...hass, user: { id: 'different-administrator', is_admin: true } };
        if (transition === 'disconnect') { card.disconnectedCallback(); card.connectedCallback(); }
        await settle();
        generation.resolve({ success: true, summary: { headline: 'OLD ACCOUNT EXPLANATION', overview: 'This belongs to the previous request.' } });
        await settle();
        expect(!text(card).includes('OLD ACCOUNT EXPLANATION'), transition + ' restored old account explanation');
        card.disconnectedCallback();
      }
    """)


def test_late_summary_status_cannot_restore_cache_after_logout():
    """An older optimizer GET cannot restore a cached explanation after logout."""
    _run_component_runtime(r"""
      const oldStatus = deferred(); let statusReads = 0;
      const { card } = await setup((method, route, body) => {
        if (route === 'power_sync/optimization') return ++statusReads === 1 ? oldStatus.promise : Promise.resolve(notConfigured);
        if (method === 'GET') return Promise.resolve(connected);
        if (body.action === 'logout') return Promise.resolve(disconnected);
        throw new Error('Unexpected request');
      });
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      oldStatus.resolve({ features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready', summary: { headline: 'OLD CACHED ACCOUNT EXPLANATION' } } });
      await settle();
      const toggle = card.shadowRoot.getElementById('toggle');
      if (toggle.attrs['aria-label'] === 'Expand AI explanation') toggle.listeners.click();
      expect(!text(card).includes('OLD CACHED ACCOUNT EXPLANATION') && button(card, 'Check settings again'), 'old status GET restored cache after logout');
      card.disconnectedCallback();
    """)


def test_late_connection_get_cannot_replace_provider_draft_or_account_state():
    """An old account GET cannot refill fields after the administrator changes provider."""
    _run_component_runtime(r"""
      const oldConnection = deferred();
      const { card } = await setup((method, route) => route === 'power_sync/optimization' ? Promise.resolve(ready) : oldConnection.promise);
      openSettings(card); chooseProvider(card, 'openai'); await settle();
      oldConnection.resolve({ ...connected, account: { email: 'OLD-ACCOUNT@example.com' }, settings: { ...connected.settings, ai_summary_openai_model: 'OLD-ACCOUNT-MODEL' } });
      await settle();
      expect(card.shadowRoot.getElementById('ai-provider').value === 'openai' && card.shadowRoot.getElementById('openai-model').value !== 'OLD-ACCOUNT-MODEL', 'old connection GET refilled provider draft');
      chooseProvider(card, 'chatgpt');
      expect(!text(card).includes('OLD-ACCOUNT@example.com') && button(card, 'Continue with ChatGPT'), 'old connection GET restored account state');
      card.disconnectedCallback();
    """)


def test_late_settings_post_cannot_cross_administrator_identity_change():
    """A saved-settings reply from an earlier administrator cannot restore account fields."""
    _run_component_runtime(r"""
      const oldSettings = deferred(); let newAdministrator = false;
      const handler = (method, route, body) => {
        if (route === 'power_sync/optimization') return Promise.resolve(newAdministrator ? notConfigured : ready);
        if (method === 'GET') return Promise.resolve(newAdministrator ? disconnected : connected);
        if (body.action === 'settings') return oldSettings.promise;
        throw new Error('Unexpected request');
      };
      const { card, hass } = await setup(handler);
      openSettings(card); chooseProvider(card, 'openai'); input(card, 'openai-key', 'FIXTURE-API-KEY');
      const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
      form.listeners.submit({ preventDefault() {} }); await settle();
      newAdministrator = true;
      card.hass = { ...hass, user: { id: 'new-administrator', is_admin: true } }; await settle();
      oldSettings.resolve({ ...connected, account: { email: 'OLD-ADMIN@example.com' }, settings: { ai_summary_provider: 'openai', ai_summary_openai_model: 'OLD-ADMIN-MODEL', ai_summary_key_configured: true } });
      await settle(); openSettings(card);
      expect(card.shadowRoot.getElementById('ai-provider').value === 'chatgpt' && !text(card).includes('OLD-ADMIN@example.com') && !button(card, 'Log out of ChatGPT'), 'old settings POST crossed administrator identity change');
      card.disconnectedCallback();
    """)


def test_timeout_with_unavailable_status_requires_authoritative_check_before_retry():
    """An unavailable timeout check keeps completion disabled until live pending status is known."""
    _run_component_runtime(r"""
      const exchange = deferred(); let submitted = false, checked = 0, completed = 0;
      const { card } = await setup((method, route, body) => {
        if (route === 'power_sync/optimization') return Promise.resolve(ready);
        if (method === 'GET') {
          checked++;
          if (!submitted) return Promise.resolve(connected);
          if (checked === 2) return Promise.reject(new Error('RAW STATUS SECRET'));
          return Promise.resolve({ ...connected, pending: { login_id: 'unknown-login', expires_at: 'later' } });
        }
        if (body.action === 'start') return Promise.resolve(login('unknown-login'));
        if (body.action === 'complete') { submitted = true; completed++; return exchange.promise; }
        throw new Error('Unexpected request');
      });
      openSettings(card); click(card, 'Reconnect ChatGPT'); await settle();
      input(card, 'chatgpt-callback', callback('unknown-login')); submitCallback(card); await settle();
      expireTimer(90000); await settle();
      expect(button(card, 'Complete connection').disabled && button(card, 'Cancel sign-in').disabled && button(card, 'Check connection again') && !button(card, 'Check connection again').disabled, 'unknown server outcome allows callback reuse or has no status recovery');
      expect(completed === 1 && !text(card).includes('RAW STATUS SECRET'), 'status failure leaked payload or retried exchange');
      click(card, 'Check connection again'); await settle();
      expect(button(card, 'Complete connection') && !button(card, 'Complete connection').disabled && card.shadowRoot.getElementById('chatgpt-callback').value === callback('unknown-login'), 'usable server pending session did not restore correction controls');
      exchange.resolve({ success: false, code: 'login_expired', login_restart_required: true }); await settle();
      expect(button(card, 'Complete connection') && !button(card, 'Complete connection').disabled, 'late original response replaced the authoritative pending outcome');
      card.disconnectedCallback();
    """)


def test_account_mutation_disables_new_generation_until_server_reply():
    """An account mutation cannot start another explanation against the old identity."""
    _run_component_runtime(r"""
      const logout = deferred(); let generationRequests = 0;
      const { card } = await setup((method, route, body) => {
        if (route === 'power_sync/optimization') return Promise.resolve(ready);
        if (route === 'power_sync/optimization/ai_summary') { generationRequests++; return Promise.resolve({ success: true, summary: { headline: 'Unexpected generation' } }); }
        if (method === 'GET') return Promise.resolve(connected);
        if (body.action === 'logout') return logout.promise;
        throw new Error('Unexpected request');
      });
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      expect(button(card, 'Generate explanation')?.disabled, 'account mutation leaves generation enabled');
      expect(generationRequests === 0, 'account mutation started generation');
      logout.resolve(disconnected); await settle(); card.disconnectedCallback();
    """)


def test_hass_tick_during_account_mutation_cannot_restore_pretransition_status():
    """A new optimizer GET must wait until the pending account mutation finishes."""
    _run_component_runtime(r"""
      for (const action of ['logout', 'settings', 'complete']) {
        const mutation = deferred(), racingStatus = deferred();
        let pending = false, finished = false, statusReads = 0;
        const handler = (method, route, body) => {
          if (route === 'power_sync/optimization') {
            statusReads++;
            if (pending && !finished) return racingStatus.promise;
            return Promise.resolve(finished && action === 'logout' ? notConfigured : ready);
          }
          if (method === 'GET') return Promise.resolve({ ...connected, models: [{ id: 'new-model', name: 'New model' }] });
          if (body.action === 'start') return Promise.resolve(login('mutation-login'));
          pending = true; return mutation.promise;
        };
        const { card, hass } = await setup(handler);
        openSettings(card);
        if (action === 'logout') click(card, 'Log out of ChatGPT');
        if (action === 'settings') {
          const select = card.shadowRoot.getElementById('chatgpt-model'); select.value = 'new-model'; select.listeners.change();
        }
        if (action === 'complete') {
          click(card, 'Reconnect ChatGPT'); await settle();
          input(card, 'chatgpt-callback', callback('mutation-login')); submitCallback(card);
        }
        await settle();
        card.hass = { ...hass }; await settle();
        finished = true; mutation.resolve(action === 'logout' ? disconnected : connected); await settle();
        racingStatus.resolve({ features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready', summary: { headline: 'PRETRANSITION ACCOUNT CACHE' } } });
        await settle();
        const toggle = card.shadowRoot.getElementById('toggle');
        if (toggle.attrs['aria-label'] === 'Expand AI explanation') toggle.listeners.click();
        expect(!text(card).includes('PRETRANSITION ACCOUNT CACHE'), action + ' allowed a hass tick to restore pretransition status');
        expect(statusReads === 2 && button(card, action === 'logout' ? 'Check settings again' : 'Generate explanation'), action + ' did not perform a fresh postmutation status read');
        card.disconnectedCallback();
      }
    """)


def test_hass_tick_during_provider_save_cannot_steal_connection_action_or_stall_controls():
    """Connection refreshes must wait for the settings POST and release its controls."""
    _run_component_runtime(r"""
      const saving = deferred(), racingConnection = deferred();
      const saved = { ...connected, settings: { ai_summary_provider: 'openai', ai_summary_openai_model: 'saved-model', ai_summary_api_key_configured: true, ai_summary_key_configured: true } };
      let pending = false, finished = false, connectionReads = 0;
      const handler = (method, route, body) => {
        if (route === 'power_sync/optimization') return Promise.resolve(ready);
        if (method === 'GET') {
          connectionReads++;
          if (pending && !finished) return racingConnection.promise;
          return Promise.resolve(finished ? saved : connected);
        }
        if (body.action === 'settings') { pending = true; return saving.promise; }
        throw new Error('Unexpected request');
      };
      const { card, hass } = await setup(handler);
      openSettings(card); chooseProvider(card, 'openai'); input(card, 'openai-key', 'FIXTURE-API-KEY');
      input(card, 'openai-model', 'submitted-model');
      const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
      form.listeners.submit({ preventDefault() {} }); await settle();
      card.hass = { ...hass }; await settle();
      finished = true; saving.resolve(saved); await settle();
      racingConnection.resolve(connected); await settle(); openSettings(card);
      expect(button(card, 'Save AI settings') && !button(card, 'Save AI settings').disabled, 'hass tick stole the settings action and left controls busy');
      expect(card.shadowRoot.getElementById('openai-key').value === '' && card.shadowRoot.getElementById('openai-model').value === 'saved-model', 'settings success was replaced by pretransition connection fields');
      expect(connectionReads === 1, 'connection refresh duplicated the authoritative settings POST');
      card.disconnectedCallback();
    """)


def test_callback_requires_registered_loopback_address_and_remains_correctable():
    """The UI must reject localhost before POST and name the registered redirect address."""
    _run_component_runtime(r"""
      let completions = 0;
      const { card } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return notConfigured;
        if (method === 'GET') return disconnected;
        if (body.action === 'start') return login('registered-address');
        if (body.action === 'complete') { completions++; return { success: false, code: 'invalid_callback', login_restart_required: false }; }
        throw new Error('Unexpected request');
      });
      click(card, 'Continue with ChatGPT'); await settle();
      const wrongAddress = 'http://localhost:1455/auth/callback?code=FIXTURE-SECRET&state=fixture-registered-address';
      input(card, 'chatgpt-callback', wrongAddress); submitCallback(card); await settle();
      expect(completions === 0, 'localhost callback was sent to the strict registered-address backend');
      expect(card.shadowRoot.getElementById('chatgpt-callback').value === wrongAddress && !button(card, 'Complete connection').disabled, 'rejected callback was not editable for correction');
      expect(text(card).includes('http://127.0.0.1:1455/auth/callback') && !text(card).includes('FIXTURE-SECRET') && !text(card).includes('localhost'), 'callback help does not safely name the registered final address');
      for (const normalizedAddress of ['http://127.1:1455/auth/callback?code=FIXTURE-SECRET&state=fixture-registered-address', 'http://127.0.0.1:1455/auth/../auth/callback?code=FIXTURE-SECRET&state=fixture-registered-address']) {
        input(card, 'chatgpt-callback', normalizedAddress); submitCallback(card); await settle();
        expect(completions === 0, 'normalized callback alias was sent to the exact registered-address backend');
        expect(card.shadowRoot.getElementById('chatgpt-callback').value === normalizedAddress, 'alias rejection discarded the correctable input');
      }
      input(card, 'chatgpt-callback', callback('registered-address')); submitCallback(card); await settle();
      expect(completions === 1 && card.shadowRoot.getElementById('chatgpt-callback'), 'corrected registered callback could not be submitted');
      card.disconnectedCallback();
    """)


def test_unknown_mutation_timeout_waits_for_connection_and_preserves_remote_notice():
    """Logout/settings timeouts keep generation fenced until authoritative connection status."""
    _run_component_runtime(r"""
      const warning = 'Local credentials were removed. Remote disconnection could not be confirmed; disconnect PowerSync in ChatGPT Settings.';
      for (const action of ['logout', 'settings']) {
        const mutation = deferred(), reconciliation = deferred();
        const saved = { ...connected, warning, settings: { ai_summary_provider: 'openai', ai_summary_openai_model: 'saved-model', ai_summary_api_key_configured: true, ai_summary_key_configured: true } };
        let pending = false, finished = false, connectionReads = 0;
        const handler = (method, route, body) => {
          if (route === 'power_sync/optimization') return Promise.resolve(finished && action === 'logout' ? notConfigured : ready);
          if (method === 'GET') { connectionReads++; return pending && !finished ? reconciliation.promise : Promise.resolve(finished ? action === 'logout' ? { ...disconnected, warning } : saved : connected); }
          pending = true; return mutation.promise;
        };
        const { card } = await setup(handler);
        openSettings(card);
        if (action === 'logout') click(card, 'Log out of ChatGPT');
        else {
          chooseProvider(card, 'openai'); input(card, 'openai-key', 'FIXTURE-API-KEY');
          const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
          form.listeners.submit({ preventDefault() {} });
        }
        await settle(); expireTimer(90000); await settle();
        expect(connectionReads === 2 && (!button(card, 'Generate explanation') || button(card, 'Generate explanation').disabled), action + ' timeout released generation before authoritative reconciliation');
        finished = true; reconciliation.resolve(action === 'logout' ? { ...disconnected, warning } : saved); await settle(); openSettings(card);
        expect(text(card).includes(warning), action + ' reconciliation discarded the current remote cleanup notice');
        expect(action === 'logout' ? button(card, 'Check settings again') && !button(card, 'Generate explanation') : button(card, 'Generate explanation') && !button(card, 'Generate explanation').disabled, action + ' reconciliation did not release the correct explanation state');
        mutation.resolve(connected); await settle();
        expect(text(card).includes(warning) && (action !== 'settings' || card.shadowRoot.getElementById('ai-provider').value === 'openai'), action + ' late POST replaced authoritative connection state');
        click(card, 'Dismiss disconnection notice'); await settle();
        card.disconnectedCallback(); card.connectedCallback(); await settle();
        expect(!text(card).includes(warning), action + ' acknowledged GET notice returned after reattachment');
        card.disconnectedCallback();
      }
    """)


def test_unknown_mutation_with_failed_status_keeps_generation_fenced_until_check():
    """A failed timeout GET leaves a working check route and no old-identity generation."""
    _run_component_runtime(r"""
      const warning = 'Local credentials were removed. Remote disconnection could not be confirmed; disconnect PowerSync in ChatGPT Settings.';
      for (const action of ['logout', 'settings']) {
        const mutation = deferred(); let pending = false, connectionReads = 0, statusReads = 0;
        const handler = (method, route) => {
          if (route === 'power_sync/optimization') { statusReads++; return Promise.resolve(ready); }
          if (method === 'GET') {
            connectionReads++;
            if (pending && connectionReads === 2) return Promise.reject(new Error('RAW STATUS SECRET'));
            return Promise.resolve({ ...connected, warning });
          }
          pending = true; return mutation.promise;
        };
        const { card } = await setup(handler);
        openSettings(card);
        if (action === 'logout') click(card, 'Log out of ChatGPT');
        else {
          chooseProvider(card, 'openai'); input(card, 'openai-key', 'FIXTURE-API-KEY');
          const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'openai-key'));
          form.listeners.submit({ preventDefault() {} });
        }
        await settle(); expireTimer(90000); await settle();
        expect(statusReads === 1 && button(card, 'Generate explanation')?.disabled && button(card, 'Check connection again') && !button(card, 'Check connection again').disabled, action + ' unknown outcome allows generation or has no live check');
        click(card, 'Check connection again'); await settle(); openSettings(card);
        expect(button(card, 'Generate explanation') && !button(card, 'Generate explanation').disabled && text(card).includes(warning), action + ' check stalled generation or dismissed remote notice merely because connected');
        expect(!text(card).includes('RAW STATUS SECRET'), 'unknown status exposed provider details');
        mutation.resolve({ ...disconnected, warning: 'RAW LATE SECRET' }); await settle();
        expect(button(card, 'Generate explanation') && !button(card, 'Generate explanation').disabled && !text(card).includes('RAW LATE SECRET'), action + ' late POST replaced the checked outcome');
        card.disconnectedCallback();
      }
    """)


def test_reattachment_during_logout_waits_for_connection_barrier_and_refreshes_summary():
    """A reattached card must replace pre-logout cache with live post-logout status."""
    _run_component_runtime(r"""
      const logout = deferred(), connectionBarrier = deferred(), oldStatus = deferred();
      let pending = false, finished = false, freshReads = 0;
      const oldCache = { features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready', summary: { headline: 'OLD REATTACHED ACCOUNT CACHE' } } };
      const handler = (method, route, body) => {
        if (route === 'power_sync/optimization') {
          if (!pending) return Promise.resolve(ready);
          if (!finished) return oldStatus.promise;
          freshReads++; return Promise.resolve(notConfigured);
        }
        if (method === 'GET') return pending && !finished ? connectionBarrier.promise : Promise.resolve(finished ? disconnected : connected);
        if (body.action === 'logout') { pending = true; return logout.promise; }
        throw new Error('Unexpected request');
      };
      const { card, hass } = await setup(handler);
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      card.disconnectedCallback(); card.connectedCallback(); card.hass = { ...hass }; await settle();
      finished = true; logout.resolve(disconnected); connectionBarrier.resolve(disconnected); await settle();
      oldStatus.resolve(oldCache); await settle();
      const toggle = card.shadowRoot.getElementById('toggle');
      if (toggle.attrs['aria-label'] === 'Expand AI explanation') toggle.listeners.click();
      expect(!text(card).includes('OLD REATTACHED ACCOUNT CACHE'), 'reattachment restored pre-logout cache after authoritative disconnected status');
      openSettings(card);
      expect(freshReads === 1 && button(card, 'Check settings again') && button(card, 'Continue with ChatGPT'), 'reattachment did not fetch actual post-logout summary availability');
      card.disconnectedCallback();
    """)


def test_authoritative_connection_read_refreshes_summary_when_provider_flags_are_unchanged():
    """Equal provider/key flags must not leave the previous key's cached explanation."""
    _run_component_runtime(r"""
      const connectionBarrier = deferred(), oldStatus = deferred();
      const accountStatus = { ...disconnected, settings: { ai_summary_provider: 'openai', ai_summary_openai_model: 'same-model', ai_summary_api_key_configured: true, ai_summary_key_configured: true } };
      const cache = headline => ({ features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready', summary: { provider: 'openai', headline } } });
      let changed = false, barrierFinished = false, freshReads = 0;
      const { card, hass } = await setup((method, route) => {
        if (route === 'power_sync/optimization') {
          if (!changed) return Promise.resolve(cache('PREVIOUS KEY EXPLANATION'));
          if (!barrierFinished) return oldStatus.promise;
          freshReads++; return Promise.resolve(cache('CURRENT KEY EXPLANATION'));
        }
        return changed && !barrierFinished ? connectionBarrier.promise : Promise.resolve(accountStatus);
      });
      openSettings(card); chooseProvider(card, 'gemini');
      changed = true; card.disconnectedCallback(); card.connectedCallback(); card.hass = { ...hass }; await settle();
      barrierFinished = true; connectionBarrier.resolve(accountStatus); await settle();
      oldStatus.resolve(cache('PREVIOUS KEY EXPLANATION')); await settle();
      const toggle = card.shadowRoot.getElementById('toggle');
      if (toggle.attrs['aria-label'] === 'Expand AI explanation') toggle.listeners.click();
      expect(text(card).includes('CURRENT KEY EXPLANATION') && !text(card).includes('PREVIOUS KEY EXPLANATION') && freshReads === 1, 'equal provider flags prevented authoritative summary refresh');
      openSettings(card);
      expect(card.shadowRoot.getElementById('ai-provider').value === 'gemini', 'authoritative refresh overwrote the provider draft');
      card.disconnectedCallback();
    """)


def test_account_status_failure_does_not_block_existing_non_chatgpt_explanation():
    """A failing account status endpoint must not hide an already configured Gemini summary."""
    _run_component_runtime(r"""
      const { card } = await setup((method, route) => {
        if (route === 'power_sync/optimization') return Promise.resolve({ features: { ai_summary: true }, ai_summary: { configured: true, state: 'ready', summary: { provider: 'gemini', headline: 'EXISTING GEMINI EXPLANATION' } } });
        return Promise.reject(new Error('RAW ACCOUNT STORAGE SECRET'));
      });
      const toggle = card.shadowRoot.getElementById('toggle');
      if (toggle.attrs['aria-label'] === 'Expand AI explanation') toggle.listeners.click();
      expect(text(card).includes('EXISTING GEMINI EXPLANATION') && button(card, 'Refresh explanation') && !text(card).includes('RAW ACCOUNT STORAGE SECRET'), 'account status failure blocked the existing provider explanation');
      card.disconnectedCallback();
    """)


def test_provider_status_refresh_preserves_draft_fields_until_successful_save():
    """HA ticks may refresh connection status without discarding unsaved settings."""
    _run_component_runtime(r"""
      let reads = 0;
      const saved = { ...disconnected, settings: {
        ai_summary_provider: 'gemini', ai_summary_local_endpoint: '', ai_summary_local_model: '',
        ai_summary_openai_model: 'old-api-model', ai_summary_openrouter_model: 'old-router-model'
      } };
      const { card, hass } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return notConfigured;
        if (method === 'GET') { reads++; return saved; }
        if (body.action === 'settings') return { ...saved, settings: { ...body,
          ai_summary_local_model: 'canonical-saved-model', ai_summary_key_configured: true } };
        throw new Error('Unexpected request');
      });
      openSettings(card);
      chooseProvider(card, 'local_openai_compatible');
      input(card, 'local-endpoint', 'http://192.168.1.10:11434/v1');
      input(card, 'local-model', 'draft-model');
      card.hass = { ...hass }; await settle();
      expect(reads === 2, 'test did not refresh status after changing provider');
      expect(card.shadowRoot.getElementById('local-endpoint').value === 'http://192.168.1.10:11434/v1', 'status refresh erased endpoint draft');
      expect(card.shadowRoot.getElementById('local-model').value === 'draft-model', 'status refresh erased model draft');
      const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'local-endpoint'));
      form.listeners.submit({ preventDefault() {} }); await settle(); openSettings(card);
      expect(card.shadowRoot.getElementById('local-model').value === 'canonical-saved-model', 'successful save did not hydrate authoritative fields');
      expect(card._errorText('provider_unavailable').includes('unavailable') && !card._errorText('provider_unavailable').includes('Reconnect'), 'temporary provider failure asks for reconnection');
      card.disconnectedCallback();
    """)


def test_status_hydrates_untouched_fields_and_preserves_each_edited_field():
    """Each editable setting independently follows status until locally edited."""
    _run_component_runtime(r"""
      const fields = [
        ['openai', 'openai-model', 'ai_summary_openai_model'],
        ['openrouter', 'openrouter-model', 'ai_summary_openrouter_model'],
        ['local_openai_compatible', 'local-endpoint', 'ai_summary_local_endpoint'],
        ['local_openai_compatible', 'local-model', 'ai_summary_local_model'],
      ];
      for (const [provider, id, setting] of fields) {
        let settings = { ai_summary_provider: provider,
          ai_summary_openai_model: 'initial-api', ai_summary_openrouter_model: 'initial-router',
          ai_summary_local_endpoint: 'http://initial:11434/v1', ai_summary_local_model: 'initial-local' };
        const { card, hass } = await setup((method, route) => Promise.resolve(
          route === 'power_sync/optimization' ? notConfigured : { ...disconnected, settings }));
        openSettings(card);
        settings = { ...settings, [setting]: 'external-first' };
        card._connectionRequested = false; card.hass = { ...hass }; await settle();
        expect(card.shadowRoot.getElementById(id).value === 'external-first', id + ' failed to hydrate untouched field');
        input(card, id, '');
        settings = { ...settings, [setting]: 'external-second', ai_summary_openrouter_model: 'updated-router', ai_summary_local_model: 'updated-local' };
        card._connectionRequested = false; card.hass = { ...hass }; await settle();
        expect(card.shadowRoot.getElementById(id).value === '', id + ' discarded edited empty value');
        if (id === 'local-endpoint') expect(card.shadowRoot.getElementById('local-model').value === 'updated-local', 'edited endpoint froze untouched local model');
        expect(card._errorText('storage_unavailable').includes('Home Assistant') && !card._errorText('storage_unavailable').includes('Reconnect'), 'storage failure lacked actionable local message');
        card.disconnectedCallback();
      }
    """)


def test_local_timeout_save_and_summary_watchdog_follow_server_budget():
    _run_component_runtime(r"""
      let savedTimeout;
      const pending = deferred();
      const local = { ...disconnected, settings: { ai_summary_provider: 'local_openai_compatible',
        ai_summary_local_endpoint: 'http://127.0.0.1:11434/v1', ai_summary_local_model: 'local-model',
        ai_summary_local_timeout: 120, ai_summary_client_timeout_seconds: 125, ai_summary_key_configured: true } };
      const { card } = await setup(async (method, route, body) => {
        if (route === 'power_sync/optimization') return { ...ready, ai_summary: { ...ready.ai_summary, client_timeout_seconds: 125 } };
        if (route === 'power_sync/optimization/ai_summary') return pending.promise;
        if (method === 'GET') return local;
        savedTimeout = body.ai_summary_local_timeout;
        return { ...local, settings: { ...local.settings, ai_summary_local_timeout: savedTimeout,
          ai_summary_client_timeout_seconds: savedTimeout + 5 } };
      });
      openSettings(card);
      const timeout = card.shadowRoot.getElementById('local-timeout');
      expect(timeout.value == 120 && timeout.min === '30' && timeout.max === '600', 'timeout bounds/default missing');
      input(card, 'local-timeout', '240');
      const form = nodes(card).find(node => node.tag === 'form' && node.children.some(child => child.id === 'local-timeout'));
      form.listeners.submit({ preventDefault() {} }); await settle();
      expect(savedTimeout === 240, 'saved timeout was not a number');
      const generation = card._generate(true); await settle();
      expect([...timers.values()].some(timer => timer.delay === 245000), 'card timed out before local server budget');
      pending.resolve({ success: true, summary: { headline: 'Local model finished' } }); await generation;
      expect(card._state === 'content' && ![...timers.values()].some(timer => timer.delay === 245000), 'success did not clear timeout');
      card.disconnectedCallback();
      // Non-admin readers receive the same budget from the public plan status.
      const wait = deferred();
      const { card: reader } = await setup(async (method, route) => route === 'power_sync/optimization'
        ? { ...ready, ai_summary: { ...ready.ai_summary, client_timeout_seconds: 605 } }
        : wait.promise, false);
      const task = reader._generate(true); await settle();
      expect([...timers.values()].some(timer => timer.delay === 605000), 'non-admin reader lost configured timeout');
      wait.resolve({ success: true, summary: { headline: 'Done' } }); await task;
      reader.disconnectedCallback();
    """)


def test_ai_explanation_details_hide_raw_readings_and_keep_aggregate_windows():
    """The default view stays short for both cached forecast representations."""
    runtime_checks = r"""
      const fs = require('fs');
      const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const start = source.indexOf('class PowerSyncAIPlanExplanation');
      const end = source.indexOf("if (!customElements.get('power-sync-ai-plan-explanation'))", start);
      class Element {
        constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; }
        append(...nodes) { this.children.push(...nodes); }
        addEventListener(name, listener) { this.listeners[name] = listener; }
        get childElementCount() { return this.children.length; }
      }
      class HTMLElement extends Element { attachShadow() { this.shadowRoot = new Element(); } }
      const context = vm.createContext({ HTMLElement, document: { createElement: tag => new Element(tag) }, Date });
      vm.runInContext(source.slice(start, end) + '\nthis.Card = PowerSyncAIPlanExplanation;', context);
      const card = new context.Card();
      card._hass = { config: { time_zone: 'Australia/Brisbane' }, callApi() { throw Error('Details requested provider usage'); } };
      card._meta = { stale: true };
      const rawWindow = {
        start: '2026-10-24T18:00:00+10:00', end: '2026-10-24T20:00:00+10:00', action: 'export',
        values: Array.from({ length: 24 }, (_, index) => ({
          timestamp: new Date(Date.parse('2026-10-24T18:00:00+10:00') + index * 300000).toISOString(), solar_kw: 0, load_kw: 1 + index / 100,
        })),
      };
      const aggregated = { ...rawWindow, values: undefined, summary: {
        slot_count: 25, solar_kw: { min: 0, average: 1, max: 2 }, load_kw: { min: 1, average: 2, max: 3 },
      }};
      card._summary = { headline: 'A concise plan', now: 'Use stored energy.', next: 'Charge at 10 am.',
        why_it_matters: 'Lower import prices.', expected_outcome: 'AUD 1.04 more than baseline.',
        provider: 'gemini', important_actions: [{ ...rawWindow, reason: 'A material change.' }],
        caveats: ['A material caveat.'], forecast_evidence: { status: 'available', windows: [rawWindow, aggregated] },
      };
      const flatten = node => [node, ...node.children.flatMap(flatten)];
      const visible = node => node.textContent + ' ' + (node.tag === 'details' && !node.open
        ? node.children.filter(child => child.tag === 'summary') : node.children).map(visible).join(' ');
      const expect = (value, message) => { if (!value) throw Error(message); };
      let body = new Element();
      card._renderContent(body);
      let details = flatten(body).find(node => node.className === 'explanation-details');
      expect(details && !details.open, 'Details must start collapsed');
      expect(/older plan/.test(visible(body)), 'stale notice was hidden');
      expect(/material caveat/.test(visible(body)), 'material caveat was hidden');
      expect(!/Forecast solar|A material change|solar 0.00/.test(visible(body)), 'default view dumped evidence');
      details.open = true; details.listeners.toggle();
      expect(/A material change/.test(visible(body)), 'timeline was lost');
      expect(!/Solar 0 kW|home use|average 1/.test(visible(body)), 'opening Details exposed every forecast window');
      const windows = flatten(details).find(node => node.className === 'forecast-windows');
      expect(windows && !windows.open, 'forecast windows must start collapsed');
      expect(/Forecast windows \(2\)/.test(visible(body)), 'forecast count was lost');
      windows.open = true; windows.listeners.toggle();
      expect(/Solar 1 kW · Home 2 kW/.test(visible(body)), '25-slot aggregate average was omitted');
      expect(/Solar 0 kW · Home 1.1 kW/.test(visible(body)), '24-slot raw average was omitted');
      expect(!/average 1|0.0–2.0/.test(visible(body)), 'compact rows exposed ranges');
      const window = flatten(windows).find(node => node.className === 'forecast-window');
      window.open = true;
      expect(/solar 0.0–0.0/.test(visible(body)), 'explicit window range was lost');
      expect(!/solar 0.00/.test(visible(body)), 'opening Details dumped raw readings');
      const raw = flatten(window).find(node => node.tag === 'details' && node !== window);
      raw.open = true;
      expect(flatten(raw).filter(node => node.tag === 'li').length === 24, 'on-demand readings were lost');
      expect(/solar 0.00/.test(visible(body)), 'explicit raw evidence could not be shown');
      body = new Element(); card._renderContent(body);
      expect(flatten(body).find(node => node.className === 'explanation-details').open, 'Details state was lost on render');
      expect(flatten(body).find(node => node.className === 'forecast-windows').open, 'forecast disclosure state was lost');
      expect(card._forecastSummaryText(aggregated).split(/\s+/).length <= 10, 'compact forecast row is too verbose');
      card._summary.forecast_evidence = { status: 'changed', windows: [rawWindow] };
      card._forecastWindowsExpanded = false;
      body = new Element(); card._renderContent(body);
      expect(/Forecast data changed/.test(visible(body)), 'changed-data notice was hidden with windows');
      card._summary.forecast_evidence = { status: 'available', windows: Array.from({ length: 24 }, () => aggregated) };
      body = new Element(); card._renderContent(body);
      expect(/Forecast windows \(24\)/.test(visible(body)), 'full-horizon count was lost');
      expect(!/Solar 1 kW|0.0–2.0/.test(visible(body)), 'full-horizon forecasts produced a wall of text');
      expect(card._forecastSummaryText({ values: [{ solar_kw: null, load_kw: null }] }) === null, 'null telemetry became zero');
      card._summary.forecast_evidence = { status: 'unavailable', windows: [] };
      body = new Element(); card._renderContent(body);
      expect(/unavailable/.test(visible(body)), 'unavailable evidence notice was lost');
    """
    result = subprocess.run(
        ["node", "-e", runtime_checks, str(STRATEGY_PATH)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
