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

const ROUTE_LABELS = {
  RETRY_SECONDARY_GATEWAY: 'Gateway retry',
  PROMPT_PAYMENT_METHOD_SWITCH: 'Customer-approved PayPal switch',
  DYNAMIC_RETRY_BACKOFF: 'Retry with backoff',
  DEGRADATION_CIRCUIT_BYPASS: 'Degradation circuit bypass',
  FALLBACK_PAYMENT_LINK: 'Fallback payment link',
};

/**
 * "Customer-approved PayPal switch (PROMPT_PAYMENT_METHOD_SWITCH)"; unknown keys are shown as they are.
 */
export function describeRoute(routeKey) {
  if (!routeKey) return '—';
  return ROUTE_LABELS[routeKey] ? `${ROUTE_LABELS[routeKey]} (${routeKey})` : routeKey;
}

const SCOPE_LABELS = { razorpay_pipeline: 'Razorpay + synthetic', paypal: 'PayPal' };

/**
 * The provider population a Stage 2-4 analytics scope covers.
 */
export function describeAnalyticsScope(scope) {
  return SCOPE_LABELS[scope] || scope || 'unknown scope';
}

/**
 * One entry per analytics scope: { label, status: 'HEALTHY' | 'DEGRADED', detail }.
 */
export function describeProviderHealth(rows) {
  if (!Array.isArray(rows)) return [];
  return rows.map((r) => ({
    label: describeAnalyticsScope(r.provider_scope),
    status: r.status,
    detail: r.status === 'DEGRADED'
      ? `${formatCount(r.active_episodes)} active episode${r.active_episodes === 1 ? '' : 's'}, worst ${r.worst_severity}`
      : 'No active degradation',
  }));
}

const OFFER_STATUS_ORDER = ['OFFERED', 'ACCEPTED', 'COMPLETED', 'DECLINED', 'EXPIRED', 'ABANDONED', 'PAYMENT_FAILED'];
const OFFER_STATUS_LABELS = {
  OFFERED: 'awaiting customer',
  ACCEPTED: 'accepted at PayPal, not yet paid',
  COMPLETED: 'paid (PayPal-verified)',
  DECLINED: 'declined by customer',
  EXPIRED: 'expired unanswered',
  ABANDONED: 'abandoned after accepting',
  PAYMENT_FAILED: 'PayPal payment failed',
};

export function describeSwitchOfferStatus(status) {
  return OFFER_STATUS_LABELS[status] || status || '—';
}

/**
 * "3 offers: 1 awaiting customer · 1 paid (PayPal-verified) · 1 declined by customer"; zero counts are omitted.
 */
export function describeSwitchOffers(counts) {
  const entries = OFFER_STATUS_ORDER.map((s) => [s, (counts && counts[s]) || 0]).filter(([, n]) => n > 0);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (total === 0) return 'No payment switch offers yet';
  return `${formatCount(total)} offer${total === 1 ? '' : 's'}: `
    + entries.map(([s, n]) => `${formatCount(n)} ${describeSwitchOfferStatus(s)}`).join(' · ');
}

/**
 * Audit lines for the payment switch offer an execution created (none when there is no offer).
 */
export function describeSwitchOffer(offer) {
  if (!offer) return [];
  return [
    `Payment switch offer ${String(offer.offer_id).slice(0, 8)}: ${describeSwitchOfferStatus(offer.status)}`,
    'Requires the customer to approve at PayPal; this engine never captures or switches methods itself',
    offer.expires_at ? `Customer response deadline: ${offer.expires_at}` : null,
  ].filter(Boolean);
}
