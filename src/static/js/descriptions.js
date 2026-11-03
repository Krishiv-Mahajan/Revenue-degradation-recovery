/**
 * Pure text descriptions for the dashboard (no DOM access), so they can be unit tested.
 */
import { formatCount, formatMoney } from './api.js';

/**
 * Audit lines for a real PayPal recovery checkout (none for simulated executions).
 */
export function describeRecoveryCheckout(checkout) {
  if (!checkout) return [];
  const amount = checkout.amount ? `${checkout.amount.value} ${checkout.amount.currency_code}` : '—';
  return [
    `PayPal ${checkout.environment || ''} recovery checkout: order ${checkout.order_id} (${checkout.order_status || '—'}), ${amount}`,
    checkout.approve_url ? `Payer approval link: ${checkout.approve_url}` : 'No approval link recorded',
    checkout.delivery ? `Delivery: ${checkout.delivery}` : null,
  ].filter(Boolean);
}

/**
 * "SIMULATOR_V1 (simulated, no real provider call): 3 · PAYPAL_ORDERS_V2_SANDBOX (real provider call): 1".
 */
export function describeExecutionProvenance(provenance, total) {
  if (!Array.isArray(provenance) || provenance.length === 0) {
    return `${formatCount(total)} completed; executor unknown`;
  }
  return provenance
    .map((e) => `${e.executor_name} (${e.is_simulation ? 'simulated, no real provider call' : 'real provider call'}): ${formatCount(e.succeeded_command_count)}`)
    .join(' · ');
}

/**
 * Protected GMV per provider and currency, never summed across currencies.
 */
export function describeProtectedByProvider(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return '';
  return 'By provider: ' + rows
    .map((r) => `${r.provider} ${formatMoney(r.amount_minor_units, r.currency)}`)
    .join(' · ');
}

/**
 * The scope each headline metric covers, exactly as the API labels it.
 */
export function describeScopes(data) {
  const scopes = data?.metric_scopes;
  if (!scopes) return '';
  return `Scope: processed ${scopes.processed_gmv}, protected ${scopes.protected_gmv}, episodes ${scopes.active_degradation_episodes}`;
}
