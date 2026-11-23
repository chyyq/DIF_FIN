import test from 'node:test';
import assert from 'node:assert/strict';
import {weeklyView,moveDate} from '../weekly.js';

const first={eventId:'a:mon',symbol:'600001.SH',price:10,status:'WATCH'};
const newer={eventId:'a:tue',symbol:'600001.SH',price:12,status:'WATCH'};
function snapshot(){return {candidates:[{...first,price:9,status:'CANCEL'},newer],pool:{mode:'WEEKLY_DAILY',weekStart:'2026-11-09',days:[{date:'2026-11-09',candidates:[first]},{date:'2026-11-10',candidates:[newer]}]}};}
function fullWeek(){
  const days=['09','10','11','12','13'].map((day,i)=>({date:`2026-11-${day}`,candidates:[{...first,eventId:`event:${i}`,symbol:`60000${i}.SH`}]}));
  return {pool:{mode:'WEEKLY_DAILY',weekStart:'2026-11-09',days},candidates:days.flatMap(d=>d.candidates)};
}

test('default shows newest saved day and missing slots represent most recent weekday dates',()=>{
  const view=weeklyView(snapshot(),'2026-11-11');
  assert.equal(view.selected,'2026-11-10');
  assert.deepEqual(view.dates,['2026-11-09','2026-11-10','2026-11-11','2026-11-05','2026-11-06']);
  assert.equal(moveDate(view,-1),'2026-11-09');
  assert.equal(moveDate(weeklyView(snapshot(),'2026-11-11','2026-11-09'),-1),'2026-11-09');
  assert.equal(moveDate(weeklyView(snapshot(),'2026-11-11','2026-11-06'),1),'2026-11-06');
  assert.equal(view.selectedIndex,1);
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
test('new Monday retains last weeks slots even before a server refresh',()=>{
  const view=weeklyView(fullWeek(),'2026-11-16','2026-11-10');
  assert.equal(view.monday,'2026-11-16');
  assert.equal(view.friday,'2026-11-20');
  assert.equal(view.days.length,5);
  assert.equal(view.selected,'2026-11-10');
  assert.equal(view.isCarried,true);
  assert.equal(view.selectedSlot.updateDue,false);
  assert.equal(view.slots[0].updateDue,true);
  assert.equal(view.selectedSlot.expectedDate,'2026-11-10');
});
test('new Monday coexists with old Tuesday-Friday in weekday navigation order',()=>{
  const data=fullWeek();
  data.pool.days[0]={date:'2026-11-16',candidates:[{...first,eventId:'new:mon'}]};
  data.candidates.push(data.pool.days[0].candidates[0]);
  const view=weeklyView(data,'2026-11-16');
  assert.deepEqual(view.dates,['2026-11-16','2026-11-10','2026-11-11','2026-11-12','2026-11-13']);
  assert.equal(view.selected,'2026-11-16');
  assert.equal(moveDate(view,1),'2026-11-10');
  assert.equal(moveDate(weeklyView(data,'2026-11-16','2026-11-13'),-1),'2026-11-12');
  assert.equal(view.isCarried,false);
});
test('a selected Tuesday follows its own replacement without jumping to another slot',()=>{
  const data=fullWeek();
  data.pool.days[1]={date:'2026-11-17',candidates:[newer]};
  const view=weeklyView(data,'2026-11-17','2026-11-10');
  assert.equal(view.selected,'2026-11-17');
  assert.equal(view.selectedIndex,1);
  assert.equal(view.candidates[0].eventId,newer.eventId);
});
test('a failed Tuesday keeps the old Tuesday date visibly marked pending replacement',()=>{
  const view=weeklyView(fullWeek(),'2026-11-17','2026-11-10');
  assert.equal(view.selected,'2026-11-10');
  assert.equal(view.isCarried,true);
  assert.equal(view.selectedSlot.updateDue,true);
  assert.equal(view.selectedSlot.expectedDate,'2026-11-17');
});
test('missing and successful empty slots remain distinct without borrowing other lists',()=>{
  const data=snapshot();data.pool.days.push({date:'2026-11-11',candidates:[]});
  assert.ok(weeklyView(data,'2026-11-11','2026-11-11').day);
  assert.equal(weeklyView(data,'2026-11-11','2026-11-05').day,undefined);
  assert.deepEqual(weeklyView(data,'2026-11-11','2026-11-05').candidates,[]);
});
test('weekend keeps Friday history and crosses year boundaries by calendar',()=>{
  const view=weeklyView(fullWeek(),'2026-11-15');
  assert.equal(view.monday,'2026-11-09');
  assert.equal(view.selected,'2026-11-13');
  assert.equal(weeklyView(null,'2027-01-01').monday,'2026-12-28');
  assert.equal(weeklyView(null,'2027-01-03').selected,'2027-01-01');
});
test('an original ready snapshot without current evidence becomes observation only',()=>{
  const data=snapshot();data.candidates=[];data.pool.days[0].candidates[0]={...first,status:'BUY_READY'};
  const c=weeklyView(data,'2026-11-16','2026-11-09').candidates[0];
  assert.equal(c.status,'WATCH');assert.equal(c.stale,true);
});
test('missing current evidence cannot revive an expired historical entry',()=>{
  const data=snapshot();data.candidates=[];data.pool.days[0].candidates[0]={...first,status:'EXPIRE'};
  const c=weeklyView(data,'2026-11-16','2026-11-09').candidates[0];
  assert.equal(c.status,'EXPIRE');assert.equal(c.stale,true);
});
test('legacy migration exposes only the known dated snapshot even after a week boundary',()=>{
  const data={meta:{generatedAt:'2026-11-10T10:00:00+08:00'},candidates:[first]};
  assert.equal(weeklyView(data,'2026-11-16').selected,'2026-11-10');
  assert.equal(weeklyView(data,'2026-11-16','2026-11-10').isCarried,true);
  assert.deepEqual(weeklyView(data,'2026-11-16','2026-11-09').candidates,[]);
});
test('long outages and malformed or future dates do not clear or invent saved slots',()=>{
  const data=fullWeek();
  data.pool.days.push({date:'2026-11-02',candidates:[newer]}, {date:'2026-12-28',candidates:[newer]}, {date:'invalid',candidates:[newer]});
  const before=JSON.stringify(data),view=weeklyView(data,'2026-12-07');
  assert.equal(view.days.length,5);
  assert.equal(view.slots[0].date,'2026-11-09');
  assert.equal(view.selected,'2026-11-13');
  assert.ok(view.slots.every(s=>s.updateDue&&s.isCarried));
  assert.equal(JSON.stringify(data),before);
});
