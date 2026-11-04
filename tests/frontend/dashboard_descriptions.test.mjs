// Dashboard description tests (node:test, no dependencies). Run by tests/unit/test_dashboard_formatters.py.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatMoney } from '../../src/static/js/api.js';
import {
  describeAnalyticsScope,
  describeExecutionProvenance,
  describeProtectedByProvider,
  describeProviderHealth,
  describeRecoveryCheckout,
  describeRoute,
  describeScopes,
  describeSwitchOffer,
  describeSwitchOfferStatus,
  describeSwitchOffers,
} from '../../src/static/js/descriptions.js';

test('names real and simulated executors separately', () => {
  const text = describeExecutionProvenance([
    { executor_name: 'PAYPAL_ORDERS_V2_SANDBOX', is_simulation: false, succeeded_command_count: 1 },
    { executor_name: 'SIMULATOR_V1', is_simulation: true, succeeded_command_count: 3 },
  ], 4);
  assert.equal(
    text,
    'PAYPAL_ORDERS_V2_SANDBOX (real provider call): 1 · SIMULATOR_V1 (simulated, no real provider call): 3',
  );
  assert.equal(describeExecutionProvenance([], 2), '2 completed; executor unknown');
});

test('reports protected GMV per provider and currency without adding currencies', () => {
  const text = describeProtectedByProvider([
    { provider: 'paypal', currency: 'USD', amount_minor_units: 16200 },
    { provider: 'razorpay', currency: 'INR', amount_minor_units: 16200 },
  ]);
  assert.equal(text, `By provider: paypal ${formatMoney(16200, 'USD')} · razorpay ${formatMoney(16200, 'INR')}`);
  assert.equal(describeProtectedByProvider([]), '');
});

test('describes a real recovery checkout and says when it was not delivered', () => {
  const lines = describeRecoveryCheckout({
    order_id: '5O190127TN364715T', order_status: 'PAYER_ACTION_REQUIRED', environment: 'sandbox',
    approve_url: 'https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T',
    amount: { currency_code: 'USD', value: '500.00' }, delivery: 'NOT_DELIVERED: needs a merchant channel',
  });
  assert.deepEqual(lines, [
    'PayPal sandbox recovery checkout: order 5O190127TN364715T (PAYER_ACTION_REQUIRED), 500.00 USD',
    'Payer approval link: https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T',
    'Delivery: NOT_DELIVERED: needs a merchant channel',
  ]);
  assert.deepEqual(describeRecoveryCheckout(null), []);
});

test('repeats the API scope labels, including not-applicable metrics', () => {
  const text = describeScopes({ metric_scopes: {
    processed_gmv: 'not_applicable', protected_gmv: 'paypal', active_degradation_episodes: 'global',
  } });
  assert.equal(text, 'Scope: processed not_applicable, protected paypal, episodes global');
  assert.equal(describeScopes({}), '');
});

test('names gateway retry and the customer-approved switch as different interventions', () => {
  assert.equal(describeRoute('RETRY_SECONDARY_GATEWAY'), 'Gateway retry (RETRY_SECONDARY_GATEWAY)');
  assert.equal(
    describeRoute('PROMPT_PAYMENT_METHOD_SWITCH'),
    'Customer-approved PayPal switch (PROMPT_PAYMENT_METHOD_SWITCH)',
  );
  assert.equal(describeRoute('SOMETHING_NEW'), 'SOMETHING_NEW');
  assert.equal(describeRoute(null), '—');
});

test('reports each analytics scope as healthy or degraded', () => {
  assert.deepEqual(describeProviderHealth([
    { provider_scope: 'razorpay_pipeline', status: 'HEALTHY', active_episodes: 0, worst_severity: null },
    { provider_scope: 'paypal', status: 'DEGRADED', active_episodes: 2, worst_severity: 'CRITICAL' },
  ]), [
    { label: 'Razorpay + synthetic', status: 'HEALTHY', detail: 'No active degradation' },
    { label: 'PayPal', status: 'DEGRADED', detail: '2 active episodes, worst CRITICAL' },
  ]);
  assert.equal(describeAnalyticsScope(undefined), 'unknown scope');
  assert.deepEqual(describeProviderHealth(null), []);
});

test('separates offers awaiting the customer from accepted and PayPal-verified paid offers', () => {
  assert.equal(
    describeSwitchOffers({ OFFERED: 1, ACCEPTED: 1, COMPLETED: 2, DECLINED: 0, EXPIRED: 1 }),
    '5 offers: 1 awaiting customer · 1 accepted at PayPal, not yet paid · 2 paid (PayPal-verified) · 1 expired unanswered',
  );
  assert.equal(describeSwitchOffers({ OFFERED: 0 }), 'No payment switch offers yet');
  assert.equal(describeSwitchOfferStatus('ABANDONED'), 'abandoned after accepting');
});

test('describes a switch offer in the audit trail without implying the engine charged anyone', () => {
  const lines = describeSwitchOffer({
    offer_id: '0123456789abcdef', status: 'OFFERED', expires_at: '2026-11-04T10:30:00+00:00',
  });
  assert.deepEqual(lines, [
    'Payment switch offer 01234567: awaiting customer',
    'Requires the customer to approve at PayPal; this engine never captures or switches methods itself',
    'Customer response deadline: 2026-11-04T10:30:00+00:00',
  ]);
  assert.deepEqual(describeSwitchOffer(null), []);
});
