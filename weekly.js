// Calendar navigation is a view only; it never mutates the journal or a fill.
const addDays=(day,n)=>new Date(Date.parse(`${day}T00:00:00Z`)+n*86400000).toISOString().slice(0,10);
export function weekStart(day){const d=new Date(`${day}T00:00:00Z`);return addDays(day,-((d.getUTCDay()+6)%7));}
function weekdayOf(day){
  if(typeof day!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(day))return -1;
  const value=new Date(`${day}T00:00:00Z`);
  return Number.isFinite(value.getTime())&&value.toISOString().slice(0,10)===day?(value.getUTCDay()+6)%7:-1;
}
export function weeklyView(data,today,selectedDate=null){
  const monday=weekStart(today),pool=data?.pool;
  const stored=Array.isArray(pool?.days)?[...pool.days]:[];
  // A legacy file proves only its own generation date, not previous weekdays.
  const legacyDay=data?.meta?.generatedAt?.slice(0,10);
  if(pool?.mode!=='WEEKLY_DAILY'&&legacyDay&&(data?.candidates||[]).length)
    stored.push({date:legacyDay,candidates:data.candidates,migrated:true});
  const byWeekday=new Map();
  for(const day of stored){
    const weekday=weekdayOf(day.date),existing=byWeekday.get(weekday);
    if(weekday<0||weekday>4||day.date>today||!Array.isArray(day.candidates))continue;
    if(!existing||day.date>existing.date)byWeekday.set(weekday,day);
  }
  const slots=Array.from({length:5},(_,weekday)=>{
    // The most recent potential update date is never in the future. Missing
    // slots remain empty; an older dated slot survives until its own refresh.
    const thisWeek=addDays(monday,weekday),expectedDate=thisWeek<=today?thisWeek:addDays(thisWeek,-7);
    const day=byWeekday.get(weekday),date=day?.date||expectedDate;
    return {weekday,date,expectedDate,day,isCarried:Boolean(day&&day.date<monday),
      updateDue:!day||day.date<expectedDate};
  });
  const dates=slots.map(slot=>slot.date),days=slots.filter(slot=>slot.day).map(slot=>slot.day);
  const latestSaved=days.map(d=>d.date).sort().at(-1);
  const pinnedWeekday=weekdayOf(selectedDate);
  // An explicitly selected Tuesday follows Tuesday's replacement, even when
  // its calendar date changes. It must not jump to a different weekday slot.
  const selected=pinnedWeekday>=0&&pinnedWeekday<=4?dates[pinnedWeekday]:latestSaved||dates[Math.min(4,weekdayOf(today))];
  const selectedIndex=dates.indexOf(selected),selectedSlot=slots[selectedIndex],day=selectedSlot?.day;
  const current=new Map((data?.candidates||[]).map(c=>[c.eventId,c]));
  const candidates=(day?.candidates||[]).map(original=>({...(
    current.get(original.eventId)||{...original,stale:true,
      status:['CANCEL','EXPIRE'].includes(original.status)?original.status:'WATCH',
      action:['CANCEL','EXPIRE'].includes(original.status)?original.action:'WATCH_NO_BROKER_ORDER'}),
    recommendationDate:selected,recommendedPrice:original.price,recommendedPeak:original.referencePeak}));
  return {monday,friday:addDays(monday,4),nextMonday:addDays(monday,7),dates,days,slots,
    selected,selectedIndex,selectedSlot,isCarried:selectedSlot?.isCarried||false,
    lastUpdatedOn:latestSaved||null,day,candidates};
}
export function moveDate(view,step){return view.dates[Math.max(0,Math.min(4,view.dates.indexOf(view.selected)+step))];}
