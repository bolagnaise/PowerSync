// Compatibility for saved dashboards: ongoing diagnostics now lives in Support.
import './power-sync-support.js';
if (!customElements.get('ps-diagnostics-card')) {
  customElements.define('ps-diagnostics-card', class extends customElements.get('ps-support-card') {});
}
