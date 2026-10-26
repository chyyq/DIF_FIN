import test from 'node:test';
import assert from 'node:assert/strict';
import { newState, recordEvents, recordBuy, recordSell, applyQuotes, holdings, learningSummary, mergeState, validateState, exportState, suggestedQuantity } from '../portfolio.js';

const sessions = ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02'];
const at = (day, time = '10:00') => `${day}T${time}:00+08:00`;
const blank = () => newState({ now: at('2026-10-19') });
const buyInput = (overrides = {}) => ({ id: 'buy-1', symbol: '600000', name: '测试股', eventId: 'event-1', eventSnapshot: { eventDay: '2026-10-19', eligible: true }, price: 10, qty: 100, fees: 5, executedAt: at('2026-10-19'), planFraction: 0.3, entry: { high: 12, low: 9, structuralSupport: 9.2, atr14: 0.4 }, ...overrides });
const buy = (overrides = {}) => recordBuy(blank(), buyInput(overrides));
const sellInput = (overrides = {}) => ({ id: 'sell-1', symbol: '600000', name: '测试股', price: 11, qty: 100, fees: 6, executedAt: at('2026-10-20'), ...overrides });
const rows = (state, day = '2026-10-20', time = '10:01') => holdings(state, { sessions, asOf: day, now: at(day, time) });

test('buy recording is immutable and freezes cent-rounded targets from known entry data', () => {
  const original = blank(), state = recordBuy(original, buyInput());
  assert.equal(original.fills.length, 0);
  assert.equal(state.fills[0].priceCents, 1000);
  assert.deepEqual(state.fills[0].entry, { highCents: 1200, lowCents: 900, structuralSupportCents: 920, atr14Cents: 40, stopCents: 880, tp1Cents: 1014, tp2Cents: 1085, knownAt: null });
  assert.equal(validateState(state).valid, true);
});

test('30/30/40 clicks only suggest quantity and never record a fill', () => {
  const state = blank();
  assert.deepEqual(suggestedQuantity({ budget: 10000, fraction: 0.3, price: 10 }), { qty: 300, allocatedBudget: 3000, fraction: 0.3 });
  assert.equal(state.fills.length, 0);
  assert.throws(() => recordBuy(state, { ...buyInput(), qty: undefined }), /股数/);
});

test('multiple buys merge to one symbol row with quantity-weighted cost and frozen lot targets', () => {
  const state = recordBuy(buy(), buyInput({ id: 'buy-2', qty: 200, price: 11, fees: 3, executedAt: at('2026-10-20'), entry: { high: 13, low: 10, stop: 9.5 } }));
  const [row] = rows(state);
  assert.equal(rows(state).length, 1);
  assert.equal(row.qty, 300);
  assert.equal(row.avgPrice, 10.67);
  assert.equal(row.avgCost, 10.69);
  assert.equal(row.sellableQty, 100);
  assert.equal(row.firstBuyDate, '2026-10-19');
  assert.equal(row.targets.length, 2);
  assert.equal(row.targets[0].tp1, 10.14);
  assert.equal(row.targets[1].tp1, 11.14);
});

test('T+1 is batch-specific and same-day buy cannot be sold', () => {
  assert.equal(rows(buy(), '2026-10-19')[0].sellableQty, 0);
  assert.throws(() => recordSell(buy(), sellInput({ executedAt: at('2026-10-19', '14:00') }), { sessions }), /T\+1/);
  const scaled = recordBuy(buy(), buyInput({ id: 'buy-2', qty: 200, executedAt: at('2026-10-20', '09:40') }));
  assert.throws(() => recordSell(scaled, sellInput({ qty: 200 }), { sessions }), /T\+1/);
  const state = recordSell(scaled, sellInput({ qty: 100 }), { sessions });
  assert.equal(rows(state)[0].qty, 200);
  assert.equal(rows(state)[0].sellableQty, 0);
});

test('unknown or non-covered calendars do not invent maturity dates or sellable shares', () => {
  const state = buy();
  const [row] = holdings(state, { asOf: '2026-10-27', now: at('2026-10-27') });
  assert.equal(row.calendarUnknown, true);
  assert.equal(row.heldSessions, null);
  assert.equal(row.expiryDate, null);
  assert.equal(row.sellableQty, null);
  assert.throws(() => recordSell(state, sellInput()), /日历/);
  assert.throws(() => recordSell(state, sellInput(), { sessions: ['2026-10-20'] }), /日历覆盖/);
  assert.equal(holdings(state, { sessions: [], asOf: '2026-10-20', now: at('2026-10-20') })[0].calendarUnknown, true);
});

