"""Exercise the consent UI with an inert DOM and the real callback wiring."""
from pathlib import Path
import subprocess


def test_support_card_requires_admin_and_reviewed_scope_before_confirmation():
    path = Path(__file__).parents[1] / 'custom_components/power_sync/frontend/power-sync-support.js'
    script = r'''
    const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
    class Node {
      constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.value = ''; }
      append(e) { this.children.push(e); }
      replaceChildren() { this.children = []; }
      setAttribute(k,v) { this.attributes[k] = v; }
      querySelectorAll() { return this.children.flatMap(x=>[x,...x.querySelectorAll()]); }
      focus() { this.focused = true; }
      select() { this.selected = true; }
      set innerHTML(_) { throw Error('unsafe HTML rendering'); }
    }
    class Element { attachShadow() { return this.shadowRoot = new Node('shadow'); } }
    const registry = new Map(), timers = new Map();let timerId=0;
    const context = { HTMLElement: Element, document: {createElement: tag=>new Node(tag)}, customElements: {get:k=>registry.get(k),define:(k,v)=>registry.set(k,v)}, Date, JSON, URL, Blob,
      setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),navigator:{clipboard:{writeText:async()=>{throw Error('clipboard unavailable');}}} };
    vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
    (async()=>{
    const Card=registry.get('ps-support-card'),card=new Card();card._hass={user:{is_admin:false}};card.render();
    assert.equal(card.shadowRoot.querySelectorAll().filter(x=>x.tag==='button').length,0,'non-admin has no consent controls');
    const canary='<img src=x onerror=alert(1)>',calls=[];
    card.diagnosticsApi=async()=>({grants:[]});card._diagnostics={grants:[]};card._diagnosticsError=false;card._ongoing=false;
    card._hass={user:{is_admin:true}};card._entry='entry-one';card._entries=[{entry_id:'entry-one'}];
    card._status={state:'pending',invitation:{actor:canary,ticket:'discord:1234',phrase:'VISIBLE'},descriptor_digest:'DISPLAYED_DIGEST',policy:{disclosure:canary}};
    card.run=(...args)=>calls.push(args);card.render();
    let nodes=card.shadowRoot.querySelectorAll();
    assert.ok(nodes.some(x=>x.textContent===canary),'untrusted disclosure remains inert text');
    const approve=nodes.find(x=>x.textContent==='Approve read-only access');assert.equal(approve.disabled,true);
    const consent=nodes.find(x=>x.tag==='label'&&x.children.some(c=>c.textContent?.startsWith('I checked'))).children[0];
    consent.checked=true;consent.onchange();assert.equal(approve.disabled,false);
    approve.onclick();assert.equal(calls[0][0],'confirm');assert.equal(calls[0][1].descriptor_digest,'DISPLAYED_DIGEST');
    assert.ok(nodes.some(x=>x.tag==='details'&&x.open&&x.children.some(c=>c.textContent==='Privacy and processing policy')),'policy opens before consent');
    assert.ok(!nodes.some(x=>x.tag==='textarea'),'no customer question surface');
    // Unchanged background reads and independent recorder activity keep the form and focus intact.
    card._refreshError=false;
    card.api=async()=>({entries:[{entry_id:'entry-one',status:{...card._status,recorder_bytes:123,access_receipt:[{}]}}]});
    await card.load(true);assert.ok(card.shadowRoot.querySelectorAll().includes(consent));assert.equal(consent.checked,true);
    card.api=async()=>({entries:[{entry_id:'entry-one',status:{...card._status,descriptor_digest:'CHANGED_DIGEST',invitation:{actor:'new investigator',ticket:'discord:1234',phrase:'NEW'}}}]});
    await card.load(true);nodes=card.shadowRoot.querySelectorAll();
    assert.equal(nodes.find(x=>x.textContent==='Approve read-only access').disabled,true,'changed identity requires fresh approval');
    card._status={state:'closed',archive_expires_at:1800000000,archive_pending:true};card.render();
    nodes=card.shadowRoot.querySelectorAll();assert.ok(nodes.some(x=>x.textContent==='Revoke shared archive'),'archive remains revocable after live closure');
    const invitation=nodes.find(x=>x.tag==='input'&&x.attributes['aria-label']==='Support invitation');
    assert.ok(invitation,'closed status restores invitation input after registration failure');
    const start=nodes.find(x=>x.textContent==='Get pairing code');assert.equal(start.disabled,true,'empty invitation cannot start');
    invitation.value='INVITATION';invitation.oninput();assert.equal(start.disabled,false);
    const categories=nodes.filter(x=>x.tag==='details'&&x.children.some(c=>c.textContent==='Choose what to share'))[0].querySelectorAll().filter(x=>x.type==='checkbox').slice(0,6);
    categories.forEach(c=>{c.checked=false;c.onchange();});assert.equal(start.disabled,true,'empty sharing scope cannot start');
    categories[0].checked=true;categories[0].onchange();assert.equal(start.disabled,false);
    card._status={state:'pending',code:'PAIRING'};card.render();nodes=card.shadowRoot.querySelectorAll();
    const code=nodes.find(x=>x.tag==='input'&&x.attributes['aria-label']==='Pairing code');
    assert.equal(code.value,'PAIRING');assert.equal(code.readOnly,true,'code is selectable without granting edit access');
    await nodes.find(x=>x.textContent==='Copy pairing code').onclick();assert.ok(code.focused&&code.selected,'clipboard failure selects the code for manual copy');
    assert.ok(!nodes.some(x=>x.textContent==='Approve read-only access'),'a code alone never permits approval');
    card.connectedCallback();assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,5000);
    card.disconnectedCallback();assert.equal(timers.size,0,'leaving the card cancels background refresh');
    card.api=async()=>{throw Error('offline');};await card.load(true);
    assert.ok(card.shadowRoot.querySelectorAll().some(x=>x.attributes.role==='alert'),'refresh failure is visible');
    card.api=async()=>({entries:[{entry_id:'entry-one',status:card._status}]});await card.load(true);
    assert.ok(!card.shadowRoot.querySelectorAll().some(x=>x.attributes.role==='alert'),'recovery clears the refresh warning');
    card._status={state:'active',scope:{categories:['system'],history_start:1700000000,expires_at:1800000000}};card.render();nodes=card.shadowRoot.querySelectorAll();
    assert.ok(nodes.some(x=>x.textContent==='Stop support access'));assert.ok(!nodes.some(x=>x.attributes['aria-label']==='Pairing code'),'active session has no old code');
    card._status={state:'closed',reason:'pairing_expired'};card.render();
    assert.ok(card.shadowRoot.querySelectorAll().some(x=>x.textContent?.includes('Ask support for a fresh invitation')),'expired setup explains recovery');
    card._entry=undefined;card._entries=[];card.render();
    assert.equal(card.shadowRoot.querySelectorAll().find(x=>x.textContent==='Refresh status').disabled,false,'failed initial discovery can be retried');
    })().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    subprocess.run(['node', '-e', script, str(path)], check=True)
