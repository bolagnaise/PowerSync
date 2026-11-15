/* Account display only. Billing activation stays on powersync.cc. */
class PowerSyncReferralsCard extends HTMLElement {
  constructor() { super(); this.attachShadow({mode:'open'}); this._busy=false; }
  setConfig(config) { this._config=config; }
  getCardSize() { return 3; }
  set hass(value) {
    this._hass=value;
    if(!this._loaded && value?.user?.is_admin) {this._loaded=true;void this.load();}
    if(!value?.user?.is_admin) {this._summary=null;this._loaded=false;this.render();}
  }
  get hass() {return this._hass;}
  node(tag,text,parent) {const element=document.createElement(tag);if(text!==undefined)element.textContent=text;parent?.append(element);return element;}
  async load(refresh=false) {
    if(this._busy || !this._hass?.user?.is_admin)return;
    this._busy=true;
    try {this._summary=await this._hass.callApi('GET',`power_sync/referrals${refresh?'?refresh=1':''}`);this._error=false;}
    catch {this._error=true;}
    finally {this._busy=false;this.render();}
  }
  async copy(value) {
    try {await navigator.clipboard.writeText(value);this._notice='Copied.';}
    catch {this._notice='Select and copy the code or link shown below.';}
    this.render();
  }
  render() {
    this.shadowRoot.replaceChildren();
    if(!this._hass?.user?.is_admin)return;
    const style=this.node('style',`ha-card{padding:20px}h2{margin-top:0}p{overflow-wrap:anywhere}button{padding:8px 12px;margin:4px 8px 4px 0;cursor:pointer}a{color:var(--primary-color)}`,this.shadowRoot);
    const card=this.node('ha-card',undefined,this.shadowRoot);this.node('h2','Refer friends',card);
    const summary=this._summary;
    if(summary?.code) {
      if(summary.enabled===false)this.node('p','New referral rewards are currently unavailable. Existing rewards remain on your account.',card);
      this.node('p',`Your referral code: ${summary.code}`,card);
      this.node('p',summary.share_url,card);
      this.node('p',`${summary.earned_this_year} of ${summary.annual_limit} rewards earned in ${summary.year}. Banked rewards: ${summary.banked_rewards}.`,card);
      for(const [label,value] of [['Copy code',summary.code],['Copy link',summary.share_url]]) {
        const button=this.node('button',label,card);button.onclick=()=>this.copy(value);button.disabled=summary.enabled===false;
      }
      if(summary.stale)this.node('p',`Showing cached information from ${new Date(summary.fetched_at*1000).toLocaleString()}.`,card);
    } else this.node('p',summary?.paired===false?'Pair your PowerSync account through HA optimiser access to see your code.':'Referral information is temporarily unavailable.',card);
    this.node('p','Your friend’s first successful payment earns you a free month. Existing subscribers get their next renewal free; free users can activate a banked month with a card and automatic renewal.',card);
    const link=this.node('a','Manage referrals',card);link.href='https://powersync.cc/app/referrals';link.target='_blank';link.rel='noopener noreferrer';
    const button=this.node('button','Refresh',card);button.onclick=()=>this.load(true);button.disabled=this._busy;
    if(this._error)this.node('p','Could not refresh. Your optimiser access is unaffected.',card);
    if(this._notice)this.node('p',this._notice,card);
    void style;
  }
}
if(!customElements.get('power-sync-referrals'))customElements.define('power-sync-referrals',PowerSyncReferralsCard);