test('first buy is day one, weekend and holiday are skipped, adding does not extend day seven', () => {
  const state = recordBuy(buy(), buyInput({ id: 'buy-2', executedAt: at('2026-10-26') }));
  assert.equal(rows(state, '2026-10-26')[0].heldSessions, 6);
  assert.equal(rows(state, '2026-10-26')[0].due, false);
  const seventh = rows(state, '2026-10-27')[0];
  assert.equal(seventh.heldSessions, 7);
  assert.equal(seventh.expiryDate, '2026-10-27');
  assert.equal(seventh.due, true);
  assert.equal(rows(state, '2026-11-02')[0].qty, 200);
  assert.equal(rows(state, '2026-11-02')[0].due, true);
});

test('partially selling all original shares does not reset the cycle deadline while later shares remain', () => {
  let state = recordBuy(buy(), buyInput({ id: 'buy-2', executedAt: at('2026-10-20', '09:40') }));
  state = recordSell(state, sellInput(), { sessions });
  assert.equal(rows(state, '2026-10-27')[0].firstBuyDate, '2026-10-19');
  assert.equal(rows(state, '2026-10-27')[0].due, true);
});

test('partial actual sells retain residual position and allocate all fees without losing cents', () => {
  let state = buy({ qty: 300, fees: 5.03 });
  state = recordSell(state, sellInput({ qty: 100, price: 11, fees: 1.01 }), { sessions });
  const [row] = rows(state);
  assert.equal(row.qty, 200);
  assert.equal(row.costCents, 200336);
  assert.equal(row.netRealizedCents, 9732);
  assert.equal(learningSummary(state).closedEventCount, 0);
  assert.equal(learningSummary(state).samples[0].netPnl, null);
  state = recordSell(state, sellInput({ id: 'sell-2', qty: 200, price: 12, fees: 2.02, executedAt: at('2026-10-21') }), { sessions });
  assert.equal(rows(state, '2026-10-21').length, 0);
  const summary = learningSummary(state);
  assert.equal(summary.closedEventCount, 1);
  assert.equal(summary.closedTrades[0].netPnlCents, 49194);
  assert.equal(summary.winRate, null);
});

test('quote touching target or stop never becomes an actual sell', () => {
  const state = applyQuotes(buy(), [{ symbol: '600000', price: 11, timestamp: at('2026-10-20', '10:00'), source: 'test' }], { now: at('2026-10-20', '10:01') });
  const [row] = rows(state);
  assert.equal(row.targets[0].tp1Reached, true);
  assert.equal(row.targets[0].tp2Reached, true);
  assert.equal(row.qty, 100);
  assert.equal(state.fills.length, 1);
  assert.equal(row.unrealizedPnlCents, 9500);
});

test('only actual post-entry quote samples affect observed extrema; daily highs/lows are ignored', () => {
  let state = applyQuotes(buy(), [{ symbol: '600000', price: 9, timestamp: at('2026-10-19', '09:50'), high: 15, low: 5 }], { now: at('2026-10-19', '09:51') });
  assert.equal(state.observations.length, 0);
  state = applyQuotes(state, [{ symbol: '600000', price: 10.5, timestamp: at('2026-10-19', '10:02'), high: 15, low: 5 }], { now: at('2026-10-19', '10:03') });
  const [row] = rows(state, '2026-10-19', '10:03');
  assert.equal(row.observedHigh, 10.5);
  assert.equal(row.observedLow, 10);
  assert.equal(row.observationCount, 1);
  assert.equal(row.coverageGap, false);
});

test('stale, out-of-order and future quotes cannot create live valuation or observed extremes', () => {
  let state = applyQuotes(buy(), [{ symbol: '600000', price: 10.1, timestamp: at('2026-10-20', '10:00') }], { now: at('2026-10-20', '10:01') });
  state = applyQuotes(state, [{ symbol: '600000', price: 999, timestamp: at('2026-10-20', '09:00') }, { symbol: '600000', price: 888, timestamp: at('2026-10-21', '10:00') }], { now: at('2026-10-20', '10:02') });
  assert.equal(state.quotes['600000'].priceCents, 1010);
  assert.equal(state.observations.length, 1);
  const [row] = rows(state, '2026-10-20', '11:00');
  assert.equal(row.stale, true);
  assert.equal(row.marketValue, null);
  assert.equal(row.unrealizedPnl, null);
  assert.equal(row.targets[0].tp1Reached, false);
  assert.equal(row.coverageGap, true);
});

