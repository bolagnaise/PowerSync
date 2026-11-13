"""One support card manages temporary sessions and reviewed ongoing permissions."""
from pathlib import Path
import subprocess


def test_support_card_ongoing_option_reviews_permissions_and_groups_revoke():
    frontend = Path(__file__).resolve().parents[1] / 'custom_components/power_sync/frontend'
    script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.attributes={};this.value='';}
  append(child){this.children.push(child);}
  replaceChildren(){this.children=[];}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  querySelectorAll(){return this.all();}
  setAttribute(key,value){this.attributes[key]=value;}
  attachShadow(){return this.shadowRoot=new Node('shadow');}
  set innerHTML(value){throw Error('unsafe HTML');}
}
const registry=new Map(),calls=[];
const context={HTMLElement:Node,document:{createElement:tag=>new Node(tag)},customElements:{get:key=>registry.get(key),define:(key,value)=>registry.set(key,value)},Date,JSON,Set,Promise,setTimeout,clearTimeout};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1]+'/power-sync-support.js','utf8'),context);
(async()=>{
 const Card=registry.get('ps-support-card'),card=new Card();
 card._hass={user:{is_admin:false},callApi:()=>{throw Error('non-admin request');}};
 await card.load();assert.equal(card.shadowRoot.all().filter(node=>node.tag==='button').length,0);
 const scopes=['ha_read','optimizer_contribution'].map(kind=>({kind,processor:'<img onerror=bad()>',actor:'operator'}));
 const state={identity:{root:'root'},grants:[]};
 let temporary={state:'closed'},failSupport=false,failOngoing=false;
 card._hass={user:{is_admin:true},callApi:async(method,path,value)=>{
   calls.push({method,path,value});
   if(path==='power_sync/support'){
     if(failSupport)throw Error('session unavailable');
     if(value?.action==='revoke')temporary={state:'closed'};
     return value?temporary:{entries:[{entry_id:'entry-one',status:temporary}]};
   }
   assert.equal(path,'power_sync/diagnostics');
   if(failOngoing)throw Error('ongoing unavailable');
   if(value?.action==='review')return {combined:true,challenge:'reviewed-both',descriptors:scopes.map(ceiling=>({ceiling,disclosure:'Signed <b>disclosure</b>'}))};
   if(value?.action==='confirm')state.grants=scopes.map((ceiling,index)=>({id:'grant-'+index,approval_set:'one-approval',ceiling,status:'connected'}));
   return state;
 }};
 await card.load();
 const nodes=()=>card.shadowRoot.all();
 const option=()=>nodes().find(node=>node.tag==='label'&&node.children.some(child=>child.textContent==='Allow ongoing support')).children[0];
 const button=text=>nodes().find(node=>node.tag==='button'&&node.textContent===text);
 const field=label=>nodes().find(node=>node.attributes['aria-label']===label);
 assert.equal(nodes().filter(node=>node.tag==='h3'&&node.textContent==='Support').length,1);
 assert.ok(button('Get pairing code'));assert.equal(option().checked,false);
 const before=calls.length;option().checked=true;option().onchange();
 assert.equal(calls.length,before,'changing access option never grants or revokes');
 assert.ok(!button('Get pairing code'),'one invitation workflow is visible');
 assert.ok(!nodes().some(node=>node.textContent==='No live access'),'temporary status cannot misrepresent ongoing access');
 assert.equal(nodes().filter(node=>node.attributes['aria-label']==='Support invitation').length,1);
 assert.ok(field('Support invitation').maxLength>=5500,'combined invitations fit');
 const identity=nodes().find(node=>node.tag==='pre');assert.ok(identity.textContent.includes('root'));
 assert.ok(button('Review invitation').disabled);
 const enterInvitation=()=>{
   field('Support invitation').value='bundle';field('Support invitation').oninput();
   field('Authority public key').value='authority';field('Authority public key').oninput();
 };
 enterInvitation();assert.equal(button('Review invitation').disabled,false);
 await button('Review invitation').onclick();
 assert.equal(calls.filter(call=>call.value?.action==='confirm').length,0,'review alone grants nothing');
 assert.ok(nodes().some(node=>node.textContent==='Signed <b>disclosure</b>'),'disclosures use inert text');
 assert.ok(nodes().some(node=>node.textContent==='Connected application: <img onerror=bad()>'));
 assert.equal(button('Enable ongoing access').disabled,true,'explicit consent required');
 // A changed invitation cannot reuse a previously checked approval.
 field('Support invitation').value='changed';field('Support invitation').oninput();
 assert.ok(!button('Enable ongoing access'));
 await button('Review invitation').onclick();
 const consent=nodes().find(node=>node.tag==='label'&&node.children.some(child=>child.textContent?.startsWith('I reviewed'))).children[0];
 consent.checked=true;consent.onchange();assert.equal(button('Enable ongoing access').disabled,false);
 await card.load(true);assert.ok(nodes().includes(consent),'unchanged background status preserves reviewed consent');
 await button('Enable ongoing access').onclick();
 const confirms=calls.filter(call=>call.value?.action==='confirm');
 assert.equal(confirms.length,1);assert.equal(confirms[0].path,'power_sync/diagnostics');
 assert.equal(confirms[0].value.challenge,'reviewed-both');
 assert.equal(nodes().filter(node=>node.textContent==='Revoke ongoing access').length,1);
 await button('Revoke ongoing access').onclick();
 const revokes=calls.filter(call=>call.value?.action==='revoke');
 assert.equal(revokes.length,1);assert.equal(revokes[0].value.grant,'grant-0');
 state.grants[1].status='connecting';await card.load();
 assert.ok(nodes().some(node=>node.textContent?.endsWith('Support and offline testing · connecting')),'partial connection is not complete');
 // Existing access stays manageable in either mode, including an active temporary session.
 temporary={state:'active',scope:{categories:['system'],history_start:1700000000,expires_at:1800000000}};
 await card.load();option().checked=false;option().onchange();
 assert.ok(button('Revoke ongoing access'));assert.ok(button('Stop support access'));
 await button('Stop support access').onclick();
 assert.equal(calls.filter(call=>call.path==='power_sync/support'&&call.value?.action==='revoke').length,1);
 // An unavailable ongoing service cannot break temporary support or local diagnostics.
 failOngoing=true;await card.load();assert.ok(button('Get pairing code'));assert.ok(button('Download local diagnostics'));
 option().checked=true;option().onchange();
 assert.ok(nodes().some(node=>node.attributes.role==='alert'&&node.textContent.includes('ongoing support status')));
 // Ongoing access is installation-wide and remains revocable if entry discovery fails.
 failOngoing=false;failSupport=true;card._entries=[];card._entry=undefined;await card.load();
 assert.equal(button('Revoke ongoing access').disabled,false);
 // Saved legacy diagnostics cards use this same implementation and discover existing grants.
 vm.runInContext(fs.readFileSync(process.argv[1]+'/power-sync-diagnostics.js','utf8').replace(/^import .*;\s*$/m,''),context);
 const Legacy=registry.get('ps-diagnostics-card'),legacy=new Legacy();assert.ok(legacy instanceof Card);
 failSupport=false;legacy._hass=card._hass;await legacy.load();
 assert.equal(legacy._ongoing,true);assert.ok(legacy.shadowRoot.all().some(node=>node.textContent==='Support'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    subprocess.run(['node', '-e', script, str(frontend)], check=True, capture_output=True, text=True, timeout=20)
