import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as portfolio from '../portfolio.js';
import * as demo from '../demo.js';

// Exercise the real form/refresh handlers against the real ledger. The tiny
// element adapter supplies input values; these tests make no markup assertions.
const appSource = readFileSync(new URL('../app.js', import.meta.url), 'utf8');
const handlers = appSource.slice(0, appSource.indexOf("document.addEventListener('click'"))
  .replace(/^import .*;\r?\n/gm, '');
const NOW = '2026-05-11T06:30:00.000Z';
const SESSIONS = ['2026-05-06', '2026-05-07', '2026-05-08', '2026-05-11'];

function harness(files = {}) {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', textContent: '', hidden: false, disabled: false, close() {},
    });
    return elements.get(selector);
  };
  const storage = new Map();
  const calls = [];
  const controls = {now:NOW, files, calls, quoteResult:null};
  const make = new Function('portfolio', 'demo', 'document', 'localStorage', 'controls', `
    const {newState,recordBuy,recordSell,recordEvents,applyQuotes,holdings,learningSummary,mergeState,validateState,exportState,suggestedQuantity}=portfolio;
    const {demoData,demoBuys,demoQuotes,DEMO_NOW}=demo;
    ${handlers}
    render=()=>{}; notice=()=>{}; toast=()=>{};
    now=()=>controls.now;
    fetchJSON=async path=>{
      if(!(path in controls.files))throw Error('Missing fixture '+path);
      return controls.files[path];
    };
    jsonpQuote=async code=>{
      controls.calls.push(code);
      if(controls.quoteResult)return controls.quoteResult;
      return {symbol:code,price:10,timestamp:controls.now,source:'integration-fixture'};
    };
    return {ctx,saveTrade,refresh,refreshLiveQuotes,held,importFile};
  `);
  const app = make(portfolio, demo, {querySelector:element, hidden:false}, {
    getItem:key=>storage.get(key)||null,
    setItem:(key,value)=>storage.set(key,value),
  }, controls);
  app.ctx.ledger = portfolio.newState({now:NOW});
  app.ctx.data = {calendar:{verified:true,sessions:SESSIONS},candidates:[]};
  const input = ({symbol='600002', date='2026-05-08T14:00', id='new-buy'} = {}) => {
    app.ctx.trade = {type:'buy',c:null,fraction:.3,id};
    for (const [selector,value] of Object.entries({
      '#trade-symbol':symbol, '#trade-name':'研究样本', '#trade-price':'10',
      '#trade-quantity':'100', '#trade-fee':'0', '#trade-time':date,
      '#entry-high':'12', '#entry-low':'9.5', '#entry-stop':'9', '#trade-note':'',
    })) element(selector).value=value;
    app.saveTrade({preventDefault(){}});
  };
  return {...app, element, input, calls, storage, controls};
}

function buy(state, symbol, id, eventId, day='2026-05-06') {
  return portfolio.recordBuy(state, {
    id,symbol,name:'研究样本',eventId,price:10,qty:100,fees:0,
    executedAt:`${day}T10:00:00+08:00`,entry:{high:12,low:9.5,stop:9},
  });
}

test('manual form rejects future fills before writing the ledger', () => {
  const h=harness();
  h.input({date:'2026-05-12T10:00'});
  assert.equal(h.ctx.ledger.fills.length,0);
  assert.match(h.element('#trade-error').textContent,/未来/);
  assert.equal(h.storage.size,0);
});

test('a new manual round trip does not reopen a previously closed event', () => {
  const h=harness();
  let ledger=buy(h.ctx.ledger,'600002','old-buy','MANUAL:600002:2026-05-06');
  ledger=portfolio.recordSell(ledger,{
    id:'old-sell',symbol:'600002',name:'研究样本',price:11,qty:100,fees:0,
    executedAt:'2026-05-07T10:00:00+08:00',reason:'MANUAL',
  },{sessions:SESSIONS});
  h.ctx.ledger=ledger;
  h.input();
  assert.equal(h.element('#trade-error').textContent,'');
  const latest=h.ctx.ledger.fills.find(f=>f.id==='new-buy');
  assert.ok(latest,'the actual fill should be recorded');
  assert.notEqual(latest.eventId,'MANUAL:600002:2026-05-06');
  const summary=portfolio.learningSummary(h.ctx.ledger);
  assert.equal(summary.closedEventCount,1);
  assert.equal(summary.openEventCount,1);
});

test('minute quote refresh includes active holdings and candidates, excludes closed holdings', async () => {
  const h=harness();
  let ledger=buy(h.ctx.ledger,'600001','closed-buy','event-closed');
  ledger=portfolio.recordSell(ledger,{
    id:'closed-sell',symbol:'600001',name:'研究样本',price:11,qty:100,fees:0,
    executedAt:'2026-05-07T10:00:00+08:00',reason:'MANUAL',
  },{sessions:SESSIONS});
  h.ctx.ledger=buy(ledger,'600002','active-buy','event-active','2026-05-08');
  h.ctx.data.candidates=[{symbol:'600003.SH',code:'600003'}];
  await h.refreshLiveQuotes();
  assert.deepEqual([...h.calls].sort(),['600002','600003']);
});