test('entry anchors stay frozen when later quotes rise and future entry knowledge is rejected', () => {
  const state = applyQuotes(buy(), [{ symbol: '600000', price: 20, timestamp: at('2026-10-20') }], { now: at('2026-10-20', '10:01') });
  assert.equal(state.fills[0].entry.highCents, 1200);
  assert.equal(state.fills[0].entry.tp2Cents, 1085);
  assert.throws(() => buy({ entry: { high: 12, low: 9, knownAt: at('2026-10-20') } }), /成交后/);
});

test('a manual actual fill may be journaled with missing plan, but cannot fabricate its targets', () => {
  const state = buy({ entry: {} });
  assert.equal(state.fills[0].entry.tp1Cents, null);
  assert.equal(rows(state)[0].targets[0].requiresExitReview, true);
});

test('export/import union is stable, idempotent and never erases local fills', () => {
  const state = buy();
  const imported = mergeState(blank(), exportState(state));
  assert.deepEqual(imported, state);
  const twice = mergeState(imported, exportState(imported));
  assert.deepEqual(twice, imported);
  const mergedEmpty = mergeState(twice, blank());
  assert.equal(mergedEmpty.fills.length, 1);
  assert.equal(mergedEmpty.events.length, 1);
  assert.deepEqual(recordBuy(state, buyInput()), state);
  const sold = recordSell(state, sellInput(), { sessions });
  assert.deepEqual(recordSell(sold, sellInput(), { sessions }), sold);
});

test('conflicting IDs and malformed imports fail atomically', () => {
  const state = buy(), original = exportState(state);
  const conflict = structuredClone(state); conflict.fills[0].priceCents += 1;
  assert.throws(() => mergeState(state, conflict), /冲突/);
  assert.equal(exportState(state), original);
  assert.throws(() => mergeState(state, '{broken'), /JSON/);
  assert.throws(() => mergeState(state, { ...state, version: 99 }), /version/);
  assert.throws(() => mergeState(state, JSON.parse('{"__proto__":{"polluted":true}}')), /禁止/);
  assert.equal({}.polluted, undefined);
});

test('tampered allocation, quantity, future knowledge and non-finite data are rejected', () => {
  const state = recordSell(buy(), sellInput(), { sessions });
  for (const mutate of [
    s => { s.fills[0].qty = -100; },
    s => { s.fills[0].priceCents = Infinity; },
    s => { s.fills[1].allocations[0].costCents -= 1; },
    s => { s.fills[1].qty = 200; },
    s => { s.fills[0].entry.tp1Cents = 9999; },
    s => { s.fills[0].entry.knownAt = at('2026-10-20'); },
    s => { s.fills.push(structuredClone(s.fills[0])); }
  ]) {
    const invalid = structuredClone(state); mutate(invalid);
    assert.equal(validateState(invalid).valid, false);
    assert.throws(() => mergeState(state, invalid));
  }
});

test('all candidate snapshots and fills become samples, including rejected/unfilled events', () => {
  let state = recordEvents(blank(), [{ eventId: 'unfilled', symbol: '000001', snapshot: { eligible: false, reason: 'TREND_GATE' } }], { now: at('2026-10-19') });
  state = recordBuy(state, buyInput());
  state = recordEvents(state, [{ eventId: 'event-1', symbol: '600000', snapshot: { eligible: false, reason: 'EXPIRED' } }], { now: at('2026-10-21') });
  const summary = learningSummary(state);
  assert.equal(summary.eventCount, 2);
  assert.equal(summary.unfilledEventCount, 1);
  assert.equal(summary.openEventCount, 1);
  assert.equal(summary.samples.find(s => s.eventId === 'event-1').snapshots.length, 2);
  assert.equal(summary.fillCount, 1);
  assert.equal(summary.winRate, null);
});

test('win-rate gates use distinct closed events, never multiple fills or open winners', () => {
  let state = blank();
  for (let i = 0; i < 30; i++) {
    state = recordBuy(state, buyInput({ id: `buy-${i}`, eventId: `event-${i}`, fees: 0 }));
    state = recordSell(state, sellInput({ id: `sell-${i}`, fees: 0 }), { sessions });
  }
  const summary = learningSummary(state);
  assert.equal(summary.closedEventCount, 30);
  assert.equal(summary.stage, 'DESCRIPTIVE_ONLY');
  assert.equal(summary.winRate, 1);
  assert.equal(summary.eligibleForRecommendationTuning, false);
  state = recordBuy(state, buyInput({ id: 'open-winner', eventId: 'open-event', price: 1, fees: 0 }));
  assert.equal(learningSummary(state).closedEventCount, 30);
});

