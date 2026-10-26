// Explicitly synthetic fixtures. Never used as a fallback for production data.
export const DEMO_NOW = '2026-10-23T14:30:00+08:00';
const calendar = [];
for (let t=Date.UTC(2026,8,1); t<=Date.UTC(2026,10,30); t+=86400000) {
  const d = new Date(t); if(d.getUTCDay()>0&&d.getUTCDay()<6) calendar.push(d.toISOString().slice(0,10));
}
const down = x => Math.floor((x+1e-8)*100)/100;
function candidate(code,name,sector,H,price,score,status,eventDate='2026-10-19') {
  return {symbol:`${code}.SH`,code,name,sector,segment:'SSE_MAIN',eventId:`DEMO:${code}:${eventDate}`,eventVersion:1,eventDate,eventDayIndex:4,limitUpPrice:H,referencePeak:H,price,pct:-1.25,quoteTimestamp:DEMO_NOW,drawdownPct:100*(H-price)/H,compositeScore:score,status,action:status==='BUY_READY'?'BUY_READY':'WATCH_NO_BROKER_ORDER',expiresOn:'2026-10-27',riskLevel:'NORMAL',isSyntheticExample:true,reasonCodes:['演示趋势结构完整','演示缩量承接，非真实行情'],buyLevels:[['A',.11],['B',.13]].map(([id,d])=>{const triggerPrice=down(H*(1-d)),tp1=down(triggerPrice+.382*(H-triggerPrice)),tp2=down(triggerPrice+.618*(H-triggerPrice));return {id,drawdownPct:d*100,triggerPrice,maxBuyPrice:down(Math.min(triggerPrice*1.015,H*(id==='A'?.905:.885))),tp1,tp2,tp1Pct:(tp1/triggerPrice-1)*100,tp2Pct:(tp2/triggerPrice-1)*100,plannedStop:down(triggerPrice*.96),rewardRisk:1.6,status}}),gates:{trend:true,event:true,structure:true,absorption:status==='BUY_READY',payoff:true},confirmedPivotLow:down(H*.858),metrics:{atr14:down(H*.02),support:down(H*.85)}};
}
export function demoData() {
  const candidates=[candidate('600001','示例·云川材料','先进材料',20,17.74,86,'BUY_READY'),candidate('600002','示例·远峰设备','高端制造',32.8,29.56,82,'WATCH'),candidate('600003','示例·森禾能源','绿色能源',12.6,11.09,79,'WATCH'),candidate('600004','示例·星原电子','电子元件',45.2,40.03,81,'WATCH')];
  return {meta:{schemaVersion:1,strategyId:'LBR_MAIN_V1',generatedAt:DEMO_NOW,quoteTimestamp:DEMO_NOW,tradingDate:'2026-10-23',mode:'SYNTHETIC_DEMO',stale:false,sourceHealth:[],errors:[]},config:{candidateSessions:7,holdingSessions:7,buyPortions:[.3,.3,.4]},calendar:{sessions:calendar,verified:true,source:'仅供演示的合成交易日历',coverageThrough:calendar.at(-1)},pool:{batchId:'DEMO-20261019',startedOn:'2026-10-19',sessionIndex:4,nextRefreshOn:'2026-10-28',refreshAfterSessions:7},candidates,stats:{},learning:{}};
}
export function demoBuys() {return [{id:'demo-buy-1',symbol:'600001',name:'示例·云川材料',eventId:'DEMO:600001:2026-10-19',price:17.52,qty:600,fees:5,executedAt:'2026-10-21T10:30:00+08:00',planFraction:.3,entry:{high:20,low:17.2,stop:16.98}}, {id:'demo-buy-2',symbol:'600005',name:'示例·南麓科技',eventId:'DEMO:600005:2026-10-15',price:24.8,qty:400,fees:5,executedAt:'2026-10-15T13:40:00+08:00',planFraction:.3,entry:{high:28.6,low:24.5,stop:23.6}}];}
export function demoQuotes(data) {return [...data.candidates.map(c=>({symbol:c.code,price:c.price,timestamp:DEMO_NOW,source:'synthetic-demo'})),{symbol:'600005',price:25.36,timestamp:DEMO_NOW,source:'synthetic-demo'}];}
