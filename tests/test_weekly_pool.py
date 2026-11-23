"""Weekday-slot replacement and outage semantics, with synthetic events."""
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

    def test_new_monday_with_friday_quotes_keeps_existing_slots(self):
        friday,_=refresh({},[event(day='2026-11-13')],'2026-11-13')
        weekend,_=refresh(friday,[event(day='2026-11-13')],'2026-11-14','2026-11-13')
        self.assertEqual(weekend['pool']['days'],friday['pool']['days'])
        monday,archives=refresh(weekend,[event(day='2026-11-13')],'2026-11-16','2026-11-13')
        self.assertEqual(monday['pool']['weekStart'],'2026-11-16')
        self.assertEqual(monday['pool']['rotation'],'WEEKDAY_ROLLING')
        self.assertEqual(monday['pool']['days'],friday['pool']['days'])
        self.assertEqual(len(monday['candidates']),1)
        self.assertEqual(archives,[])

    def test_wednesday_initialization_does_not_invent_monday_and_tuesday(self):
        snapshot,_=refresh({},[event(day='2026-11-11')],'2026-11-11')
        self.assertEqual([d['date'] for d in snapshot['pool']['days']],['2026-11-11'])

    def test_empty_valid_session_is_saved_distinct_from_missing_day(self):
        snapshot,_=refresh({},[],'2026-11-11')
        self.assertEqual(snapshot['pool']['days'][0]['candidates'],[])

    def test_outage_keeps_daily_history_across_new_monday(self):
        first,_=refresh({},[event()],'2026-11-09')
        failed=stale_snapshot(first,CONFIG,datetime.fromisoformat('2026-11-10T10:00:00+08:00'),['offline'],[])
        self.assertEqual(failed['pool']['days'],first['pool']['days'])
        self.assertEqual(failed['candidates'][0]['stale'],True)
        reset=stale_snapshot(failed,CONFIG,datetime.fromisoformat('2026-11-16T00:00:00+08:00'),['offline'],[])
        self.assertEqual(reset['pool']['days'],first['pool']['days'])
        self.assertEqual(reset['stats']['candidateCount'],1)

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

    def full_week(self):
        previous = {}
        for index, day in enumerate(['2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13']):
            previous, _ = refresh(previous, [event(f'60000{index}', day)], day)
        return previous

    def test_new_monday_replaces_only_monday_then_tuesday_replaces_only_tuesday(self):
        first = self.full_week()
        old = copy.deepcopy(first)
        monday, archives = refresh(first, [event('600010','2026-11-16')], '2026-11-16')
        self.assertEqual([d['date'] for d in monday['pool']['days']],
                         ['2026-11-16','2026-11-10','2026-11-11','2026-11-12','2026-11-13'])
        self.assertEqual(monday['pool']['days'][1:], old['pool']['days'][1:])
        self.assertEqual(archives[0]['archiveReason'], 'WEEKDAY_SLOT_REPLACED')
        self.assertEqual(archives[0]['recommendationDate'], '2026-11-09')
        self.assertEqual(len(monday['candidates']), 5)
        tuesday, archives = refresh(monday, [event('600011','2026-11-17')], '2026-11-17')
        self.assertEqual([d['date'] for d in tuesday['pool']['days']],
                         ['2026-11-16','2026-11-17','2026-11-11','2026-11-12','2026-11-13'])
        self.assertEqual(tuesday['pool']['days'][0], monday['pool']['days'][0])
        self.assertEqual(archives[0]['weekday'], 1)
        self.assertEqual(first, old)

    def test_empty_success_replaces_own_slot_but_failure_does_not(self):
        first = self.full_week()
        monday, archives = refresh(first, [], '2026-11-16')
        self.assertEqual(monday['pool']['days'][0]['candidates'], [])
        self.assertEqual(len(monday['pool']['days']), 5)
        self.assertEqual(len(archives), 1)
        failed = stale_snapshot(first, CONFIG, datetime.fromisoformat('2026-11-16T10:00:00+08:00'), ['offline'], [])
        self.assertEqual(failed['pool']['days'], first['pool']['days'])

    def test_missing_weekday_and_long_holiday_do_not_discard_known_records(self):
        first = self.full_week()
        pool, members, archives = build_weekly_pool(first, [], CONFIG, '2026-12-07', None)
        self.assertEqual(pool['days'], first['pool']['days'])
        self.assertEqual(len(members), 5)
        self.assertEqual(archives, [])
        self.assertTrue(all(c['stale'] for c in members))

    def test_duplicate_legacy_dates_keep_newest_known_day_per_weekday(self):
        first = self.full_week()
        original = copy.deepcopy(first['pool']['days'][0])
        original['date'] = '2026-11-02'
        first['pool']['days'].append(original)
        first['pool']['days'].append({**original, 'date':'2026-11-30'})
        first['pool']['days'].append({**original, 'date':'invalid'})
        pool, _, _ = build_weekly_pool(first, [], CONFIG, '2026-11-16')
        self.assertEqual(len(pool['days']), 5)
        self.assertEqual(pool['days'][0]['date'], '2026-11-09')

    def test_legacy_flat_snapshot_from_prior_week_is_retained_until_own_weekday(self):
        old = {'pool': {'startedOn':'2026-11-09'}, 'candidates':[event()],
               'meta':{'generatedAt':'2026-11-09T14:00:00+08:00'}}
        upgraded, _ = refresh(old, [event('600012','2026-11-17')], '2026-11-17')
        self.assertEqual([d['date'] for d in upgraded['pool']['days']], ['2026-11-09','2026-11-17'])
        self.assertTrue(upgraded['pool']['days'][0]['migrated'])

    def test_an_event_shared_by_two_days_survives_replacement_of_only_one_slot(self):
        first, _ = refresh({}, [event()], '2026-11-09')
        tuesday, _ = refresh(first, [event()], '2026-11-10')
        monday, _ = refresh(tuesday, [], '2026-11-16')
        self.assertEqual(len(monday['candidates']), 1)
        self.assertEqual(monday['pool']['days'][1]['candidates'][0]['eventId'], event()['eventId'])

    def test_lost_current_quote_never_revives_an_expired_or_cancelled_history_record(self):
        first, _ = refresh({}, [event()], '2026-11-09')
        first['pool']['days'][0]['candidates'][0]['status'] = 'EXPIRE'
        pool, members, _ = build_weekly_pool(first, [], CONFIG, '2026-11-16')
        self.assertEqual(members[0]['status'], 'EXPIRE')
        self.assertTrue(members[0]['stale'])

    def test_per_day_price_risk_and_capacity_guards_remain(self):
        cfg={**CONFIG,'maxCandidates':1}
        snapshot,_=refresh({},[event('600001',riskVetoReasons=['DROP']),event('600002'),event('600003')],'2026-11-09',config=cfg)
        self.assertEqual([e['symbol'] for e in snapshot['candidates']],['600002.SH'])


if __name__=='__main__':unittest.main()