test('sell fee attribution across events remains equal to total actual cash flow', () => {
  let state = recordBuy(buy({ qty: 100, fees: 0.01 }), buyInput({ id: 'buy-2', eventId: 'event-2', qty: 200, fees: 0.02, executedAt: at('2026-10-19', '10:01') }));
  state = recordSell(state, sellInput({ qty: 300, fees: 0.05 }), { sessions });
  const summary = learningSummary(state);
  assert.equal(summary.closedEventCount, 2);
  assert.equal(summary.closedTrades.reduce((sum, s) => sum + s.netPnlCents, 0), 29992);
  assert.equal(summary.netRealized, 299.92);
});

test('a fully closed position starts a new tracking window only after a new actual buy', () => {
  let state = recordSell(buy(), sellInput(), { sessions });
  state = recordBuy(state, buyInput({ id: 'new-cycle', eventId: 'new-event', price: 9, executedAt: at('2026-10-23') }));
  const [row] = rows(state, '2026-10-27');
  assert.equal(row.firstBuyDate, '2026-10-23');
  assert.equal(row.heldSessions, 3);
  assert.equal(row.due, false);
  assert.equal(row.observedHigh, 9);
  assert.equal(row.observedLow, 9);
});

test('fractional cents, monetary overflow and inconsistent timestamp formats fail closed', () => {
  assert.throws(() => buy({ price: 1.005 }), /两位小数/);
  assert.throws(() => buy({ price: '10' }), /金额/);
  assert.throws(() => buy({ priceCents: Number.MAX_SAFE_INTEGER, qty: 2 }), /整数/);
  const invalid = buy(); invalid.fills[0].executedAt = at('2026-10-19');
  assert.equal(validateState(invalid).valid, false);
  const symbolAlias = buy(); symbolAlias.fills[0].symbol = 'SH600000';
  assert.equal(validateState(symbolAlias).valid, false);
});

test('replaying an old buy after newer event snapshots keeps its original entry snapshot', () => {
  let state = buy({ eventSnapshot: undefined });
  state = recordEvents(state, [{ eventId: 'event-1', symbol: '600000', snapshot: { state: 'UPDATED' } }], { now: at('2026-10-20') });
  assert.deepEqual(recordBuy(state, buyInput({ eventSnapshot: undefined })), state);
});

test('merging independently recorded identical snapshots deduplicates by content with earliest recording time', () => {
  const event = { eventId: 'unfilled', symbol: '000001', snapshot: { eligible: false } };
  const a = recordEvents(blank(), [event], { now: at('2026-10-19') });
  const b = recordEvents(blank(), [event], { now: at('2026-10-20') });
  const merged = mergeState(a, b);
  assert.equal(merged.events[0].snapshots.length, 1);
  assert.equal(merged.events[0].snapshots[0].recordedAt, new Date(at('2026-10-19')).toISOString());
});

test('historical view excludes future buys and even a one-minute future quote cannot trigger a target', () => {
  let state = recordBuy(buy(), buyInput({ id: 'tomorrow-buy', executedAt: at('2026-10-20') }));
  state = applyQuotes(state, [{ symbol: '600000', price: 15, timestamp: at('2026-10-19', '10:02') }], { now: at('2026-10-19', '10:01') });
  const [row] = rows(state, '2026-10-19', '10:01');
  assert.equal(row.qty, 100);
  assert.equal(row.marketPrice, null);
  assert.equal(row.targets[0].tp1Reached, false);
});

test('one event with many fills remains one independent sample, and one hundred events stay research-only', () => {
  let state = blank();
  for (let i = 0; i < 100; i++) {
    state = recordBuy(state, buyInput({ id: `b-${i}`, eventId: `e-${i}`, fees: 0 }));
    state = recordSell(state, sellInput({ id: `s-${i}`, fees: 0 }), { sessions });
  }
  const research = learningSummary(state);
  assert.equal(research.closedEventCount, 100);
  assert.equal(research.stage, 'RESEARCH_ONLY');
  assert.equal(research.eligibleForRecommendationTuning, false);
  const one = structuredClone(state);
  for (const fill of one.fills.filter(f => f.side === 'BUY')) {
    fill.eventId = one.events[0].id;
    fill.eventSnapshotId = one.events[0].snapshots[0].id;
  }
  one.events = [one.events[0]];
  const single = learningSummary(one);
  assert.equal(single.closedEventCount, 1);
  assert.equal(single.winRate, null);
});

