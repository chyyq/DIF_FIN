"""Regression from observed recommendation history: no bypass via same-symbol events."""
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from scripts.strategy import TradingCalendar
from scripts.update_data import build_snapshot

CONFIG=json.loads((Path(__file__).resolve().parents[1]/'config/strategy.json').read_text())


class RecommendationIntegrityTests(unittest.TestCase):
    def scenario(self,mode):
        config={**CONFIG,'candidatePoolMode':mode}
        old={'eventId':'old-limit-event','eventDate':'2026-11-09','eventDayIndex':1,
             'symbol':'600001.SH','code':'600001','status':'WATCH','sector':'test',
             'trendScore':100,'price':10,'amount':2e8,'metrics':{'historySessions':150},
             'gates':{'trend':True,'dailyHistory':True},'volatility':{'available':True},
             'buyLevels':[{'structurePlanValid':True,'payoffPassed':True,'reachableToday':True}]}
        new={**old,'eventId':'new-event-with-no-payoff','eventDate':'2026-11-10','eventDayIndex':0,
             'buyLevels':[{'structurePlanValid':True,'payoffPassed':False,'reachableToday':True}]}
        pool={'batchId':'old','startedOn':'2026-11-09','selectionVersion':config['parameterVersion']}
        if mode=='WEEKLY_DAILY':
            pool.update(mode=mode,weekStart='2026-11-09',days=[{'date':'2026-11-09','candidates':[copy.deepcopy(old)]}])
        previous={'pool':pool,'candidates':[old]}
        calendar=TradingCalendar(['2026-11-09','2026-11-10'],'synthetic explicit sessions',True)
        quotes=[{'symbol':old['symbol'],'quoteTimestamp':'2026-11-10T10:00:00+08:00'}]
        with patch('scripts.update_data.evaluate_event',side_effect=lambda e,*args,**kwargs:copy.deepcopy(e)):
            return build_snapshot(previous,[old,new],quotes,{},calendar,config,
                                  datetime.fromisoformat('2026-11-10T10:00:00+08:00'),[],[])[0]

    def test_legacy_compatibility_cannot_replace_member_with_unqualified_new_event(self):
        snapshot=self.scenario('LEGACY_BATCH')
        self.assertEqual(snapshot['candidates'][0]['eventId'],'old-limit-event')
        self.assertEqual(snapshot['candidates'][0]['status'],'CANCEL')

    def test_new_weekday_slot_keeps_old_record_and_excludes_new_event_without_payoff(self):
        snapshot=self.scenario('WEEKLY_DAILY')
        self.assertNotIn('new-event-with-no-payoff',{e['eventId'] for e in snapshot['candidates']})
        monday=next(d for d in snapshot['pool']['days'] if d['date']=='2026-11-09')
        self.assertEqual(monday['candidates'][0]['eventId'],'old-limit-event')
        tuesday=next(d for d in snapshot['pool']['days'] if d['date']=='2026-11-10')
        self.assertEqual(tuesday['candidates'],[])

    def test_actual_recommendation_is_journaled_once_with_its_original_asof(self):
        e={'eventId':'admitted','eventDate':'2026-11-09','eventDayIndex':0,'symbol':'600001.SH',
           'code':'600001','sector':'test','status':'WATCH','price':10,'amount':2e8,
           'metrics':{'historySessions':150},'gates':{'trend':True,'dailyHistory':True},
           'volatility':{'available':True},
           'buyLevels':[{'structurePlanValid':True,'payoffPassed':True,'reachableToday':True}]}
        calendar=TradingCalendar(['2026-11-09'],'synthetic',True)
        now=datetime.fromisoformat('2026-11-09T10:00:00+08:00')
        qs=[{'symbol':e['symbol'],'quoteTimestamp':now.isoformat()}]
        with patch('scripts.update_data.evaluate_event',side_effect=lambda event,*a,**k:copy.deepcopy(event)):
            first,_,audit=build_snapshot({},[e],qs,{},calendar,CONFIG,now,[],[])
            second,_,later=build_snapshot(first,[{**e,'price':12}],qs,{},calendar,CONFIG,now,[],[])
        admitted=[r for r in audit if r['type']=='RECOMMENDATION_RECORDED']
        self.assertEqual(len(admitted),1)
        self.assertEqual(admitted[0]['snapshot']['price'],10)
        self.assertEqual(admitted[0]['asOf'],now.isoformat())
        self.assertEqual(admitted[0]['parameterVersion'],CONFIG['parameterVersion'])
        self.assertFalse(any(r['type']=='RECOMMENDATION_RECORDED' for r in later))
        self.assertEqual(second['pool']['days'][0]['candidates'][0]['price'],10)
