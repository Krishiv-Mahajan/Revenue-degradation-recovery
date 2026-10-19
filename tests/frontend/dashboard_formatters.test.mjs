// Dashboard formatter tests (node:test, no dependencies). Run by tests/unit/test_dashboard_formatters.py.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatDateTime, formatMoney, formatMoneyTotals } from '../../src/static/js/api.js';

const reference = (amount, currency) =>
  new Intl.NumberFormat('en-IN', { style: 'currency', currency }).format(amount);

test('formats minor units with each currency\'s own decimal places', () => {
  assert.equal(formatMoney(1041823, 'INR'), '₹10,418.23');
  assert.equal(formatMoney(1000, 'USD'), reference(10, 'USD'));
  assert.equal(formatMoney(1000, 'JPY'), reference(1000, 'JPY'));
  assert.equal(formatMoney(1000, 'KWD'), reference(1, 'KWD'));
  assert.match(formatMoney(1000, 'JPY'), /1,000/);
  assert.match(formatMoney(1000, 'KWD'), /1\.000/);
});

test('normalises currency codes but never assumes one', () => {
  assert.equal(formatMoney(1000, ' inr '), '₹10.00');
  for (const missing of [null, undefined, '', 'IN', 'RUPEES', 12]) {
    const text = formatMoney(1000, missing);
    assert.equal(text, '1,000 minor units (currency unknown)');
    assert.doesNotMatch(text, /₹/);
  }
});

test('reports codes the runtime does not know instead of inventing decimals', () => {
  assert.equal(formatMoney(1000, 'ZZZ'), '1,000 minor units (unsupported currency ZZZ)');
});

test('shows a placeholder, not zero, when the amount is missing', () => {
  for (const missing of [null, undefined, '', 'abc']) {
    assert.equal(formatMoney(missing, 'INR'), '—');
  }
  assert.equal(formatMoney(0, 'INR'), '₹0.00');
});

test('lists totals per currency and never adds different currencies together', () => {
  const totals = [
    { currency: 'INR', amount_minor_units: 50000 },
    { currency: 'USD', amount_minor_units: 7000 },
    { currency: null, amount_minor_units: 5 },
  ];
  const text = formatMoneyTotals(totals);
  assert.equal(
    text,
    [formatMoney(50000, 'INR'), formatMoney(7000, 'USD'), '5 minor units (currency unknown)'].join(' · '),
  );
  assert.doesNotMatch(text, /570\.05|57,005|570\.00/);
  assert.equal(formatMoneyTotals([{ currency: 'JPY', amount_minor_units: 1200 }]), reference(1200, 'JPY'));
});

test('reports missing totals honestly', () => {
  assert.equal(formatMoneyTotals([]), 'None recorded');
  assert.equal(formatMoneyTotals(null), 'None recorded');
  assert.equal(formatMoneyTotals(undefined, 'n/a'), 'n/a');
});

test('formats timestamps in UTC regardless of the runtime time zone', () => {
  assert.notEqual(new Date(0).getTimezoneOffset(), 0, 'run with a non-UTC TZ to exercise the conversion');
  assert.equal(formatDateTime('2026-10-18T10:00:00+00:00'), '18 Oct, 10:00:00 UTC');
  assert.equal(formatDateTime('2026-10-18T23:30:00+05:30'), '18 Oct, 18:00:00 UTC');
  assert.equal(formatDateTime(null), '—');
});