test('optional buy/sell notes survive export, learning samples and idempotent merge', () => {
  let state = buy({ note: '已核对券商成交，第一笔30%计划。' });
  state = recordSell(state, sellInput({ note: '部分成交后已核实费用。', reason: 'TP1手动执行' }), { sessions });
  const imported = mergeState(blank(), exportState(state));
  assert.equal(imported.fills[0].note, '已核对券商成交，第一笔30%计划。');
  assert.equal(imported.fills[1].note, '部分成交后已核实费用。');
  assert.deepEqual(mergeState(imported, exportState(imported)), imported);
  assert.equal(learningSummary(imported).samples[0].fills[0].note, state.fills[0].note);
  assert.equal(learningSummary(imported).samples[0].fills[1].note, state.fills[1].note);
  assert.throws(() => recordSell(state, sellInput({ note: '另一个内容', reason: 'TP1手动执行' }), { sessions }), /内容不同/);
});

test('empty optional notes preserve old fill identities, while malformed/long notes reject atomically', () => {
  const old = buy();
  assert.equal(Object.hasOwn(old.fills[0], 'note'), false);
  assert.deepEqual(recordBuy(old, buyInput({ note: '' })), old);
  const sold = recordSell(old, sellInput(), { sessions });
  assert.equal(Object.hasOwn(sold.fills[1], 'note'), false);
  assert.deepEqual(recordSell(sold, sellInput({ note: '' }), { sessions }), sold);
  assert.throws(() => buy({ note: '长'.repeat(241) }), /备注/);
  assert.throws(() => buy({ note: { nested: true } }), /备注/);
  for (const note of [null, 123, '长'.repeat(241)]) {
    const invalid = structuredClone(old); invalid.fills[0].note = note;
    assert.equal(validateState(invalid).valid, false);
    assert.throws(() => mergeState(old, invalid), /备注/);
  }
  assert.equal(Object.hasOwn(old.fills[0], 'note'), false);
});

test('historical buy cannot silently reference an existing snapshot recorded after the fill', () => {
  const state = recordEvents(blank(), [{ eventId: 'event-1', symbol: '600000', snapshot: { eligible: true } }], { now: at('2026-10-20') });
  const before = exportState(state);
  assert.throws(() => recordBuy(state, buyInput({ eventSnapshot: undefined })), /快照记录晚于/);
  assert.equal(exportState(state), before);
  const explicit = recordBuy(state, buyInput({ eventSnapshot: { manualFill: true, asOf: at('2026-10-19', '09:50'), highKnownAtFill: 12 } }));
  assert.equal(explicit.fills.length, 1);
  assert.equal(explicit.events[0].snapshots.length, 2);
});

test('explicit snapshots must not declare future quote, calculation or as-of times', () => {
  for (const key of ['quoteTimestamp', 'generatedAt', 'asOf']) {
    assert.throws(() => buy({ eventSnapshot: { [key]: at('2026-10-19', '10:01') } }), /晚于实际成交/);
    assert.equal(buy({ eventSnapshot: { [key]: at('2026-10-19', '09:59') } }).fills.length, 1);
  }
  assert.throws(() => buy({ eventSnapshot: { asOf: '2026-10-20' } }), /晚于实际成交/);
  assert.equal(buy({ eventSnapshot: { asOf: '2026-10-19' } }).fills.length, 1);
});

test('explicit manual backfill may have a later logging time without pretending the source was later known', () => {
  const manual = { manualFill: true, recordedAt: at('2026-10-21'), quoteTimestamp: at('2026-10-19', '09:59'), generatedAt: at('2026-10-19', '09:59') };
  let state = recordEvents(blank(), [{ eventId: 'event-1', symbol: '600000', snapshot: manual }], { now: at('2026-10-21') });
  state = recordBuy(state, buyInput({ eventSnapshot: manual }));
  assert.equal(state.fills.length, 1);
  assert.equal(state.events[0].snapshots.length, 1);
  assert.equal(state.events[0].snapshots[0].recordedAt, new Date(at('2026-10-21')).toISOString());
});
