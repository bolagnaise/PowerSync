"""Exercise the consent UI with an inert DOM and the real callback wiring."""
from pathlib import Path
import subprocess


def test_support_card_requires_admin_and_reviewed_scope_before_confirmation():
    path = Path(__file__).parents[1] / 'custom_components/power_sync/frontend/power-sync-support.js'
    script = r'''
    const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
    class Node {
      constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
      append(e) { this.children.push(e); }
      replaceChildren() { this.children = []; }
      setAttribute(k,v) { this.attributes[k] = v; }
      querySelectorAll() { return this.children.flatMap(x=>[x,...x.querySelectorAll()]); }
      set innerHTML(_) { throw Error('unsafe HTML rendering'); }
    }
    class Element { attachShadow() { return this.shadowRoot = new Node('shadow'); } }
    const registry = new Map();
    const context = { HTMLElement: Element, document: {createElement: tag=>new Node(tag)}, customElements: {get:k=>registry.get(k),define:(k,v)=>registry.set(k,v)}, Date, JSON, URL, Blob, setTimeout };
    vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
    const Card=registry.get('ps-support-card'),card=new Card();card._hass={user:{is_admin:false}};card.render();
    assert.equal(card.shadowRoot.querySelectorAll().filter(x=>x.tag==='button').length,0,'non-admin has no consent controls');
    const canary='<img src=x onerror=alert(1)>',calls=[];
    card._hass={user:{is_admin:true}};card._entry='entry-one';card._entries=[{entry_id:'entry-one'}];
    card._status={state:'pending',invitation:{actor:canary,ticket:'discord:1234',phrase:'VISIBLE'},descriptor_digest:'DISPLAYED_DIGEST',policy:{disclosure:canary}};
    card.run=(...args)=>calls.push(args);card.render();
    let nodes=card.shadowRoot.querySelectorAll();
    assert.ok(nodes.some(x=>x.textContent?.includes(canary)),'untrusted disclosure remains inert text');
    const approve=nodes.find(x=>x.textContent==='Approve read-only access');assert.equal(approve.disabled,true);
    const consent=nodes.filter(x=>x.tag==='input'&&x.type==='checkbox').at(-1);consent.checked=true;consent.onchange();assert.equal(approve.disabled,false);
    approve.onclick();assert.equal(calls[0][0],'confirm');assert.equal(calls[0][1].descriptor_digest,'DISPLAYED_DIGEST');
    assert.ok(!nodes.some(x=>x.tag==='textarea'),'no customer question surface');
    card._status={state:'closed',archive_expires_at:1800000000,archive_pending:true};card.render();
    nodes=card.shadowRoot.querySelectorAll();assert.ok(nodes.some(x=>x.textContent==='Revoke shared archive'),'archive remains revocable after live closure');
    '''
    subprocess.run(['node', '-e', script, str(path)], check=True)