test('snapshot refresh archives the full backend event universe and normalizes quote symbols', async () => {
  const candidate={eventId:'600007.SH:2026-05-08:01',symbol:'600007.SH',code:'600007',name:'候选样本',eventDate:'2026-05-08',quoteTimestamp:NOW,status:'WATCH',price:10};
  const rejected={eventId:'600008.SH:2026-05-08:01',symbol:'600008.SH',code:'600008',name:'被拒绝样本',eventDate:'2026-05-08',quoteTimestamp:NOW,status:'CANCEL',reasonCodes:['TREND_GATE']};
  const h=harness({
    'data/latest.json':{meta:{strategyId:'LBR_MAIN_V1',generatedAt:NOW,quoteTimestamp:NOW,stale:false},calendar:{verified:true,sessions:SESSIONS},candidates:[candidate]},
    'data/events.json':{strategyId:'LBR_MAIN_V1',events:[candidate,rejected]},
    'data/quotes.json':{meta:{source:'fixture'},quotes:{'600007.SH':{symbol:'600007.SH',price:10,quoteTimestamp:NOW,source:'fixture'}}},
  });
  await h.refresh({silent:true});
  assert.equal(h.ctx.ledger.quotes['600007'].priceCents,1000);
  assert.deepEqual(h.ctx.ledger.events.map(e=>e.id).sort(),[candidate.eventId,rejected.eventId].sort());
  assert.equal(portfolio.learningSummary(h.ctx.ledger).unfilledEventCount,2);
});

test('position display separates remaining unrealized gains from prior realized gains', () => {
  const h=harness();
  let ledger=buy(h.ctx.ledger,'600002','pnl-buy','event-pnl');
  ledger=portfolio.recordSell(ledger,{
    id:'pnl-sell',symbol:'600002',name:'研究样本',price:12,qty:50,fees:0,
    executedAt:'2026-05-07T10:00:00+08:00',reason:'TP1',
  },{sessions:SESSIONS});
  h.ctx.ledger=portfolio.applyQuotes(ledger,[{symbol:'600002',price:11,timestamp:NOW,source:'fixture'}],{now:NOW});
  const [position]=h.held();
  assert.equal(position.pnl,50);
  assert.equal(position.totalPnl,150);
  assert.equal(position.pnlPct,10);
});

test('production import rejects synthetic event-only ledgers even without fills', async () => {
  const h=harness();
  const synthetic=portfolio.recordEvents(portfolio.newState({now:NOW}),[
    {eventId:'DEMO:600001:2026-05-08',symbol:'600001',snapshot:{synthetic:true}},
  ],{now:NOW});
  const serialized=portfolio.exportState(synthetic);
  await h.importFile({size:serialized.length,text:async()=>serialized});
  assert.equal(h.ctx.ledger.events.length,0);
  assert.equal(h.storage.size,0);
});

test('demo import rejects a mixed synthetic and production event ledger', async () => {
  const h=harness(); h.ctx.demo=true;
  const mixed=portfolio.recordEvents(portfolio.newState({now:NOW}),[
    {eventId:'DEMO:600001:2026-05-08',symbol:'600001',snapshot:{synthetic:true}},
    {eventId:'600002.SH:2026-05-08:01',symbol:'600002',snapshot:{price:10}},
  ],{now:NOW});
  const serialized=portfolio.exportState(mixed);
  await h.importFile({size:serialized.length,text:async()=>serialized});
  assert.equal(h.ctx.ledger.events.length,0);
  assert.equal(h.storage.size,0);
});

test('a production snapshot response cannot overwrite a newly selected demo ledger', async () => {
  let resolveSnapshot;
  const pending=new Promise(resolve=>{resolveSnapshot=resolve;});
  const h=harness({
    'data/latest.json':pending,
    'data/events.json':{events:[]},
    'data/quotes.json':{quotes:{}},
  });
  const request=h.refresh({silent:true});
  h.ctx.generation++; h.ctx.demo=true;
  const demoLedger=portfolio.newState({now:NOW});
  h.ctx.ledger=demoLedger; h.ctx.data=demo.demoData();
  resolveSnapshot({meta:{strategyId:'LBR_MAIN_V1',generatedAt:NOW},calendar:{sessions:SESSIONS},candidates:[]});
  await request;
  assert.equal(h.ctx.ledger,demoLedger);
  assert.equal(h.ctx.data.meta.mode,'SYNTHETIC_DEMO');
  assert.equal(h.storage.size,0);
});

test('a late production browser quote cannot be persisted into demo storage', async () => {
  let resolveQuote;
  const h=harness();
  h.ctx.ledger=buy(h.ctx.ledger,'600002','race-buy','event-race');
  h.controls.quoteResult=new Promise(resolve=>{resolveQuote=resolve;});
  const request=h.refreshLiveQuotes();
  h.ctx.generation++; h.ctx.demo=true;
  const demoLedger=portfolio.newState({now:NOW}); h.ctx.ledger=demoLedger;
  resolveQuote({symbol:'600002',price:10,timestamp:NOW,source:'fixture'});
  await request;
  assert.equal(h.ctx.ledger,demoLedger);
  assert.equal(h.storage.size,0);
});
