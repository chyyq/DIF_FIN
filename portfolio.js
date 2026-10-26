/**
 * Manual LBR trade journal. This module never submits, simulates, or infers a fill.
 * Money is stored as safe integer cents; all writes return a new state.
 */
export const SCHEMA = 'lbr-portfolio';
export const VERSION = 1;
const DEFAULT_CONFIG = Object.freeze({ maxHoldSessions: 7, buyFractions: [0.3, 0.3, 0.4], quoteMaxAgeMinutes: 15 });
const MAX_ITEMS = 100000;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const ID = /^[\p{L}\p{N}_.:/-]{1,180}$/u;

function fail(message) { throw new Error(message); }
function integer(value, label, min = 0) {
  if (!Number.isSafeInteger(value) || value < min) fail(`${label}必须为有效整数`);
  return value;
}
function money(value, label, allowZero = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < (allowZero ? 0 : 0.01)) fail(`${label}必须为有效金额`);
  const cents = Math.round((value + Number.EPSILON) * 100);
  if (!Number.isSafeInteger(cents)) fail(`${label}超出金额范围`);
  if (Math.abs(value * 100 - cents) > 0.000001) fail(`${label}最多保留两位小数`);
  return cents;
}
function centsFrom(input, key, optional = false, zero = false) {
  const centKey = `${key}Cents`;
  if (input[centKey] != null) return integer(input[centKey], centKey, zero ? 0 : 1);
  if (input[key] != null) return money(input[key], key, zero);
  if (optional) return null;
  fail(`缺少${key}`);
}
function amount(cents) { return cents == null ? null : cents / 100; }
function product(a, b, label = '金额') { return integer(a * b, label); }
function portion(total, n, d) { return Number(BigInt(total) * BigInt(n) / BigInt(d)); }
function timestamp(value, label = '时间') {
  if (typeof value !== 'string' || !/T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value) || !Number.isFinite(Date.parse(value))) fail(`${label}需要含时区的时间戳`);
  return new Date(value).toISOString();
}
function date(value, label = '日期') {
  if (typeof value !== 'string' || !DAY.test(value) || !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) || new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value) fail(`${label}格式无效`);
  return value;
}
function shanghaiDay(value) { return new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 10); }
function sym(value) {
  if (typeof value !== 'string') fail('股票代码无效');
  const s = value.toUpperCase().replace(/^(SH|SZ)/, '').replace(/\.(SH|SZ)$/, '');
  if (!/^\d{6}$/.test(s)) fail('股票代码必须为六位数字');
  return s;
}
function identifier(value, label = 'ID') { if (typeof value !== 'string' || !ID.test(value)) fail(`${label}无效`); return value; }
function textField(value, label, limit = 200) { if (typeof value !== 'string' || value.length > limit) fail(`${label}无效`); return value; }
function plain(value) { return value != null && typeof value === 'object' && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null); }
function safeJson(value, depth = 0) {
  if (depth > 24) fail('数据层级过深');
  if (value == null || typeof value === 'boolean') return;
  if (typeof value === 'number') { if (!Number.isFinite(value)) fail('数据包含非有限数值'); return; }
  if (typeof value === 'string') { if (value.length > 100000) fail('文本过长'); return; }
  if (Array.isArray(value)) { if (value.length > MAX_ITEMS) fail('数据条目过多'); value.forEach(v => safeJson(v, depth + 1)); return; }
  if (!plain(value)) fail('数据必须为普通JSON对象');
  for (const key of Object.keys(value)) {
    if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('禁止的对象字段');
    safeJson(value[key], depth + 1);
  }
}
function clone(value) { safeJson(value); return JSON.parse(JSON.stringify(value)); }
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (plain(value)) return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}
function hash(value) {
  let h = 2166136261;
  for (const c of canonical(value)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
}
function uniqueId(prefix) { return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`; }
function normalizedConfig(config = {}) {
  if (!plain(config)) fail('配置无效');
  const result = { ...DEFAULT_CONFIG, ...clone(config) };
  integer(result.maxHoldSessions, '跟踪交易日', 1);
  if (result.maxHoldSessions > 1000) fail('跟踪期限过长');
  if (!Array.isArray(result.buyFractions) || result.buyFractions.length < 2 || result.buyFractions.length > 3 || result.buyFractions.some(x => typeof x !== 'number' || !Number.isFinite(x) || x <= 0 || x > 1) || Math.abs(result.buyFractions.reduce((a, b) => a + b, 0) - 1) > 1e-8) fail('买入计划比例须为2至3档且合计100%');
  if (!Number.isFinite(result.quoteMaxAgeMinutes) || result.quoteMaxAgeMinutes < 1 || result.quoteMaxAgeMinutes > 1440) fail('报价有效期无效');
  return result;
}
export function newState({ config = {}, now = new Date().toISOString() } = {}) {
  now = timestamp(now);
  return { schema: SCHEMA, version: VERSION, strategyId: 'LBR_MAIN_V1', config: normalizedConfig(config), createdAt: now, updatedAt: now, events: [], fills: [], quotes: {}, observations: [] };
}
function assertState(state) { const r = validateState(state); if (!r.valid) fail(`账本无效：${r.errors.join('；')}`); }
function sortedFills(state) { return [...state.fills].sort((a, b) => a.executedAt.localeCompare(b.executedAt) || (a.side === b.side ? a.id.localeCompare(b.id) : a.side === 'BUY' ? -1 : 1)); }
function feeAllocation(buy, soldBefore, qty) { return portion(buy.feesCents, soldBefore + qty, buy.qty) - portion(buy.feesCents, soldBefore, buy.qty); }
function lotsFor(state, symbol, before = null) {
  const buys = new Map();
  for (const f of sortedFills(state)) {
    if (f.symbol !== symbol || (before && f.executedAt > before)) continue;
    if (f.side === 'BUY') buys.set(f.id, { ...f, soldQty: 0 });
    else for (const a of f.allocations) { const lot = buys.get(a.buyFillId); if (lot) lot.soldQty += a.qty; }
  }
  return [...buys.values()].map(b => ({ ...b, remainingQty: b.qty - b.soldQty, remainingFeesCents: b.feesCents - portion(b.feesCents, b.soldQty, b.qty) }));
}

/** Returns diagnostics and never mutates the supplied value. Unsupported versions fail closed. */
export function validateState(state) {
  try {
    safeJson(state);
    if (!plain(state) || state.schema !== SCHEMA || state.version !== VERSION || state.strategyId !== 'LBR_MAIN_V1') fail('不支持的账本schema/version/strategyId');
    normalizedConfig(state.config);
    if (timestamp(state.createdAt) !== state.createdAt || timestamp(state.updatedAt) !== state.updatedAt) fail('账本时间须为标准UTC格式');
    for (const key of ['events', 'fills', 'observations']) if (!Array.isArray(state[key]) || state[key].length > MAX_ITEMS) fail(`${key}无效或过大`);
    if (!plain(state.quotes)) fail('quotes无效');
    const eventMap = new Map();
    for (const e of state.events) {
      identifier(e.id, '事件ID'); if (sym(e.symbol) !== e.symbol) fail('事件股票代码须为六位数字');
      if (eventMap.has(e.id)) fail('事件ID重复');
      eventMap.set(e.id, e);
      if (!Array.isArray(e.snapshots) || e.snapshots.length === 0 || e.snapshots.length > 10000) fail('事件快照无效');
      const seen = new Set();
      for (const s of e.snapshots) { identifier(s.id, '快照ID'); if (timestamp(s.recordedAt) !== s.recordedAt) fail('快照时间须为标准UTC格式'); if (!plain(s.data) || seen.has(s.id)) fail('事件快照无效或重复'); seen.add(s.id); }
    }
    const fillMap = new Map(), sold = new Map();
    let aggregateCashCents = 0;
    for (const f of sortedFills(state)) {
      identifier(f.id, '成交ID'); if (sym(f.symbol) !== f.symbol) fail('成交股票代码须为六位数字'); textField(f.name, '股票名称'); if (timestamp(f.executedAt) !== f.executedAt) fail('成交时间须为标准UTC格式'); date(f.tradeDate);
      if (Object.hasOwn(f, 'note')) textField(f.note, '成交备注', 240);
      if (shanghaiDay(f.executedAt) !== f.tradeDate) fail('成交日期与上海时间不一致');
      integer(f.priceCents, '成交价', 1); integer(f.qty, '股数', 1); integer(f.feesCents, '费用');
      aggregateCashCents = integer(aggregateCashCents + product(f.priceCents, f.qty) + f.feesCents, '账本累计成交额');
      if (fillMap.has(f.id)) fail('成交ID重复');
      if (f.side === 'BUY') {
        const event = eventMap.get(f.eventId);
        if (!event || event.symbol !== f.symbol || !event.snapshots.some(s => s.id === f.eventSnapshotId)) fail('买入缺少对应事件快照');
        if (f.planFraction != null && (typeof f.planFraction !== 'number' || f.planFraction <= 0 || f.planFraction > 1)) fail('计划比例无效');
        if (!plain(f.entry)) fail('入场冻结参数无效');
        for (const key of ['highCents', 'lowCents', 'tp1Cents', 'tp2Cents', 'stopCents', 'structuralSupportCents', 'atr14Cents']) if (f.entry[key] != null) integer(f.entry[key], key, 1);
        if (f.entry.highCents != null && f.entry.lowCents != null) {
          if (f.entry.highCents < f.entry.lowCents) fail('入场高低点无效');
          if (f.entry.tp1Cents !== f.entry.lowCents + portion(f.entry.highCents - f.entry.lowCents, 382, 1000) || f.entry.tp2Cents !== f.entry.lowCents + portion(f.entry.highCents - f.entry.lowCents, 618, 1000)) fail('冻结止盈价与公式不符');
        } else if (f.entry.tp1Cents != null || f.entry.tp2Cents != null) fail('目标价格缺少入场高低点');
        if (f.entry.knownAt != null && timestamp(f.entry.knownAt) > f.executedAt) fail('入场参数包含未来数据');
      } else if (f.side === 'SELL') {
        if (!Array.isArray(f.allocations) || f.allocations.length === 0) fail('卖出缺少批次分配');
        let total = 0; const allocationIds = new Set();
        for (const a of f.allocations) {
          const b = fillMap.get(a.buyFillId);
          if (!b || b.side !== 'BUY' || b.symbol !== f.symbol || b.tradeDate >= f.tradeDate || allocationIds.has(a.buyFillId)) fail('卖出批次无效或违反T+1');
          integer(a.qty, '分配股数', 1);
          const previous = sold.get(b.id) || 0;
          if (previous + a.qty > b.qty) fail('卖出超过实际买入余量');
          const expectedFee = feeAllocation(b, previous, a.qty);
          if (a.entryFeesCents !== expectedFee || a.costCents !== product(b.priceCents, a.qty) + expectedFee) fail('卖出成本或费用分配被改动');
          sold.set(b.id, previous + a.qty); total += a.qty; allocationIds.add(a.buyFillId);
        }
        if (total !== f.qty) fail('卖出分配数量不符');
      } else fail('未知成交方向');
      fillMap.set(f.id, f);
    }
    const observations = new Set();
    for (const q of [...Object.values(state.quotes), ...state.observations]) { if (sym(q.symbol) !== q.symbol) fail('报价股票代码须为六位数字'); integer(q.priceCents, '报价', 1); if (timestamp(q.timestamp) !== q.timestamp) fail('报价时间须为标准UTC格式'); textField(q.source, '报价来源'); }
    for (const [symbol, q] of Object.entries(state.quotes)) if (symbol !== q.symbol) fail('报价股票代码不一致');
    for (const o of state.observations) { identifier(o.id, '报价观测ID'); if (observations.has(o.id)) fail('报价观测ID重复'); observations.add(o.id); }
    return { valid: true, errors: [] };
  } catch (error) { return { valid: false, errors: [error.message] }; }
}

function addEvents(next, events, now) {
  for (const raw of events) {
    if (!plain(raw)) fail('事件必须为对象');
    const symbol = sym(raw.symbol || raw.code);
    const id = identifier(raw.eventId || raw.event_id || raw.id, '事件ID');
    const data = clone(raw.snapshot || raw.data || raw);
    const recordedAt = timestamp(raw.recordedAt || raw.updatedAt || raw.report_timestamp || now);
    const snapshotId = identifier(raw.snapshotId || `snapshot-${hash({ id, data })}`, '快照ID');
    let event = next.events.find(e => e.id === id);
    if (event && event.symbol !== symbol) fail('相同事件ID对应不同股票');
    if (!event) { event = { id, symbol, snapshots: [] }; next.events.push(event); }
    const existing = event.snapshots.find(s => s.id === snapshotId);
    if (existing && canonical(existing.data) !== canonical(data)) fail('相同快照ID内容冲突');
    if (!existing) event.snapshots.push({ id: snapshotId, recordedAt, data });
  }
}
/** Persist all observed candidate/rejected/unfilled events, not just purchased winners. */
export function recordEvents(state, events, { now = new Date().toISOString() } = {}) {
  assertState(state); now = timestamp(now);
  if (!Array.isArray(events)) fail('事件列表无效');
  const next = clone(state); addEvents(next, events, now); next.updatedAt = now; assertState(next); return next;
}
function freezeEntry(input = {}, executedAt) {
  if (!plain(input)) fail('入场参数无效');
  const highCents = centsFrom(input, 'high', true), lowCents = centsFrom(input, 'low', true);
  const structuralSupportCents = centsFrom(input, 'structuralSupport', true), atr14Cents = centsFrom(input, 'atr14', true);
  let stopCents = centsFrom(input, 'stop', true);
  if (highCents != null && lowCents != null && highCents < lowCents) fail('入场高点不能低于低点');
  if (stopCents == null && lowCents != null && structuralSupportCents != null && atr14Cents != null) {
    const buffer = Math.max(Math.ceil(atr14Cents / 2), Math.ceil(lowCents / 100));
    const computed = Math.min(structuralSupportCents, lowCents) - buffer;
    if (computed > 0) stopCents = computed;
  }
  const ready = highCents != null && lowCents != null;
  const knownAt = input.knownAt == null ? null : timestamp(input.knownAt);
  if (knownAt != null && knownAt > executedAt) fail('不能使用成交后才知道的高低点');
  return { highCents, lowCents, structuralSupportCents, atr14Cents, stopCents, tp1Cents: ready ? lowCents + portion(highCents - lowCents, 382, 1000) : null, tp2Cents: ready ? lowCents + portion(highCents - lowCents, 618, 1000) : null, knownAt };
}
function baseFill(input, side) {
  if (!plain(input)) fail('成交输入无效');
  const executedAt = timestamp(input.executedAt);
  const tradeDate = date(input.tradeDate || shanghaiDay(executedAt));
  if (tradeDate !== shanghaiDay(executedAt)) fail('成交日与上海日期不一致');
  const f = { id: identifier(input.id || uniqueId('fill')), side, symbol: sym(input.symbol), name: textField(input.name || input.symbol, '股票名称'), executedAt, tradeDate, priceCents: centsFrom(input, 'price'), qty: integer(input.qty, '成交股数', 1), feesCents: centsFrom(input, 'fees', true, true) ?? 0 };
  // Omit empty optional notes so an older fill without this field can be retried.
  if (input.note != null) { const note = textField(input.note, '成交备注', 240); if (note) f.note = note; }
  product(f.priceCents, f.qty); return f;
}
function appendFill(next, fill) {
  const previous = next.fills.find(f => f.id === fill.id);
  if (previous && canonical(previous) !== canonical(fill)) fail('成交ID已存在但内容不同，拒绝覆盖');
  if (!previous) next.fills.push(fill);
  next.updatedAt = new Date(Math.max(Date.parse(next.updatedAt), Date.parse(fill.executedAt))).toISOString();
  assertState(next); return next;
}
function checkSnapshotTiming(snapshot, fill) {
  // recordedAt is a logging time, while these fields explicitly describe when
  // the snapshot's source data or calculation was available.
  for (const key of ['quoteTimestamp', 'generatedAt', 'asOf']) {
    const value = snapshot?.[key];
    if (typeof value !== 'string') continue;
    if (DAY.test(value)) {
      if (Number.isFinite(Date.parse(value)) && value > fill.tradeDate) fail(`事件快照${key}晚于实际成交；历史补录请提供当时已知的手动快照`);
    } else {
      const parsed = Date.parse(value);
      if (Number.isFinite(parsed) && parsed > Date.parse(fill.executedAt)) fail(`事件快照${key}晚于实际成交；历史补录请提供当时已知的手动快照`);
    }
  }
}
export function recordBuy(state, input) {
  assertState(state);
  const f = baseFill(input, 'BUY');
  f.eventId = identifier(input.eventId, '事件ID');
  f.planFraction = input.planFraction ?? null;
  f.entry = freezeEntry(input.entry || {}, f.executedAt);
  const previousFill = state.fills.find(x => x.id === f.id);
  if (previousFill) {
    const reference = { ...previousFill }; delete reference.eventSnapshotId;
    if (canonical(reference) !== canonical(f) || (input.eventSnapshotId != null && input.eventSnapshotId !== previousFill.eventSnapshotId)) fail('成交ID已存在但内容不同，拒绝覆盖');
    if (input.eventSnapshot != null) {
      const snapshot = state.events.find(e => e.id === previousFill.eventId)?.snapshots.find(s => s.id === previousFill.eventSnapshotId);
      if (!snapshot || canonical(snapshot.data) !== canonical(input.eventSnapshot)) fail('成交ID的事件快照内容冲突');
    }
    return clone(state);
  }
  const next = clone(state);
  const event = next.events.find(e => e.id === f.eventId);
  if (input.eventSnapshot != null) checkSnapshotTiming(input.eventSnapshot, f);
  else if (event) {
    const referenced = input.eventSnapshotId ? event.snapshots.find(s => s.id === input.eventSnapshotId) : event.snapshots.at(-1);
    if (referenced && referenced.recordedAt > f.executedAt) fail('现有事件快照记录晚于实际成交；历史补录请显式提供当时已知的手动快照');
    if (referenced) checkSnapshotTiming(referenced.data, f);
  }
  if (input.eventSnapshot != null || !event) addEvents(next, [{ eventId: f.eventId, symbol: f.symbol, snapshot: input.eventSnapshot || { manualFill: true, symbol: f.symbol, eventId: f.eventId }, recordedAt: f.executedAt }], f.executedAt);
  const snapshots = next.events.find(e => e.id === f.eventId).snapshots;
  f.eventSnapshotId = input.eventSnapshotId || snapshots[snapshots.length - 1].id;
  return appendFill(next, f);
}

function calendarInfo(input) {
  if (input == null) return null;
  const days = Array.isArray(input) ? input : input.sessions;
  if (!Array.isArray(days) || !days.length) return null;
  const sessions = days.map(x => date(typeof x === 'string' ? x : x.date));
  if (new Set(sessions).size !== sessions.length || sessions.some((x, i) => i && x <= sessions[i - 1])) fail('交易日历必须有序且无重复');
  const coveredFrom = Array.isArray(input) ? sessions[0] : date(input.coveredFrom || sessions[0]);
  const coveredTo = Array.isArray(input) ? sessions[sessions.length - 1] : date(input.coveredTo || sessions[sessions.length - 1]);
  if (coveredFrom > sessions[0] || coveredTo < sessions[sessions.length - 1]) fail('交易日历覆盖范围无效');
  return { sessions, coveredFrom, coveredTo };
}
function sessionStatus(calendar, firstBuyDate, asOf, length) {
  const unknown = { heldSessions: null, expiryDate: null, due: false, calendarUnknown: true };
  if (!calendar || firstBuyDate > asOf || firstBuyDate < calendar.coveredFrom || asOf > calendar.coveredTo) return unknown;
  const index = calendar.sessions.indexOf(firstBuyDate);
  if (index < 0) return unknown;
  const heldSessions = calendar.sessions.filter(d => d >= firstBuyDate && d <= asOf).length;
  return { heldSessions, expiryDate: calendar.sessions[index + length - 1] || null, due: heldSessions >= length, calendarUnknown: false };
}
function isSellable(lot, day, calendar) { return !!calendar && day >= calendar.coveredFrom && day <= calendar.coveredTo && calendar.sessions.includes(day) && calendar.sessions.includes(lot.tradeDate) && lot.tradeDate < day; }
export function recordSell(state, input, { sessions } = {}) {
  assertState(state);
  const f = baseFill(input, 'SELL');
  const existing = state.fills.find(x => x.id === f.id);
  // Retrying an acknowledged fill must not consume its shares a second time.
  if (existing) {
    if (existing.side !== 'SELL' || ['symbol', 'executedAt', 'tradeDate', 'priceCents', 'qty', 'feesCents'].some(k => existing[k] !== f[k]) || (input.reason ?? '') !== existing.reason || (existing.note ?? '') !== (f.note ?? '')) fail('成交ID已存在但内容不同，拒绝覆盖');
    return clone(state);
  }
  const calendar = calendarInfo(sessions);
  if (!calendar) fail('真实交易日历未知，无法确认T+1可卖数量');
  const lots = lotsFor(state, f.symbol, f.executedAt).filter(l => l.remainingQty > 0 && isSellable(l, f.tradeDate, calendar));
  if (lots.reduce((sum, l) => sum + l.remainingQty, 0) < f.qty) fail('成交股数超过T+1可卖余量或日历覆盖不足');
  let remaining = f.qty;
  f.allocations = [];
  for (const lot of lots) {
    const qty = Math.min(lot.remainingQty, remaining);
    if (!qty) continue;
    const entryFeesCents = feeAllocation(lot, lot.soldQty, qty);
    f.allocations.push({ buyFillId: lot.id, qty, entryFeesCents, costCents: product(lot.priceCents, qty) + entryFeesCents });
    remaining -= qty;
  }
  f.reason = textField(input.reason || '', '卖出原因', 1000);
  return appendFill(clone(state), f);
}

function normalizedQuote(q, defaultSymbol) {
  if (!plain(q)) fail('报价无效');
  return { symbol: sym(q.symbol || defaultSymbol), priceCents: centsFrom(q, 'price'), timestamp: timestamp(q.timestamp), source: textField(q.source || 'manual-quote', '报价来源') };
}
export function applyQuotes(state, quotes, { now = new Date().toISOString(), maxAgeMinutes = state.config.quoteMaxAgeMinutes } = {}) {
  assertState(state); now = timestamp(now);
  if (!Number.isFinite(maxAgeMinutes) || maxAgeMinutes <= 0) fail('报价时效参数无效');
  const list = Array.isArray(quotes) ? quotes.map(q => normalizedQuote(q)) : plain(quotes) ? Object.entries(quotes).map(([s, q]) => normalizedQuote(q, s)) : fail('报价列表无效');
  const next = clone(state);
  for (const q of list) {
    if (q.timestamp > now) continue;
    const existing = next.quotes[q.symbol];
    if (existing && existing.timestamp > q.timestamp) continue;
    if (existing && existing.timestamp === q.timestamp && existing.priceCents !== q.priceCents) fail('同一时间戳的报价内容冲突');
    next.quotes[q.symbol] = q;
    const fresh = Date.parse(now) - Date.parse(q.timestamp) <= maxAgeMinutes * 60000;
    if (!fresh || !next.fills.some(f => f.side === 'BUY' && f.symbol === q.symbol && f.executedAt <= q.timestamp)) continue;
    const observation = { ...q, id: `quote-${q.symbol}-${q.timestamp.replace(/[^\d]/g, '')}` };
    const previous = next.observations.find(o => o.id === observation.id);
    if (previous && previous.priceCents !== observation.priceCents) fail('报价观测冲突');
    if (!previous) next.observations.push(observation);
  }
  next.updatedAt = now; assertState(next); return next;
}

function currentCycle(state, symbol) {
  let qty = 0, cycle = [];
  for (const f of sortedFills(state).filter(f => f.symbol === symbol)) {
    if (qty === 0 && f.side === 'BUY') cycle = [];
    cycle.push(f); qty += f.side === 'BUY' ? f.qty : -f.qty;
  }
  return qty > 0 ? cycle : [];
}
export function holdings(state, { sessions, asOf, now = new Date().toISOString(), maxQuoteAgeMinutes = state.config.quoteMaxAgeMinutes } = {}) {
  assertState(state); now = timestamp(now); asOf = date(asOf || shanghaiDay(now));
  // A historical view must not include later fills, even if the full backup has them.
  state = { ...state, fills: state.fills.filter(f => f.executedAt <= now && f.tradeDate <= asOf) };
  const calendar = calendarInfo(sessions), result = [];
  for (const symbol of [...new Set(state.fills.filter(f => f.side === 'BUY').map(f => f.symbol))].sort()) {
    const cycle = currentCycle(state, symbol);
    if (!cycle.length) continue;
    const buys = cycle.filter(f => f.side === 'BUY'), first = buys[0], lots = lotsFor(state, symbol).filter(l => l.remainingQty > 0);
    const qty = lots.reduce((sum, l) => sum + l.remainingQty, 0);
    const costCents = lots.reduce((sum, l) => sum + product(l.priceCents, l.remainingQty) + l.remainingFeesCents, 0);
    const priceBasisCents = lots.reduce((sum, l) => sum + product(l.priceCents, l.remainingQty), 0);
    integer(costCents, '持仓总成本');
    const status = sessionStatus(calendar, first.tradeDate, asOf, state.config.maxHoldSessions);
    const canAssessSellable = !!calendar && asOf >= calendar.coveredFrom && asOf <= calendar.coveredTo && lots.every(l => calendar.sessions.includes(l.tradeDate));
    const lastSession = calendar?.sessions.filter(d => d <= asOf).at(-1);
    const sellableQty = canAssessSellable ? lots.reduce((sum, l) => sum + (l.tradeDate < (lastSession || asOf) ? l.remainingQty : 0), 0) : null;
    const q = state.quotes[symbol], quoteAgeMinutes = q ? (Date.parse(now) - Date.parse(q.timestamp)) / 60000 : null;
    const stale = !q || quoteAgeMinutes < 0 || quoteAgeMinutes > maxQuoteAgeMinutes;
    const observed = state.observations.filter(o => o.symbol === symbol && o.timestamp >= first.executedAt && o.timestamp <= now).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    // Daily high/low fields are deliberately ignored: they may predate the actual fill.
    const points = [...buys.map(b => ({ timestamp: b.executedAt, priceCents: b.priceCents })), ...observed].filter(p => p.timestamp <= now).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    const highCents = points.length ? Math.max(...points.map(o => o.priceCents)) : null, lowCents = points.length ? Math.min(...points.map(o => o.priceCents)) : null;
    const gaps = points.slice(1).filter((p, i) => Date.parse(p.timestamp) - Date.parse(points[i].timestamp) > maxQuoteAgeMinutes * 60000).length;
    const netRealizedCents = cycle.filter(f => f.side === 'SELL').reduce((sum, s) => sum + product(s.priceCents, s.qty) - s.feesCents - s.allocations.reduce((a, b) => a + b.costCents, 0), 0);
    const marketValueCents = q && !stale ? product(q.priceCents, qty) : null;
    const unrealizedPnlCents = marketValueCents == null ? null : marketValueCents - costCents;
    const targets = lots.map(l => ({ buyFillId: l.id, eventId: l.eventId, qty: l.remainingQty, boughtAt: l.executedAt, high: amount(l.entry.highCents), low: amount(l.entry.lowCents), tp1: amount(l.entry.tp1Cents), tp2: amount(l.entry.tp2Cents), stop: amount(l.entry.stopCents), tp1Cents: l.entry.tp1Cents, tp2Cents: l.entry.tp2Cents, stopCents: l.entry.stopCents, requiresExitReview: l.entry.tp1Cents == null || l.entry.stopCents == null || l.entry.tp1Cents <= l.priceCents, tp1Reached: !stale && l.entry.tp1Cents != null && q.priceCents >= l.entry.tp1Cents, tp2Reached: !stale && l.entry.tp2Cents != null && q.priceCents >= l.entry.tp2Cents, stopReached: !stale && l.entry.stopCents != null && q.priceCents <= l.entry.stopCents }));
    result.push({ symbol, name: buys.at(-1).name, qty, sellableQty, costCents, avgPrice: Math.round(priceBasisCents / qty) / 100, avgCost: Math.round(costCents / qty) / 100, firstBuyDate: first.tradeDate, firstBuyAt: first.executedAt, ...status, quoteTimestamp: q?.timestamp || null, quoteAgeMinutes, stale, marketPrice: q ? amount(q.priceCents) : null, marketValue: amount(marketValueCents), marketValueCents, unrealizedPnl: amount(unrealizedPnlCents), unrealizedPnlCents, netRealized: amount(netRealizedCents), netRealizedCents, pnl: unrealizedPnlCents == null ? null : amount(unrealizedPnlCents + netRealizedCents), high: amount(highCents), low: amount(lowCents), observedHigh: amount(highCents), observedLow: amount(lowCents), coverageGap: observed.length === 0 || gaps > 0 || stale, coverageGapCount: gaps, coverageNote: '仅记录成交后实际收到的离散报价；未覆盖时段的真实高低点未知', observationCount: observed.length, targets, buyCount: buys.length, fills: clone(cycle) });
  }
  return result;
}

export function learningSummary(state) {
  assertState(state);
  const samples = state.events.map(event => ({ eventId: event.id, symbol: event.symbol, snapshots: clone(event.snapshots), fills: [], buyQty: 0, sellQty: 0, buyCostCents: 0, netProceedsCents: 0, netPnlCents: null, status: 'UNFILLED' }));
  const byEvent = new Map(samples.map(s => [s.eventId, s])), byFill = new Map(state.fills.map(f => [f.id, f]));
  for (const f of sortedFills(state)) {
    if (f.side === 'BUY') {
      const s = byEvent.get(f.eventId); s.buyQty += f.qty; s.buyCostCents += product(f.priceCents, f.qty) + f.feesCents; s.fills.push(clone(f));
    } else {
      let assignedQty = 0, assignedFees = 0;
      for (const a of f.allocations) {
        const s = byEvent.get(byFill.get(a.buyFillId).eventId);
        const nextFees = portion(f.feesCents, assignedQty + a.qty, f.qty), fees = nextFees - assignedFees;
        assignedFees = nextFees; assignedQty += a.qty;
        s.sellQty += a.qty; s.netProceedsCents += product(f.priceCents, a.qty) - fees;
        let journal = s.fills.find(x => x.id === f.id);
        if (!journal) { journal = clone(f); journal.attributedQty = 0; journal.attributedFeesCents = 0; s.fills.push(journal); }
        journal.attributedQty += a.qty; journal.attributedFeesCents += fees;
      }
    }
  }
  for (const s of samples) {
    if (!s.buyQty) continue;
    s.status = s.sellQty === s.buyQty ? 'CLOSED' : 'OPEN';
    if (s.status === 'CLOSED') s.netPnlCents = s.netProceedsCents - s.buyCostCents;
    s.netPnl = amount(s.netPnlCents);
    s.netReturnPct = s.netPnlCents == null ? null : 100 * s.netPnlCents / s.buyCostCents;
  }
  const closedTrades = samples.filter(s => s.status === 'CLOSED'), closedEventCount = closedTrades.length;
  const wins = closedTrades.filter(s => s.netPnlCents > 0).length, losses = closedTrades.filter(s => s.netPnlCents < 0).length;
  const stage = closedEventCount < 30 ? 'INSUFFICIENT' : closedEventCount < 100 ? 'DESCRIPTIVE_ONLY' : 'RESEARCH_ONLY';
  return { eventCount: samples.length, independentEventCount: samples.length, closedEventCount, openEventCount: samples.filter(s => s.status === 'OPEN').length, unfilledEventCount: samples.filter(s => s.status === 'UNFILLED').length, fillCount: state.fills.length, wins, losses, breakeven: closedEventCount - wins - losses, winRate: closedEventCount < 30 ? null : wins / closedEventCount, winRatePct: closedEventCount < 30 ? null : 100 * wins / closedEventCount, stage, eligibleForRecommendationTuning: false, message: stage === 'INSUFFICIENT' ? '独立已平仓事件不足30，仅积累样本，不推断胜率。' : stage === 'DESCRIPTIVE_ONLY' ? '30至99个独立已平仓事件：仅描述已记录结果，不能推断推荐更精准。' : '达到100个独立已平仓事件：仅可用于研究，参数调整必须另经样本外验证。', netRealized: amount(closedTrades.reduce((sum, s) => sum + s.netPnlCents, 0)), closedTrades, samples };
}

function mergeUnique(local, incoming, label) {
  const map = new Map(local.map(item => [item.id, clone(item)]));
  for (const item of incoming) {
    const existing = map.get(item.id);
    if (existing && canonical(existing) !== canonical(item)) fail(`${label}相同ID内容冲突，拒绝覆盖`);
    if (!existing) map.set(item.id, clone(item));
  }
  return [...map.values()];
}
/** Full validation occurs before returning; a backup can only add history, never erase fills. */
export function mergeState(local, incoming) {
  assertState(local);
  if (typeof incoming === 'string') { if (incoming.length > 30000000) fail('导入文件过大'); try { incoming = JSON.parse(incoming); } catch { fail('导入文件不是有效JSON'); } }
  assertState(incoming);
  const next = clone(local), events = new Map(next.events.map(e => [e.id, e]));
  for (const e of incoming.events) {
    const previous = events.get(e.id);
    if (previous && previous.symbol !== e.symbol) fail('事件ID股票冲突');
    if (previous) {
      for (const snapshot of e.snapshots) {
        const existing = previous.snapshots.find(s => s.id === snapshot.id);
        if (existing && canonical(existing.data) !== canonical(snapshot.data)) fail('事件快照相同ID内容冲突，拒绝覆盖');
        if (!existing) previous.snapshots.push(clone(snapshot));
        else if (snapshot.recordedAt < existing.recordedAt) existing.recordedAt = snapshot.recordedAt;
      }
    }
    else { const copy = clone(e); events.set(e.id, copy); }
  }
  next.events = [...events.values()];
  next.fills = mergeUnique(next.fills, incoming.fills, '成交');
  next.observations = mergeUnique(next.observations, incoming.observations, '报价观测');
  for (const [symbol, q] of Object.entries(incoming.quotes)) {
    const previous = next.quotes[symbol];
    if (previous && previous.timestamp === q.timestamp && previous.priceCents !== q.priceCents) fail('报价冲突');
    if (!previous || previous.timestamp < q.timestamp) next.quotes[symbol] = clone(q);
  }
  next.createdAt = local.createdAt < incoming.createdAt ? local.createdAt : incoming.createdAt;
  next.updatedAt = local.updatedAt > incoming.updatedAt ? local.updatedAt : incoming.updatedAt;
  // Local configuration is deliberate; importing another device's backup cannot change it.
  assertState(next); return next;
}
export function exportState(state) { assertState(state); return JSON.stringify(state, null, 2); }

/** A budget helper only. A click never creates a trade: actual price and quantity are still required. */
export function suggestedQuantity({ budget, fraction, price, lotSize = 100 }) {
  const budgetCents = money(budget, '计划资金', true), priceCents = money(price, '参考价');
  if (!Number.isFinite(fraction) || fraction <= 0 || fraction > 1) fail('计划比例无效');
  integer(lotSize, '整手数量', 1);
  // Avoid 0.3 * integer rounding just below a cent boundary.
  const fractionUnits = Math.round(fraction * 100000000);
  const allocatedCents = portion(budgetCents, fractionUnits, 100000000);
  return { qty: Math.floor(allocatedCents / priceCents / lotSize) * lotSize, allocatedBudget: amount(allocatedCents), fraction };
}
