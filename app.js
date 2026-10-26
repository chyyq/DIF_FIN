import {newState,recordBuy,recordSell,recordEvents,applyQuotes,holdings,learningSummary,mergeState,validateState,exportState,suggestedQuantity} from './portfolio.js';
import {demoData,demoBuys,demoQuotes,DEMO_NOW} from './demo.js';

const $=s=>document.querySelector(s);
const escapeHTML=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toFixed(2);
const integer=v=>v==null?'—':Number(v).toLocaleString('zh-CN');
const percent=v=>v==null||!Number.isFinite(Number(v))?'—':`${Number(v)>0?'+':''}${Number(v).toFixed(2)}%`;
const sign=v=>v>0?'positive':v<0?'negative':'';
const codeOf=v=>String(v??'').replace(/^(SH|SZ)/i,'').replace(/\.(SH|SZ)$/i,'');
const fullSymbol=c=>c.symbol||`${c.code}.${String(c.code).startsWith('6')?'SH':'SZ'}`;
const reasonNames={INSUFFICIENT_HISTORY:'历史行情不足',TREND_UNAVAILABLE:'趋势待核验',TREND_FAILED:'趋势未通过',QUOTE_NOT_FRESH_FOR_ENTRY:'闭市报价仅作参考',SECURITY_METADATA_UNVERIFIED:'证券状态待核验',ANNOUNCEMENTS_UNVERIFIED:'公告待核验',SECTOR_UNVERIFIED:'板块待核验',STRUCTURE_UNVERIFIED:'支撑结构待核验',ACCOUNT_STATE_UNSYNCED:'账户状态未同步',INTRADAY_CONFIRMATION_UNAVAILABLE:'缺盘中承接证据',PAYOFF_UNAVAILABLE_OR_INSUFFICIENT:'风险收益比待核验',UPTREND_VALID:'上升趋势有效',LIMIT_EVENT_CONFIRMED:'涨停事件确认'};
const reasonLabel=x=>reasonNames[x]||x;
const shanghaiNow=()=>new Date().toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}).replace(' ','T');
const dayOf=t=>new Date(Date.parse(t)+8*3600000).toISOString().slice(0,10);
const formatTime=t=>t?new Date(t).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';
const dateShort=t=>t?String(t).slice(5).replace('-',' / '):'待日历确认';
const names={workspace:'策略工作台',journal:'成交与样本',learning:'学习观察室',settings:'策略与设置'};
const MODEL_KEY='lbr-wave-ledger-v1';
const ctx={demo:false,data:null,ledger:null,rows:[],page:'workspace',refreshing:false,trade:null,lastAlerts:new Set(),settings:{equity:100000,budget:20000,fee:5},storageBlocked:false,generation:0};
function now(){return ctx.demo?DEMO_NOW:new Date().toISOString();}
function asOf(){return dayOf(now());}
function sessions(){return ctx.data?.calendar?.verified?ctx.data.calendar.sessions:[];}
function storageKey(){return ctx.demo?`${MODEL_KEY}-demo`:MODEL_KEY;}
function notice(message){$('#global-notice').textContent=message;$('#global-notice').hidden=!message;}
let toastTimer;
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,5000);}
function loadLedger(){
  ctx.storageBlocked=false;
  try{
    const raw=localStorage.getItem(storageKey());
    if(!raw)return newState({now:now()});
    const value=JSON.parse(raw),r=validateState(value);if(!r.valid)throw Error(r.errors.join('；'));
    return value;
  }catch(e){ctx.storageBlocked=true;notice(`本地账本暂时无法读取：${e.message}。原数据未覆盖，请先导出或恢复备份。`);return newState({now:now()});}
}
function persist(next,{silent=false}={}){
  const r=validateState(next);if(!r.valid)throw Error(r.errors.join('；'));
  if(ctx.storageBlocked)throw Error('账本读取异常，为保护原数据暂停写入，请先恢复有效备份。');
  try{localStorage.setItem(storageKey(),exportState(next));}
  catch(e){notice('浏览器存储已满或不可用，本次操作未保存。请先导出账本备份，再释放存储空间。');throw Error('本次操作未保存到本地，请导出备份后重试。');}
  ctx.ledger=next;if(!silent)render();
}
function empty(title,description,action=''){return `<div class="empty-state"><div class="empty-symbol" aria-hidden="true">⌁</div><h3>${escapeHTML(title)}</h3><p>${escapeHTML(description)}</p>${action}</div>`;}
async function fetchJSON(path){const r=await fetch(`${path}?t=${Date.now()}`,{cache:'no-store',signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error(`${path}: HTTP ${r.status}`);return r.json();}
function dataValid(data){return data?.meta?.strategyId==='LBR_MAIN_V1'&&Array.isArray(data.candidates)&&Array.isArray(data.calendar?.sessions);}
async function refresh({silent=false}={}){
  if(ctx.refreshing||ctx.demo)return;
  ctx.refreshing=true;$('#refresh-button').disabled=true;const old=ctx.data?.meta?.generatedAt,generation=ctx.generation;
  try{
    const [latest,quoteResult,eventResult]=await Promise.allSettled([fetchJSON('data/latest.json'),fetchJSON('data/quotes.json'),fetchJSON('data/events.json')]);
    if(generation!==ctx.generation||ctx.demo)return;
    if(latest.status!=='fulfilled')throw latest.reason;if(!dataValid(latest.value))throw Error('快照结构不受支持');
    ctx.data=latest.value;
    let next=ctx.ledger;
    if(eventResult.status==='fulfilled'&&eventResult.value.strategyId==='LBR_MAIN_V1'){
      // The server retains complete audit snapshots. Keep stable research facts
      // for rejected/unfilled events here, without duplicating every quote tick.
      const events=(eventResult.value.events||[]).map(c=>({eventId:c.eventId,symbol:c.symbol,snapshot:{eventId:c.eventId,symbol:c.symbol,eventDate:c.eventDate,eventVersion:c.eventVersion,referencePeak:c.referencePeak,status:c.status,reasonCodes:c.reasonCodes||[]}}));
      next=recordEvents(next,events,{now:now()});
    }
    next=recordEvents(next,ctx.data.candidates.map(c=>({...c,eventId:c.eventId||`${c.symbol}:${c.eventDate}`,snapshot:c})),{now:now()});
    if(quoteResult.status==='fulfilled'){
      const quoteFile=quoteResult.value;
      const needed=new Set([...held().map(h=>h.symbol),...ctx.data.candidates.map(c=>codeOf(c.symbol||c.code))]);
      const quotes=Object.values(quoteFile.quotes||{}).filter(q=>needed.has(codeOf(q.symbol||q.code))&&q.price>0&&q.quoteTimestamp).map(q=>({symbol:q.symbol||q.code,price:q.price,timestamp:q.quoteTimestamp,source:q.source||quoteFile.meta?.source||'snapshot'}));
      next=applyQuotes(next,quotes,{now:now()});
    }
    if(!ctx.storageBlocked)persist(next,{silent:true});
    if(ctx.data.meta.stale)notice('数据管道当前标记为过期或不完整。参考价只用于观察，请检查来源时间和策略详情。');
    else if(!ctx.storageBlocked)notice(ctx.data.meta.errors?.length?'部分数据源不可用，当前使用备用报价；历史趋势、承接或结构资料未通过核验时，只显示观察参考价。':'');
    await refreshLiveQuotes();if(generation!==ctx.generation||ctx.demo)return;render();
    if(!silent)toast(old&&old===ctx.data.meta.generatedAt?'已检查：服务器快照未更新；已另外尝试读取个股行情。':'已读取最新数据；本地成交记录已保留。');
  }catch(e){if(generation===ctx.generation&&!ctx.demo){notice(`读取行情失败：${e.message}。保留已有行情和账本，旧数据不会当作实时。`);render();}}
  finally{if(generation===ctx.generation){ctx.refreshing=false;$('#refresh-button').disabled=ctx.demo;}}
}
function jsonpQuote(c){
  return new Promise((resolve,reject)=>{
    const cb=`lbr_${crypto.randomUUID().replaceAll('-','')}`,script=document.createElement('script');let timer;
    const cleanup=()=>{clearTimeout(timer);delete window[cb];script.remove();};
    window[cb]=payload=>{cleanup();const q=payload?.data;const raw=q?.f43,seconds=q?.f124;
      if(!q||typeof raw!=='number'||raw<=0||!Number.isFinite(seconds))return reject(Error('个股报价缺价或来源时间'));
      const digits=Number(q.f59);if(!Number.isInteger(digits)||digits<0||digits>5)return reject(Error('报价精度未知'));
      resolve({symbol:c,price:raw/(10**digits),timestamp:new Date(seconds*1000).toISOString(),source:'eastmoney-browser'});
    };
    script.onerror=()=>{cleanup();reject(Error('实时行情接口不可用'));};
    timer=setTimeout(()=>{cleanup();reject(Error('实时行情超时'));},9000);
    const secid=`${String(c).startsWith('6')?'1':'0'}.${c}`;
    script.src=`https://push2.eastmoney.com/api/qt/stock/get?secid=${encodeURIComponent(secid)}&fields=f43,f57,f58,f59,f124&cb=${cb}`;
    document.head.append(script);
  });
}
async function refreshLiveQuotes(){
  if(ctx.demo||document.hidden)return;
  const generation=ctx.generation;
  const codes=[...new Set([...held().map(h=>h.symbol),...(ctx.data?.candidates||[]).map(c=>c.code||codeOf(c.symbol))])];
  if(!codes.length)return;
  const results=[];
  // Small bounded batches avoid flooding the public endpoint; no arbitrary holding cutoff.
  for(let i=0;i<codes.length;i+=6)results.push(...await Promise.allSettled(codes.slice(i,i+6).map(jsonpQuote)));
  if(generation!==ctx.generation||ctx.demo)return;
  const quotes=results.filter(r=>r.status==='fulfilled').map(r=>r.value);
  if(quotes.length&&!ctx.storageBlocked){try{persist(applyQuotes(ctx.ledger,quotes,{now:now()}),{silent:true});}catch(e){notice(e.message);}}
  const failed=results.filter(r=>r.status==='rejected').length;
  $('#refresh-status').textContent=failed?`${quotes.length}只个股行情可用 · ${failed}只读取失败`:'每60秒检查个股行情 · 来源时间可查';
}
function held(){const calendar=sessions(),last=calendar.filter(d=>d<=asOf()).at(-1);return holdings(ctx.ledger,{sessions:calendar,asOf:last||asOf(),now:now()}).map(h=>({...h,totalPnl:h.pnl,pnl:h.unrealizedPnl,pnlPct:h.unrealizedPnl!=null&&h.costCents?100*h.unrealizedPnl/(h.costCents/100):null}));}
function quoteFor(c){return ctx.ledger.quotes[codeOf(c.symbol||c.code)];}
function quotePrice(c){const q=quoteFor(c);return q?.priceCents!=null?q.priceCents/100:c.price;}
function getAlerts(rows){
  const list=[];
  for(const h of rows){
    if(h.due)list.push({symbol:h.symbol,name:h.name,title:`第${h.heldSessions}个交易日 · 请处理`,text:'七日跟踪到期，持仓继续保留，等待实际卖出确认。',type:'warning'});
    if(h.calendarUnknown)list.push({symbol:h.symbol,name:h.name,title:'交易日历待确认',text:'无法可靠计算期限与可卖数量，请先补齐交易日历。',type:'warning'});
    if(h.targets.some(t=>t.stopReached))list.push({symbol:h.symbol,name:h.name,title:'触及止损参考',text:h.sellableQty?'请核对可卖数量并手动处理。':'当前可能有T+1限制，请核对可卖股数。',type:'danger'});
    else if(h.targets.some(t=>t.tp2Reached&&!t.requiresExitReview))list.push({symbol:h.symbol,name:h.name,title:'触及第二止盈',text:'已观测到目标触价；未自动记作卖出。',type:'warning'});
    else if(h.targets.some(t=>t.tp1Reached&&!t.requiresExitReview))list.push({symbol:h.symbol,name:h.name,title:'触及第一止盈',text:'可按计划处理50%；实际卖出请记账。',type:'warning'});
    if(h.targets.some(t=>t.requiresExitReview))list.push({symbol:h.symbol,name:h.name,title:'风险计划需要复核',text:'入场锚点、止损资料不足，或第一目标不高于成本；请按实际成交核对计划。',type:'warning'});
    if(h.marketPrice==null||h.pnl==null)list.push({symbol:h.symbol,name:h.name,title:'行情过期或缺失',text:'浮动盈亏与当前触价提示暂停，保留已有记录。',type:'warning'});
  }return list;
}
function render(){
  if(!ctx.ledger)return;
  ctx.rows=held();const summary=learningSummary(ctx.ledger),alerts=getAlerts(ctx.rows),data=ctx.data,candidates=data?.candidates||[];
  $('#today-label').textContent=asOf().replaceAll('-',' / ');$('#demo-banner').hidden=!ctx.demo;$('#demo-toggle').textContent=ctx.demo?'退出演示':'体验演示';$('#refresh-button').disabled=ctx.demo;
  $('#pool-count').innerHTML=`${candidates.filter(c=>!['CANCEL','EXPIRE'].includes(c.status)).length}<small>/ 10</small>`;$('#pool-badge').textContent=candidates.length;$('#pool-description').textContent=data?.pool?.batchId?`批次 ${data.pool.batchId}`:'等待可核验涨停事件';
  $('#position-count').innerHTML=`${ctx.rows.length}<small>只</small>`;$('#holdings-badge').textContent=ctx.rows.length;
  const known=ctx.rows.filter(h=>h.pnl!=null);const pnl=known.reduce((s,h)=>s+h.pnl,0);
  $('#floating-pnl').textContent=known.length?`${pnl>=0?'+':''}${money(pnl)}`:'—';$('#floating-pnl').className=sign(pnl);
  $('#pnl-description').textContent=known.length!==ctx.rows.length?'部分报价缺失 · 当前只统计有有效报价的持仓':'按有效行情估算 · 含已录买入费';
  $('#alert-count').innerHTML=`${alerts.length}<small>项</small>`;$('#alert-description').textContent=alerts.length?'请查看监控提醒，成交后更新账本':'止盈、止损与第七日检查';$('#alerts-total').textContent=`${alerts.length} 项`;
  $('#data-status').textContent=ctx.demo?'演示行情':data?.meta?.stale?'数据待核验':data?.meta?.quoteTimestamp&&dayOf(data.meta.quoteTimestamp)<asOf()?'闭市 / 历史快照':data?.meta?.generatedAt?'行情快照':'等待数据';
  $('#data-time').textContent=data?.meta?.quoteTimestamp?`来源 ${formatTime(data.meta.quoteTimestamp)}`:'尚未取得可靠来源时间';$('#data-dot').style.background=data?.meta?.stale?'#cba267':'#75a480';
  const p=data?.pool;$('#cycle-range').textContent=p?.startedOn?`${dateShort(p.startedOn)} — ${p.nextRefreshOn?dateShort(p.nextRefreshOn):'后续日历待确认'}`:'等待交易日历';$('#cycle-dots').innerHTML=Array.from({length:7},(_,i)=>`<i class="cycle-dot ${i<(p?.sessionIndex??-1)?'past':i===p?.sessionIndex?'current':''}" title="第${i+1}个交易日"></i>`).join('');
  renderCandidates();renderPositions();renderJournal();renderLearning(summary);
  $('#alerts-list').innerHTML=alerts.length?alerts.slice(0,8).map(a=>`<article class="alert-item ${a.type}"><b>${escapeHTML(a.name)} · ${escapeHTML(a.title)}</b><p>${escapeHTML(a.text)}</p></article>`).join(''):'<p class="aside-note">暂无待处理提醒。买入记录会在这里持续跟踪。</p>';
  $('#sample-count').textContent=`${summary.closedEventCount} 个已完成事件`;$('#sample-progress-bar').style.width=`${Math.min(100,summary.closedEventCount/30*100)}%`;$('#journal-count').textContent=summary.fillCount;
  for(const a of alerts){const key=`${asOf()}:${a.symbol}:${a.title}`;if(!ctx.demo&&!ctx.lastAlerts.has(key)&&'Notification'in window&&Notification.permission==='granted'){new Notification(`LBR · ${a.name}`,{body:`${a.title}。${a.text}`,tag:key});ctx.lastAlerts.add(key);}}
}
function renderCandidates(){
  const search=$('#stock-search').value.trim().toLowerCase(),filter=$('#pool-filter').value;
  const list=(ctx.data?.candidates||[]).filter(c=>`${c.name} ${c.code} ${c.symbol}`.toLowerCase().includes(search)).filter(c=>filter==='all'||(filter==='ready'?c.status==='BUY_READY':c.status!=='BUY_READY'));
  if(!list.length){$('#candidate-list').innerHTML=empty(search||filter!=='all'?'没有匹配的备选':'等待符合条件的涨停事件',search?'换一个名称或代码试试。':'数据不完整或暂无合格候选时保持空池。可以先体验演示，或运行数据脚本获取市场快照。',!ctx.demo?'<button class="text-button" data-action="demo">体验完整演示 ↗</button>':'');return;}
  $('#candidate-list').innerHTML=list.map(c=>{
    const price=quotePrice(c),A=c.buyLevels?.[0]||{},B=c.buyLevels?.[1]||{},liveQ=quoteFor(c),expired=['CANCEL','EXPIRE'].includes(c.status),inHolding=ctx.rows.some(h=>h.symbol===codeOf(c.symbol));
    const status=expired?'已失效':inHolding?'已持仓 · 继续跟踪':c.status==='BUY_READY'?'确认就绪':'等待承接确认';
    const dd=c.referencePeak&&price!=null?(1-price/c.referencePeak)*100:null;
    return `<article class="candidate-card"><div class="stock-row"><div class="stock-identity"><div class="stock-mark">${escapeHTML(c.name?.replace(/^示例·/,'').slice(0,1))}</div><div><span class="stock-name">${escapeHTML(c.name)}</span><span class="stock-code">${escapeHTML(c.code||c.symbol)}</span><div class="stock-meta">${escapeHTML((c.sector&&c.sector!=='UNKNOWN')?c.sector:'板块待核验')} · 涨停 ${dateShort(c.eventDate)} · 基数 ¥${money(c.referencePeak)}</div></div></div><div class="stock-price"><b>${money(price)}</b><small class="${sign(c.pct)}">回撤 ${dd==null?'—':`${dd.toFixed(2)}%`}</small></div></div><div class="stock-status"><span class="status-pill ${expired?'danger':c.status==='BUY_READY'?'ready':''}">${status}</span><span class="subtle">${ctx.demo?'演示数据':`来源 ${liveQ?formatTime(liveQ.timestamp):formatTime(c.quoteTimestamp)}`}</span></div><div class="price-grid"><div class="price-cell"><label>A档低吸 <span class="buy-rate">−11%</span></label><b>¥${money(A.triggerPrice)}</b><em>上限 ${money(A.maxBuyPrice)}</em></div><div class="price-cell"><label>B档低吸 <span class="buy-rate">−13%</span></label><b>¥${money(B.triggerPrice)}</b><em>上限 ${money(B.maxBuyPrice)}</em></div><div class="price-cell"><label>A档预估TP1 <span class="sell-rate">50%</span></label><b>¥${money(A.tp1)}</b><em>${percent(100*(A.tp1/A.maxBuyPrice-1))} / A上限价</em></div><div class="price-cell"><label>A档预估TP2 <span class="sell-rate">50%</span></label><b>¥${money(A.tp2)}</b><em>${percent(100*(A.tp2/A.maxBuyPrice-1))} / A上限价</em></div></div><div class="candidate-bottom"><span class="candidate-reason">${escapeHTML((c.reasonCodes||[]).slice(0,2).map(reasonLabel).join(' · ')||'等待更多证据，价格不等于买入信号')}</span><div class="buy-buttons"><button class="text-button" data-action="detail" data-code="${escapeHTML(c.code)}">详情 ↗</button>${[.3,.3,.4].map((f,i)=>`<button class="mini-button" data-action="buy" data-code="${escapeHTML(c.code)}" data-fraction="${f}" data-tranche="${i+1}" title="按计划资金${f*100}%计算参考股数，再确认实际成交">记 ${f*100}%</button>`).join('')}</div></div></article>`;
  }).join('');
}
function targetLabel(h,key){const vals=[...new Set(h.targets.map(t=>t[key]).filter(v=>v!=null))];return vals.length?vals.map(v=>money(v)).join(' / '):'待补齐';}
function renderPositions(){
  $('#position-list').innerHTML=ctx.rows.length?ctx.rows.map(h=>`<article class="position-card"><div class="stock-row"><div class="stock-identity"><div class="stock-mark">${escapeHTML(h.name?.replace(/^示例·/,'').slice(0,1)||'持')}</div><div><span class="stock-name">${escapeHTML(h.name)}</span><span class="stock-code">${escapeHTML(h.symbol)}</span><div class="stock-meta">${dateShort(h.firstBuyDate)} 首次买入 · ${integer(h.qty)}股 · 可卖 ${integer(h.sellableQty)}股</div></div></div><div class="stock-price"><b class="${sign(h.pnl)}">${h.pnl==null?'—':`${h.pnl>=0?'+':''}${money(h.pnl)}`}</b><small class="${sign(h.pnlPct)}">${percent(h.pnlPct)}</small></div></div><div class="position-overview"><div><label>加权成本 / 现价</label><b>${money(h.avgCost)} / ${money(h.marketPrice)}</b></div><div><label>观察高点 / 低点</label><b>${money(h.high)} / ${money(h.low)}</b></div><div><label>TP1 / TP2（分批）</label><b>${targetLabel(h,'tp1')} / ${targetLabel(h,'tp2')}</b></div><div><label>止损参考</label><b>${targetLabel(h,'stop')}</b></div></div><div class="holding-progress"><span class="status-pill ${h.due?'warning':''}">${h.calendarUnknown?'期限待确认':`第${h.heldSessions} / 7交易日`}</span><div class="bar"><i style="width:${Math.min(100,(h.heldSessions||0)/7*100)}%"></i></div><span>${h.expiryDate?`${dateShort(h.expiryDate)} 提醒`:'期限待后续交易日历'}</span><div class="position-actions"><button class="mini-button" data-action="add" data-code="${escapeHTML(h.symbol)}">＋ 加仓</button><button class="mini-button sell" data-action="sell" data-code="${escapeHTML(h.symbol)}" data-fraction="0.5">记卖出50%</button><button class="mini-button sell" data-action="sell" data-code="${escapeHTML(h.symbol)}" data-fraction="1">记卖出</button></div></div><div class="coverage">${h.coverageGap?'观察覆盖不完整 · 峰谷不是完整行情路径':'峰谷仅使用买入后收到的有效报价'} · ${h.due?'到期保留持仓，等待你处理':'加仓不会重置七日计数'} · <button class="text-button" data-action="holding-detail" data-code="${escapeHTML(h.symbol)}">分笔目标 / 明细 ↗</button></div></article>`).join(''):empty('还没有正在监控的持仓','在备选池点击计划比例，或补录实际成交。买入后自动合并同股，并开始独立七日跟踪。');
}
function renderJournal(){
  const fills=[...ctx.ledger.fills].sort((a,b)=>b.executedAt.localeCompare(a.executedAt));
  $('#journal-content').innerHTML=fills.length?`<div class="table-scroll"><table class="trade-table"><thead><tr><th>成交时间（上海）</th><th>股票</th><th>方向</th><th>实际价格</th><th>股数</th><th>费用</th><th>事件 / 原因</th></tr></thead><tbody>${fills.map(f=>`<tr><td class="num">${escapeHTML(formatTime(f.executedAt))}</td><td>${escapeHTML(f.name)}<br><span class="num">${escapeHTML(f.symbol)}</span></td><td><span class="status-pill ${f.side==='BUY'?'ready':'warning'}">${f.side==='BUY'?'买入':'卖出'}</span></td><td class="num">${money(f.priceCents/100)}</td><td class="num">${integer(f.qty)}</td><td class="num">${money(f.feesCents/100)}</td><td>${escapeHTML(f.reason||f.eventId)}${f.note?`<br><span class="muted">备注：${escapeHTML(f.note)}</span>`:""}</td></tr>`).join('')}</tbody></table></div>`:empty('从第一笔实际成交开始','确认买入、加仓和部分卖出都会在此保留；不会把触价提醒记录成成交。');
}
function renderLearning(s){
  $('#learning-content').innerHTML=`<div class="learning-stages">${[['01 · COLLECT','不足30个事件','检查数据、成交语义和缺失证据，不推断稳定胜率。'],['02 · DESCRIBE','30—99个事件','仅展示已记录结果，按回撤档位和市场日期研究差异。'],['03 · VALIDATE','100个事件起','按时间前推，隔离相邻事件，另做样本外检验。']].map((v,i)=>`<article class="learning-stage ${i===(s.closedEventCount<30?0:s.closedEventCount<100?1:2)?'current':''}"><small>${v[0]}</small><h3>${v[1]}</h3><p>${v[2]}</p></article>`).join('')}</div><section class="panel"><div class="panel-header"><h2>样本积累</h2><span class="tiny-tag">${ctx.demo?'DEMO ONLY':'ACTUAL FILLS'}</span></div><div class="learning-metrics"><div><label>独立记录事件</label><b>${s.eventCount}</b></div><div><label>已完成 / 未成交</label><b>${s.closedEventCount} <small>/ ${s.unfilledEventCount}</small></b></div><div><label>已平仓事件净盈亏</label><b class="${sign(s.netRealized)}">${money(s.netRealized)}</b></div><div><label>描述性胜率</label><b>${s.winRatePct==null?'—':`${s.winRatePct.toFixed(1)}%`}</b></div></div><div class="learning-explanation">${escapeHTML(s.message)}<br>每笔买入保留事件快照和冻结参数。未平仓、未成交样本独立保留。导出账本后，离线学习脚本可以读取全部事件和真实成交；当前版本不会自动修改推荐门槛。</div></section><section class="panel"><div class="panel-header"><h2>事件样本状态</h2></div>${s.samples.length?`<div class="table-scroll"><table class="trade-table"><thead><tr><th>事件</th><th>代码</th><th>状态</th><th>已实现净收益</th></tr></thead><tbody>${s.samples.map(x=>`<tr><td>${escapeHTML(x.eventId||x.id)}</td><td class="num">${escapeHTML(x.symbol)}</td><td>${escapeHTML(x.status)}</td><td class="num">${x.status==='CLOSED'?money(x.netPnlCents/100):'待完成'}</td></tr>`).join('')}</tbody></table></div>`:empty('尚无研究样本','真实候选和实际成交会逐步累积；演示数据保持隔离。')}</section>`;
}
function showPage(page){if(!names[page])return;ctx.page=page;for(const p of Object.keys(names))$(`#${p}-page`).hidden=p!==page;document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.page===page));$('#breadcrumb-name').textContent=names[page];history.replaceState(null,'',`#${page}`);window.scrollTo({top:0,behavior:'instant'});}
function candidateFor(code){return ctx.data?.candidates.find(c=>codeOf(c.code||c.symbol)===codeOf(code));}
function rowFor(code){return ctx.rows.find(h=>h.symbol===codeOf(code));}
function setupTrade(){
  const anchors=document.createElement('details');anchors.id='entry-plan';anchors.className='details-reasons';anchors.innerHTML='<summary>入场锚点与止损计划（用于冻结实盘目标）</summary><p class="muted">填写成交当时已知的确认低点和计划止损。缺失时仍可记账，但不把预估卖价冒充实盘冻结目标。</p><div class="form-grid"><label>事件高点 H（元）<input id="entry-high" type="number" min="0.01" step="0.01"></label><label>入场确认低点 L（元）<input id="entry-low" type="number" min="0.01" step="0.01"></label><label>计划止损参考（元）<input id="entry-stop" type="number" min="0.01" step="0.01"></label></div>';
  $('#trade-estimate').before(anchors);
}
function openTrade(type,code=null,fraction=.3,tranche=null){
  const c=candidateFor(code),h=rowFor(code),isSell=type==='sell';ctx.trade={type,code,c,h,fraction,tranche,id:crypto.randomUUID()};$('#trade-form').reset();$('#trade-error').textContent='';
  $('#trade-title').textContent=isSell?'记录实际卖出':h?'记录实际加仓':'记录实际买入';$('#trade-kicker').textContent=ctx.demo?'SYNTHETIC DEMO FILL':'MANUAL EXECUTION';
  $('#trade-symbol').value=codeOf(code||'');$('#trade-name').value=c?.name||h?.name||'';$('#trade-symbol').readOnly=Boolean(code);$('#trade-name').readOnly=Boolean(code);$('#sell-reason-field').hidden=!isSell;$('#entry-plan').hidden=isSell;$('#trade-time').step='1';
  const price=c?quotePrice(c):h?.marketPrice;$('#trade-price').value=price?money(price):'';$('#trade-fee').value=ctx.settings.fee;$('#trade-time').value=ctx.demo?DEMO_NOW.slice(0,19):shanghaiNow().slice(0,19);
  if(isSell){const available=h.sellableQty??h.qty;$('#trade-quantity').value=fraction===.5?Math.max(1,Math.floor(available/2/100)*100):available;$('#trade-context').textContent=`${h.name} · 共${h.qty}股，可卖${h.sellableQty??'待日历确认'}股。按实际已成交数量填写。`;$('#trade-reason').value=fraction===.5?'TP1':h.due?'TIME':'MANUAL';}
  else{const suggest=price?suggestedQuantity({budget:ctx.settings.budget,fraction,price}):null;$('#trade-quantity').value=suggest?.qty||100;$('#trade-context').textContent=`${tranche?`第${tranche}次计划份额 · `:''}计划资金${money(ctx.settings.budget)}元的${fraction*100}% ≈ ${money(ctx.settings.budget*fraction)}元。参考股数按100股向下取整，请改成实际成交值。`;$('#entry-high').value=c?.referencePeak||'';$('#entry-low').value=c?.confirmedPivotLow||'';$('#entry-stop').value=ctx.demo&&c?c.buyLevels?.[0]?.plannedStop||'':'';}
  $('#trade-warning').textContent=isSell?'仅记录已经发生的卖出。触及目标但未成交时，请继续保留持仓。':'这是成交记账，不会发送券商委托。观察状态、过期报价仍可补录真实成交；缺入场锚点将提示资料待补。';
  updateEstimate();$('#trade-dialog').showModal();
}
function updateEstimate(){const p=Number($('#trade-price').value),q=Number($('#trade-quantity').value),fee=Number($('#trade-fee').value);$('#trade-estimate').textContent=p>0&&q>0?`${ctx.trade?.type==='sell'?'预计净收款':'成交金额含费'} ¥${money(p*q+(ctx.trade?.type==='sell'?-fee:fee))} · ${integer(q)}股 × ¥${money(p)}`:'';}
function saveTrade(e){
  e.preventDefault();try{
    const f=ctx.trade,timeValue=$('#trade-time').value,executedAt=`${timeValue.length===16?`${timeValue}:00`:timeValue}+08:00`;if(Date.parse(executedAt)>Date.parse(now()))throw Error('实际成交时间不能在未来。');
    const symbol=$('#trade-symbol').value,name=$('#trade-name').value.trim(),price=Number($('#trade-price').value),qty=Number($('#trade-quantity').value),fees=Number($('#trade-fee').value);let next;
    if(f.type==='sell')next=recordSell(ctx.ledger,{id:f.id,symbol,name,price,qty,fees,executedAt,reason:$('#trade-reason').value,note:$('#trade-note').value},{sessions:sessions()});
    else{
      const c=f.c,activeHolding=rowFor(symbol),eventId=c?.eventId||(activeHolding?ctx.ledger.fills.filter(x=>x.symbol===symbol&&x.side==='BUY').at(-1)?.eventId:null)||`${ctx.demo?'DEMO:':'MANUAL:'}${symbol}:${dayOf(executedAt)}:${f.id}`;
      const entry={knownAt:executedAt};for(const [key,id]of[['high','entry-high'],['low','entry-low'],['stop','entry-stop']]){const v=Number($(`#${id}`).value);if(v>0)entry[key]=v;}
      if(entry.high&&entry.low&&entry.high<entry.low)throw Error('确认低点不能高于事件高点。');if(entry.stop&&entry.stop>=price)throw Error('计划止损参考应低于实际买入价，请核对输入。');
      if(c&&[c.quoteTimestamp,ctx.data?.meta?.generatedAt].filter(Boolean).some(t=>Date.parse(t)>Date.parse(executedAt)))throw Error('当前候选快照晚于你填写的历史成交时间。请使用补录成交并提供当时已知锚点，避免未来数据进入样本。');
      next=recordBuy(ctx.ledger,{id:f.id,symbol,name,eventId,eventSnapshot:c?{...c,generatedAt:ctx.data.meta.generatedAt}:{manualFill:true,symbol,recordedAt:executedAt},price,qty,fees,executedAt,planFraction:f.fraction,entry,note:$('#trade-note').value});
    }
    persist(next);$('#trade-dialog').close();toast('实际成交已保存到本地账本。');
  }catch(error){$('#trade-error').textContent=error.message;}
}
function showDetail(code,holding=false){
  const c=candidateFor(code),h=rowFor(code);$('#detail-title').textContent=`${c?.name||h?.name||code} · ${holding?'分笔监控':'事件计划'}`;
  if(holding&&h){$('#detail-content').innerHTML=`<p class="details-reasons">同股合并展示，${h.targets.length}个买入批次分别冻结目标。目标触及与实际成交分开记录。</p><div class="table-scroll"><table class="trade-table"><thead><tr><th>剩余股数</th><th>冻结TP1</th><th>冻结TP2</th><th>计划止损</th><th>锚点高 / 低</th></tr></thead><tbody>${h.targets.map(t=>`<tr><td>${integer(t.qty)}</td><td>${money(t.tp1)}</td><td>${money(t.tp2)}</td><td>${money(t.stop)}</td><td>${money(t.high)} / ${money(t.low)}</td></tr>`).join('')}</tbody></table></div><p class="details-reasons">TP1 / TP2 的比例由入场确认低点与事件高点按0.382 / 0.618修复网格计算。已观测峰谷：${money(h.high)} / ${money(h.low)}，仅覆盖收到的买后行情。</p>`;}
  else if(c){$('#detail-content').innerHTML=`<div class="detail-grid">${[['涨停日期',c.eventDate],['涨停价',money(c.limitUpPrice)],['事件高点基数',money(c.referencePeak)],['当前回撤',`${money(c.drawdownPct)}%`],['事件版本',c.eventVersion],['有效至',c.expiresOn||'待后续交易日历']].map(([k,v])=>`<div>${k}<b>${escapeHTML(v)}</b></div>`).join('')}</div><div class="table-scroll"><table class="trade-table"><thead><tr><th>档位</th><th>触发 / 上限</th><th>预估TP1 / TP2</th><th>预估收益率</th><th>止损 / RR</th></tr></thead><tbody>${(c.buyLevels||[]).map(l=>`<tr><td>${escapeHTML(l.id)}</td><td>${money(l.triggerPrice)} / ${money(l.maxBuyPrice)}</td><td>${money(l.tp1)} / ${money(l.tp2)}</td><td>${percent(100*(l.tp1/l.maxBuyPrice-1))} / ${percent(100*(l.tp2/l.maxBuyPrice-1))}</td><td>${money(l.plannedStop)} / ${money(l.rewardRisk)}</td></tr>`).join('')}</tbody></table></div><p class="details-reasons">${escapeHTML((c.reasonCodes||[]).map(reasonLabel).join(' · '))}<br>预估收益率以各档最高可接受买价为基数，实际买入后以成交时已知的确认低点与高点重算。14%—16%为高风险观察区，深度回撤不能替代承接确认。</p><p class="details-reasons">Gate：${escapeHTML(JSON.stringify(c.gates||{}))}<br>来源：${escapeHTML(formatTime(c.quoteTimestamp))} · ${escapeHTML(c.action||c.status)}</p>`;}
  if(holding&&h){
    const gains=h.targets.map(t=>{const fill=ctx.ledger.fills.find(f=>f.id===t.buyFillId),price=fill?.priceCents/100;return `<tr><td>${integer(t.qty)}</td><td>${money(price)}</td><td>${t.tp1!=null?percent(100*(t.tp1/price-1)):'待补齐'}</td><td>${t.tp2!=null?percent(100*(t.tp2/price-1)):'待补齐'}</td><td>${t.stop!=null?percent(100*(t.stop/price-1)):'待补齐'}</td></tr>`;}).join('');
    $('#detail-content').insertAdjacentHTML('beforeend',`<div class="table-scroll"><table class="trade-table"><thead><tr><th>剩余股数</th><th>原实际买价</th><th>TP1比率</th><th>TP2比率</th><th>止损比率</th></tr></thead><tbody>${gains}</tbody></table></div><p class="details-reasons">目标比率相对各笔原实际成交价。当前持仓周期已实现净盈亏：¥${money(h.netRealized)}；余仓浮盈：¥${money(h.unrealizedPnl)}。费用按实际成交分配。</p>`);
  }
  $('#detail-dialog').showModal();
}
function download(){const blob=new Blob([exportState(ctx.ledger)],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`LBR-${ctx.demo?'DEMO-':''}ledger-${asOf()}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出全部事件、成交和观察记录。');}
async function importFile(file){
  if(!file)return;try{if(file.size>12*1024*1024)throw Error('文件超过12MB，请先检查是否为LBR账本。');const incoming=JSON.parse(await file.text());const r=validateState(incoming);if(!r.valid)throw Error(r.errors.join('；'));const isDemo=e=>e.id.startsWith('DEMO:')||e.snapshots.some(s=>s.data.synthetic===true||s.data.isSyntheticExample===true);if(incoming.events.some(e=>isDemo(e)!==ctx.demo))throw Error('演示账本与正式账本不能混合导入。');const next=mergeState(ctx.ledger,incoming);persist(next);toast('已校验并合并账本，同一成交ID不会重复记账。');}catch(e){toast(`导入失败：${e.message}`);}finally{$('#import-file').value='';}
}
async function toggleDemo(){
  ctx.generation++;ctx.refreshing=false;ctx.demo=!ctx.demo;ctx.lastAlerts.clear();notice('');ctx.ledger=loadLedger();
  if(ctx.demo){ctx.data=demoData();if(!ctx.ledger.fills.length){let next=recordEvents(ctx.ledger,ctx.data.candidates.map(c=>({...c,snapshot:c})),{now:DEMO_NOW});for(const buy of demoBuys())next=recordBuy(next,{...buy,eventSnapshot:{synthetic:true,...buy}});next=applyQuotes(next,demoQuotes(ctx.data),{now:DEMO_NOW});persist(next,{silent:true});}render();}
  else{ctx.data=null;render();await refresh({silent:true});}
}
document.addEventListener('click',e=>{
  const page=e.target.closest('[data-page]');if(page){showPage(page.dataset.page);return;}
  const close=e.target.closest('[data-close]');if(close){close.closest('dialog').close();return;}
  const b=e.target.closest('[data-action]');if(!b)return;
  const {action,code,fraction,tranche}=b.dataset;
  if(action==='demo')toggleDemo();else if(action==='buy')openTrade('buy',code,Number(fraction),Number(tranche));else if(action==='add')openTrade('buy',code,.3);else if(action==='sell')openTrade('sell',code,Number(fraction));else if(action==='detail')showDetail(code);else if(action==='holding-detail')showDetail(code,true);
});
$('#demo-toggle').addEventListener('click',toggleDemo);$('#exit-demo').addEventListener('click',toggleDemo);$('#refresh-button').addEventListener('click',()=>refresh());$('#manual-buy').addEventListener('click',()=>openTrade('buy'));$('#stock-search').addEventListener('input',renderCandidates);$('#pool-filter').addEventListener('change',renderCandidates);$('#trade-form').addEventListener('submit',saveTrade);for(const id of['trade-price','trade-quantity','trade-fee'])$(`#${id}`).addEventListener('input',updateEstimate);for(const id of['export-button','journal-export'])$(`#${id}`).addEventListener('click',download);$('#import-button').addEventListener('click',()=>$('#import-file').click());$('#import-file').addEventListener('change',e=>importFile(e.target.files[0]));$('#notify-button').addEventListener('click',async()=>{if(!('Notification'in window))return toast('此浏览器不支持系统提醒，页面内仍会显示。');try{const permission=await Notification.requestPermission();toast(permission==='granted'?'已开启：页面打开时按有效报价检查并提醒。':'系统提醒未开启，页面内提醒仍可查看。');}catch(e){toast(`提醒不可用：${e.message}`);}});
$('#settings-form').addEventListener('submit',e=>{e.preventDefault();const next={equity:Number($('#account-equity').value),budget:Number($('#symbol-budget').value),fee:Number($('#default-fee').value)};if(next.budget>next.equity)return toast('每只股票计划资金不能超过账户研究资金。');try{localStorage.setItem('lbr-wave-settings-v1',JSON.stringify(next));ctx.settings=next;toast('账户计划已保存。');}catch{toast('浏览器存储不可用，设置未保存。');}});
for(const d of document.querySelectorAll('dialog'))d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}});
try{const s=JSON.parse(localStorage.getItem('lbr-wave-settings-v1')||'null');if(s&&s.equity>=1000&&s.budget>0&&s.budget<=s.equity&&s.fee>=0)ctx.settings=s;}catch{}
$('#account-equity').value=ctx.settings.equity;$('#symbol-budget').value=ctx.settings.budget;$('#default-fee').value=ctx.settings.fee;
setupTrade();ctx.ledger=loadLedger();showPage(location.hash.slice(1)||'workspace');render();refresh({silent:true});
setInterval(()=>{if(!ctx.demo&&!document.hidden&&!ctx.refreshing)refresh({silent:true});},60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!ctx.demo)refresh({silent:true});});
