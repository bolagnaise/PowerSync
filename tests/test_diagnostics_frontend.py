"""The actual card wires one confirmation and one grouped revoke, with inert text."""
from pathlib import Path
import subprocess


def test_ongoing_diagnostics_card_reviews_both_purposes_and_groups_revoke():
    source = Path(__file__).resolve().parents[1] / 'custom_components/power_sync/frontend/power-sync-diagnostics.js'
    script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.style={};}
  append(child){this.children.push(child);}
  replaceChildren(){this.children=[];}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  set innerHTML(value){throw Error('unsafe HTML');}
}
const registry=new Map(),calls=[];
const context={HTMLElement:Node,document:{createElement:tag=>new Node(tag)},customElements:{get:key=>registry.get(key),define:(key,value)=>registry.set(key,value)},Date,JSON,Set};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 const Card=registry.get('ps-diagnostics-card'),card=new Card();
 card._hass={user:{is_admin:false}};await card.load();assert.equal(card.all().length,0);
 const scopes=['ha_read','optimizer_contribution'].map(kind=>({kind,processor:'<img onerror=bad()>',actor:'operator'}));
 const state={identity:{root:'root'},grants:scopes.map((ceiling,index)=>({id:'grant-'+index,approval_set:'one-approval',ceiling,status:'connected'}))};
 card._hass={user:{is_admin:true},callApi:async(method,path,value)=>{assert.equal(path,'power_sync/diagnostics');calls.push(value);if(value?.action==='review')return {combined:true,challenge:'reviewed-both',descriptors:scopes.map(ceiling=>({ceiling,disclosure:'Signed <b>disclosure</b>'}))};return state;}};
 await card.load();
 let nodes=card.all();assert.equal(nodes.filter(node=>node.textContent==='Revoke ongoing access').length,1);
 const identity=nodes.find(node=>node.tag==='pre');
 assert.equal(identity.style.whiteSpace,'pre-wrap','setup identity must wrap in three-column layouts');
 assert.equal(identity.style.userSelect,'text','identity supports normal text selection');
 assert.equal(nodes.find(node=>node.tag==='ha-card').style.overflowWrap,'anywhere');
 for(const control of nodes.filter(node=>['button','input'].includes(node.tag))){
   assert.equal(control.style.maxWidth,'100%');assert.equal(control.style.boxSizing,'border-box');
 }
 const inputs=nodes.filter(node=>node.tag==='input');inputs[0].value='bundle';inputs[1].value='authority';
 await nodes.find(node=>node.textContent==='Review invitation').onclick();
 assert.equal(calls.filter(value=>value?.action==='confirm').length,0,'review alone grants nothing');
 nodes=card.all();assert.ok(nodes.some(node=>node.textContent==='Signed <b>disclosure</b>'));
 await nodes.find(node=>node.textContent==='Enable ongoing access').onclick();
 assert.equal(calls.filter(value=>value?.action==='confirm').length,1);
 assert.equal(calls.find(value=>value?.action==='confirm').challenge,'reviewed-both');
 await card.all().find(node=>node.textContent==='Revoke ongoing access').onclick();
 assert.equal(calls.filter(value=>value?.action==='revoke').length,1);
 assert.equal(calls.find(value=>value?.action==='revoke').grant,'grant-0');
 state.grants[1].status='connecting';await card.load();
 assert.ok(card.all().some(node=>node.textContent?.endsWith('Support and offline testing · connecting')),'partial connection is not reported as complete');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    subprocess.run(['node', '-e', script, str(source)], check=True, capture_output=True, text=True, timeout=20)
