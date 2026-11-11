import test from 'node:test';
import assert from 'node:assert/strict';
import {weeklyView,moveDate} from '../weekly.js';

const first={eventId:'a:mon',symbol:'600001.SH',price:10,status:'WATCH'};
const newer={eventId:'a:tue',symbol:'600001.SH',price:12,status:'WATCH'};
function snapshot(){return {candidates:[{...first,price:9,status:'CANCEL'},newer],pool:{mode:'WEEKLY_DAILY',weekStart:'2026-11-09',days:[{date:'2026-11-09',candidates:[first]},{date:'2026-11-10',candidates:[newer]}]}};}
test('default shows newest saved day while arrows navigate all five dates',()=>{
  const view=weeklyView(snapshot(),'2026-11-11');
  assert.equal(view.selected,'2026-11-10');
  assert.deepEqual(view.dates,['2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13']);
  assert.equal(moveDate(view,-1),'2026-11-09');
  assert.equal(moveDate(weeklyView(snapshot(),'2026-11-11','2026-11-09'),-1),'2026-11-09');
  assert.equal(moveDate(weeklyView(snapshot(),'2026-11-11','2026-11-13'),1),'2026-11-13');
});
test('past selection survives refresh and shows its own event with latest risk',()=>{
  const data=snapshot(),before=JSON.stringify(data);
  const view=weeklyView(data,'2026-11-11','2026-11-09');
  assert.equal(view.candidates[0].eventId,'a:mon');
  assert.equal(view.candidates[0].status,'CANCEL');
  assert.equal(view.candidates[0].price,9);
  assert.equal(view.candidates[0].recommendedPrice,10);
  assert.equal(JSON.stringify(data),before);
});
test('new Monday hides last week even if server has not refreshed yet',()=>{
  const view=weeklyView(snapshot(),'2026-11-16','2026-11-09');
  assert.equal(view.monday,'2026-11-16');
  assert.deepEqual(view.candidates,[]);
  assert.deepEqual(view.days,[]);
});
test('missing and empty days remain distinct without borrowing other lists',()=>{
  const data=snapshot();data.pool.days.push({date:'2026-11-11',candidates:[]});
  assert.ok(weeklyView(data,'2026-11-11','2026-11-11').day);
  assert.equal(weeklyView(data,'2026-11-11','2026-11-12').day,undefined);
  assert.deepEqual(weeklyView(data,'2026-11-11','2026-11-12').candidates,[]);
});
test('weekend keeps Friday history and crosses year boundaries by calendar',()=>{
  assert.equal(weeklyView(snapshot(),'2026-11-15').monday,'2026-11-09');
  assert.equal(weeklyView(null,'2027-01-01').monday,'2026-12-28');
});
test('an original ready snapshot without current evidence is observation only',()=>{
  const data=snapshot();data.candidates=[];data.pool.days[0].candidates[0]={...first,status:'BUY_READY'};
  const c=weeklyView(data,'2026-11-11','2026-11-09').candidates[0];
  assert.equal(c.status,'WATCH');assert.equal(c.stale,true);
});
test('legacy migration can expose only the one actual snapshot date',()=>{
  const data={meta:{generatedAt:'2026-11-10T10:00:00+08:00'},candidates:[first]};
  assert.equal(weeklyView(data,'2026-11-11').selected,'2026-11-10');
  assert.deepEqual(weeklyView(data,'2026-11-11','2026-11-09').candidates,[]);
});
