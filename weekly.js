// Calendar navigation is a view only; it never mutates the journal or a fill.
const addDays=(day,n)=>new Date(Date.parse(`${day}T00:00:00Z`)+n*86400000).toISOString().slice(0,10);
export function weekStart(day){const d=new Date(`${day}T00:00:00Z`);return addDays(day,-((d.getUTCDay()+6)%7));}
export function weeklyView(data,today,selectedDate=null){
  const monday=weekStart(today),pool=data?.pool;
  let days=pool?.mode==='WEEKLY_DAILY'&&pool.weekStart===monday?pool.days||[]:[];
  // A legacy file proves only its own generation date, not previous weekdays.
  const legacyDay=data?.meta?.generatedAt?.slice(0,10);
  if(pool?.mode!=='WEEKLY_DAILY'&&legacyDay&&weekStart(legacyDay)===monday&&legacyDay<=today&&new Date(`${legacyDay}T00:00:00Z`).getUTCDay()%6!==0)
    days=[{date:legacyDay,candidates:data.candidates||[]}];
  days=days.filter(d=>d.date>=monday&&d.date<=addDays(monday,4)&&d.date<=today);
  const dates=Array.from({length:5},(_,i)=>addDays(monday,i));
  const selected=dates.includes(selectedDate)?selectedDate:days.map(d=>d.date).sort().at(-1)||dates[Math.min(4,Math.max(0,dates.filter(d=>d<=today).length-1))];
  const day=days.find(d=>d.date===selected),current=new Map((data?.candidates||[]).map(c=>[c.eventId,c]));
  const candidates=(day?.candidates||[]).map(original=>({...(
    current.get(original.eventId)||{...original,stale:true,status:'WATCH',action:'WATCH_NO_BROKER_ORDER'}),
    recommendationDate:selected,recommendedPrice:original.price,recommendedPeak:original.referencePeak}));
  return {monday,friday:dates[4],nextMonday:addDays(monday,7),dates,days,selected,day,candidates};
}
export function moveDate(view,step){return view.dates[Math.max(0,Math.min(4,view.dates.indexOf(view.selected)+step))];}
