"""Daily retention, Monday rollover and outage semantics, with synthetic events."""
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from scripts.weekly_pool import build_weekly_pool
from scripts.update_data import build_snapshot, stale_snapshot
from scripts.strategy import TradingCalendar

CONFIG = json.loads((Path(__file__).resolve().parents[1]/'config/strategy.json').read_text())


def event(code='600001', day='2026-11-09', **changes):
    return dict(eventId=f'{code}:{day}', symbol=f'{code}.SH', code=code, name=code,
                eventDate=day, eventDayIndex=0, price=10, sector=code, amount=2e8,
                status='WATCH', trendScore=100, gates={'trend':True,'dailyHistory':True},
                volatility={'available':True}, metrics={'historySessions':130},
                buyLevels=[{'structurePlanValid':True,'payoffPassed':True,'reachableToday':True}], **changes)


def refresh(previous, events, day, quote_day=None, config=CONFIG):
    pool, candidates, archives=build_weekly_pool(previous,events,config,day,quote_day or day,day+'T14:00:00+08:00')
    return {'pool':pool,'candidates':candidates,'meta':{'generatedAt':day+'T14:00:00+08:00'}}, archives


class WeeklyPoolTests(unittest.TestCase):
    def test_live_pipeline_retains_daily_membership_and_current_event_states(self):
        calendar=TradingCalendar(['2026-11-09','2026-11-10'],'explicit synthetic sessions',True)
        a=event()
        def build(previous, events, day):
            quotes=[{'symbol':e['symbol'],'quoteTimestamp':day+'T10:00:00+08:00'} for e in events]
            with patch('scripts.update_data.evaluate_event',side_effect=lambda e,*args,**kwargs:copy.deepcopy(e)):
                return build_snapshot(previous,events,quotes,{},calendar,CONFIG,
                                      datetime.fromisoformat(day+'T10:00:00+08:00'),[],[])
        first,_,_=build({},[a],'2026-11-09')
        second,_,_=build(first,[{**a,'status':'CANCEL'},event('600002','2026-11-10')],'2026-11-10')
        self.assertEqual([d['date'] for d in second['pool']['days']],['2026-11-09','2026-11-10'])
        self.assertEqual(second['pool']['days'][0]['candidates'][0]['status'],'WATCH')
        self.assertEqual(second['candidates'][0]['status'],'CANCEL')

    def test_daily_refresh_retains_prior_days_with_current_risk_state(self):
        monday=event()
        first,_=refresh({},[monday],'2026-11-09')
        second,_=refresh(first,[{**monday,'price':9,'status':'CANCEL'},event('600002','2026-11-10')],'2026-11-10')
        self.assertEqual([d['date'] for d in second['pool']['days']],['2026-11-09','2026-11-10'])
        self.assertEqual(second['pool']['days'][0]['candidates'][0]['price'],10)
        self.assertEqual(second['candidates'][0]['status'],'CANCEL')
        self.assertEqual(second['candidates'][0]['price'],9)

    def test_same_day_additions_never_remove_or_rewrite_original_selection(self):
        first,_=refresh({},[event()],'2026-11-09')
        untouched=copy.deepcopy(first)
        second,_=refresh(first,[{**event(),'price':12},event('600002')],'2026-11-09')
        self.assertEqual(len(second['pool']['days']),1)
        self.assertEqual(len(second['pool']['days'][0]['candidates']),2)
        self.assertEqual(second['pool']['days'][0]['candidates'][0]['price'],10)
        self.assertEqual(first,untouched)

    def test_same_symbol_new_event_does_not_replace_mondays_event(self):
        first,_=refresh({},[event()],'2026-11-09')
        second,_=refresh(first,[{**event(),'status':'CANCEL'},event(day='2026-11-10')],'2026-11-10')
        self.assertEqual(second['pool']['days'][0]['candidates'][0]['eventId'],'600001:2026-11-09')
        self.assertEqual(second['pool']['days'][1]['candidates'][0]['eventId'],'600001:2026-11-10')
        self.assertEqual(len(second['candidates']),2)

    def test_monday_reset_is_calendar_based_even_with_friday_quotes(self):
        friday,_=refresh({},[event(day='2026-11-13')],'2026-11-13')
        weekend,_=refresh(friday,[event(day='2026-11-13')],'2026-11-14','2026-11-13')
        self.assertEqual(weekend['pool']['days'],friday['pool']['days'])
        monday,archives=refresh(weekend,[event(day='2026-11-13')],'2026-11-16','2026-11-13')
        self.assertEqual(monday['pool']['weekStart'],'2026-11-16')
        self.assertEqual(monday['pool']['nextRefreshOn'],'2026-11-23')
        self.assertEqual(monday['pool']['days'],[])
        self.assertEqual(monday['candidates'],[])
        self.assertEqual(len(archives),1)

    def test_wednesday_initialization_does_not_invent_monday_and_tuesday(self):
        snapshot,_=refresh({},[event(day='2026-11-11')],'2026-11-11')
        self.assertEqual([d['date'] for d in snapshot['pool']['days']],['2026-11-11'])

    def test_empty_valid_session_is_saved_distinct_from_missing_day(self):
        snapshot,_=refresh({},[],'2026-11-11')
        self.assertEqual(snapshot['pool']['days'][0]['candidates'],[])

    def test_outage_keeps_daily_history_and_resets_on_next_monday(self):
        first,_=refresh({},[event()],'2026-11-09')
        failed=stale_snapshot(first,CONFIG,datetime.fromisoformat('2026-11-10T10:00:00+08:00'),['offline'],[])
        self.assertEqual(failed['pool']['days'],first['pool']['days'])
        self.assertEqual(failed['candidates'][0]['stale'],True)
        reset=stale_snapshot(failed,CONFIG,datetime.fromisoformat('2026-11-16T00:00:00+08:00'),['offline'],[])
        self.assertEqual(reset['pool']['days'],[])
        self.assertEqual(reset['stats']['candidateCount'],0)

    def test_parameter_changes_do_not_erase_current_week(self):
        first,_=refresh({},[event()],'2026-11-09')
        cfg={**CONFIG,'parameterVersion':'test-next'}
        second,_=refresh(first,[],'2026-11-10',config=cfg)
        self.assertEqual(second['pool']['days'][0],first['pool']['days'][0])

    def test_legacy_migration_uses_only_known_date_and_preserves_input(self):
        old={'pool':{'startedOn':'2026-11-09'},'candidates':[event()], 'meta':{'generatedAt':'2026-11-10T14:00:00+08:00'}}
        upgraded,_=refresh(old,[],'2026-11-11')
        self.assertEqual([d['date'] for d in upgraded['pool']['days']],['2026-11-10','2026-11-11'])
        self.assertTrue(upgraded['pool']['days'][0]['migrated'])

    def test_per_day_price_risk_and_capacity_guards_remain(self):
        cfg={**CONFIG,'maxCandidates':1}
        snapshot,_=refresh({},[event('600001',riskVetoReasons=['DROP']),event('600002'),event('600003')],'2026-11-09',config=cfg)
        self.assertEqual([e['symbol'] for e in snapshot['candidates']],['600002.SH'])


if __name__=='__main__':unittest.main()
