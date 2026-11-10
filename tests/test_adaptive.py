"""Synthetic as-of scenarios for the default v1.1 strategy, never market advice."""
import copy
import json
import math
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.adaptive import adaptive_levels, admission_ok, falling_risk, history_probes, market_profile, ranking_key
from scripts.strategy import TradingCalendar, evaluate_event, hard_veto, rotate_pool
from scripts.update_data import build_snapshot
from scripts.learning import analyze

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config/strategy.json').read_text())
NOW = datetime.fromisoformat('2026-11-02T10:00:00+08:00')


def rows(scale=1, width=.15):
    # Synthetic daily dates are not used as a production exchange calendar.
    start = date(2026, 10, 30) - timedelta(days=159)
    return [{'date': (start + timedelta(days=i)).isoformat(), 'open': (10 + .01*i)*scale,
             'close': (10 + .01*i)*scale, 'high': (10 + .01*i + width)*scale,
             'low': (10 + .01*i - width)*scale, 'adjusted': False} for i in range(160)]


def profile():
    return {'available': True, 'asOf':'2026-10-30', 'atr14':.4, 'atrPct':4, 'close':10,
            'ma10':9.8, 'ma20':9.6, 'support':9, 'recentPeak':10,
            'return5Pct':2,'return10Pct':4,'consecutiveDeclines':0,
            'pullbackMedian':.04,'pullback75':.07,'historySessions':130}


def quote(code='600001', price=9.8, **kwargs):
    return {'symbol':code+'.SH','code':code,'name':'模拟样本','segment':'SSE_MAIN','sector':'模拟行业',
            'price':price,'high':10,'low':9.7,'preClose':9.9,'pct':-1,'amount':200_000_000,
            'quoteTimestamp':NOW.isoformat(),'limitUpPrice':10.89,'limitDownPrice':8.91,
            'limitPriceVerified':True,'stale':False,**kwargs}


def eligible(code='600001', price=10, **kwargs):
    return {'eventId':code+':event','symbol':code+'.SH','eventDate':'2026-11-02',
            'eventDayIndex':0,'sector':code,'status':'WATCH','trendScore':100,'price':price,
            'amount':200_000_000,'gates':{'trend':True,'dailyHistory':True},'volatility':profile(),
            'metrics':{'historySessions':130},'riskVetoReasons':[],
            'buyLevels':[{'structurePlanValid':True,'payoffPassed':True,'reachableToday':True}],**kwargs}


class VolatilityTests(unittest.TestCase):
    def test_default_enables_adaptive_and_both_setup_families(self):
        self.assertTrue(CONFIG['adaptivePricing']['enabled'])
        self.assertTrue(CONFIG['selectionRules']['includeTrendPullbacks'])

    def test_more_volatile_stock_has_deeper_prices(self):
        quiet=market_profile(rows(width=.08),'2026-11-02',CONFIG)
        volatile=market_profile(rows(width=.4),'2026-11-02',CONFIG)
        a=adaptive_levels(12,quiet,CONFIG)
        b=adaptive_levels(12,volatile,CONFIG)
        self.assertLess(b[0]['triggerPrice'],a[0]['triggerPrice'])
        self.assertGreater(b[1]['drawdownPct'],a[1]['drawdownPct'])

    def test_no_fixed_eleven_thirteen_grid(self):
        p=profile()
        levels=adaptive_levels(10,p,CONFIG)
        self.assertNotEqual([x['drawdownPct'] for x in levels],[11,13])
        self.assertTrue(all(x['priceMethod']=='ATR14_SWING_SUPPORT' for x in levels))

    def test_future_bars_do_not_change_prices(self):
        original=market_profile(rows(),'2026-11-02',CONFIG)
        polluted=market_profile(rows()+[{'date':'2026-11-02','close':1e9}], '2026-11-02',CONFIG)
        self.assertEqual(original,polluted)

    def test_adjusted_history_cannot_be_execution_anchor(self):
        history=rows()
        history[-1]['adjusted']=True
        self.assertEqual(market_profile(history,'2026-11-02',CONFIG)['reason'],'ADJUSTED_HISTORY_NOT_EXECUTABLE')
        self.assertEqual(adaptive_levels(10,{'available':False},CONFIG),[])

    def test_missing_duplicate_and_invalid_ohlc_history_fail_closed(self):
        for history in (rows()[:10],rows()[:-1]+[rows()[-2]],rows()[:-1]+[{**rows()[-1],'low':1000}]):
            self.assertFalse(market_profile(history,'2026-11-02',CONFIG)['available'])

    def test_price_scaling_preserves_relative_volatility_and_bands(self):
        a=market_profile(rows(),'2026-11-02',CONFIG)
        b=market_profile(rows(scale=3),'2026-11-02',CONFIG)
        self.assertAlmostEqual(a['atrPct'],b['atrPct'])
        self.assertAlmostEqual(a['atr14']*3,b['atr14'])

    def test_cap_stop_targets_and_ratio_are_coherent(self):
        for level in adaptive_levels(10,profile(),CONFIG):
            self.assertLess(level['triggerPrice'],10)
            self.assertGreaterEqual(level['maxBuyPrice'],level['triggerPrice'])
            self.assertLess(level['maxBuyPrice'],level['tp1'])
            self.assertLess(level['tp1'],level['tp2'])
            self.assertLess(level['plannedStop'],level['triggerPrice'])
            self.assertAlmostEqual(level['tp1Pct'],100*(level['tp1']/level['maxBuyPrice']-1),places=5)

    def test_below_support_and_below_daily_limit_cannot_pass(self):
        p=profile();p['support']=9.9
        self.assertTrue(any(not x['structurePlanValid'] for x in adaptive_levels(10,p,CONFIG)))
        self.assertTrue(all(not x['reachableToday'] for x in adaptive_levels(10,profile(),CONFIG,9.9)))


class RiskAndRankingTests(unittest.TestCase):
    def test_recent_crash_cannot_hide_behind_long_term_uptrend(self):
        p={**profile(),'return5Pct':-12,'return10Pct':-18}
        reasons,_=falling_risk(p,9.8,CONFIG)
        self.assertIn('RECENT_RAPID_DECLINE',reasons)

    def test_support_break_peak_collapse_and_excessive_volatility(self):
        self.assertIn('DAILY_SUPPORT_BROKEN',falling_risk(profile(),8,CONFIG)[0])
        self.assertIn('RECENT_PEAK_COLLAPSE',falling_risk(profile(),7,CONFIG)[0])
        self.assertIn('VOLATILITY_TOO_HIGH',falling_risk({**profile(),'atrPct':9},9.8,CONFIG)[0])

    def test_risk_thresholds_respond_to_stock_volatility_with_caps(self):
        _,a=falling_risk({**profile(),'atrPct':1},9.8,CONFIG)
        _,b=falling_risk({**profile(),'atrPct':4},9.8,CONFIG)
        self.assertGreater(b['fiveDayLossLimitPct'],a['fiveDayLossLimitPct'])
        self.assertLessEqual(b['fiveDayLossLimitPct'],10)

    def test_lower_price_wins_only_within_same_quality_band(self):
        options=[eligible('600003',25),eligible('600002',5),eligible('600001',3,trendScore=66)]
        ranked=sorted(options,key=lambda x:ranking_key(x,CONFIG))
        self.assertEqual([x['price'] for x in ranked],[5,25,3])

    def test_default_price_ceiling_is_hard_veto(self):
        self.assertIn('CANDIDATE_PRICE_TOO_HIGH',hard_veto(quote(price=61),CONFIG))
        self.assertFalse(admission_ok(eligible(price=61),CONFIG))

    def test_low_price_does_not_override_bad_trend_risk_or_missing_plan(self):
        for changes in ({'gates':{'trend':False}}, {'riskVetoReasons':['RECENT_RAPID_DECLINE']},
                        {'volatility':{'available':False}}, {'buyLevels':[]}, {'stale':True}):
            self.assertFalse(admission_ok(eligible(price=2,**changes),CONFIG))

    def test_new_policy_reselects_old_pool_without_touching_positions(self):
        previous={'pool':{'batchId':'old','startedOn':'2026-11-02','selectionVersion':'1.0.0'},'candidates':[eligible(price=100)]}
        calendar=TradingCalendar(['2026-10-30','2026-11-02'],'synthetic explicit session',True)
        pool,candidates,archives=rotate_pool(previous,[eligible(price=6)],calendar,CONFIG,'2026-11-02')
        self.assertEqual(candidates[0]['price'],6)
        self.assertTrue(pool['ruleChangeRefresh'])
        self.assertEqual(len(archives),1)

    def test_same_version_retains_seven_session_membership(self):
        calendar=TradingCalendar(['2026-11-02','2026-11-03'],'synthetic explicit sessions',True)
        first,chosen,_=rotate_pool({},[eligible(price=10)],calendar,CONFIG,'2026-11-02')
        pool,updated,_=rotate_pool({'pool':first,'candidates':chosen},[eligible(price=11),eligible('600002',2)],calendar,CONFIG,'2026-11-03')
        self.assertTrue(pool['retained'])
        self.assertEqual([e['symbol'] for e in updated],['600001.SH'])

    def test_both_setup_types_share_selection_without_duplicating_symbols(self):
        options=[eligible('600001',10,eventType='LIMIT_UP'),eligible('600002',8,eventType='TREND_PULLBACK'),
                 eligible('600001',10,eventType='TREND_PULLBACK')]
        _,selected,_=rotate_pool({},options,TradingCalendar(['2026-11-02'],'synthetic',True),CONFIG,'2026-11-02')
        self.assertEqual([e['eventType'] for e in selected],['TREND_PULLBACK','LIMIT_UP'])
        self.assertEqual(len({e['symbol'] for e in selected}),2)


class SetupAndPipelineTests(unittest.TestCase):
    def test_history_queue_covers_non_limit_pullbacks_and_excludes_expensive(self):
        qs=[quote('600001',12),quote('600002',15,pct=-2),quote('600003',80),
            quote('600004',8,high=10.89),quote('600005',7,segment='CHINEXT')]
        symbols={q['symbol'] for q in history_probes(qs,set(),{},CONFIG)}
        self.assertEqual(symbols,{'600001.SH','600002.SH','600004.SH'})

    def test_non_limit_uptrend_is_discovered_without_fake_limit_event(self):
        q=quote(price=11.5)
        calendar=TradingCalendar(['2026-10-30','2026-11-02'],'synthetic explicit session',True)
        snapshot,events,_=build_snapshot({},[],[q],{q['symbol']:rows()},calendar,CONFIG,NOW,[],[])
        setup=next(e for e in events if e.get('eventType')=='TREND_PULLBACK')
        self.assertIsNone(setup['limitUpPrice'])
        self.assertIsNone(setup['gates']['limitEvent'])
        self.assertTrue(setup['gates']['setup'])
        self.assertNotEqual(setup['status'],'BUY_READY')

    def test_downtrend_cannot_create_non_limit_setup(self):
        q=quote(price=11.5)
        down=rows()
        for i,row in enumerate(down):
            for key in ('open','high','low','close'):row[key]=30-row[key]
            row['high'],row['low']=row['low'],row['high']
        _,events,_=build_snapshot({},[],[q],{q['symbol']:down},TradingCalendar(['2026-11-02'],'synthetic',True),CONFIG,NOW,[],[])
        self.assertFalse(any(e.get('eventType')=='TREND_PULLBACK' for e in events))

    def test_non_limit_keeps_symbol_and_calendar_safety(self):
        q=quote(price=11.5)
        event={'eventId':'t','eventDate':'2026-11-02','eventType':'TREND_PULLBACK','trendSetupVerified':True,
               'symbol':q['symbol'],'segment':q['segment'],'referencePeak':12,'limitUpPrice':None}
        q['symbol']='600002.SH'
        out=evaluate_event(event,q,rows(),TradingCalendar(['2026-11-02'],'synthetic',True),CONFIG,'2026-11-02',now=NOW)
        self.assertIn('INSTRUMENT_MISMATCH',out['hardVetoReasons'])

    def test_verified_non_limit_setup_can_pass_without_claiming_a_limit_touch(self):
        # A controlled computed-feature fixture tests signal semantics only;
        # it is not fitted market history or evidence of strategy performance.
        q=quote(price=9.02,eventLowVerified=True,eventLowAsOf=9,
                securityMetadataVerified=True,announcementsVerified=True,
                sectorVerifiedStable=True,structureVerified=True,accountStateSynced=True)
        event={'eventId':'ready-trend','eventDate':'2026-11-02','eventType':'TREND_PULLBACK',
               'trendSetupVerified':True,'symbol':q['symbol'],'segment':q['segment'],'referencePeak':10}
        prices=[(9.06,9,8.99,10_000_000,2),(9,9.005,8.99,8_000_000,1.9),
                (9.005,9.01,9,4_000_000,.5),(9.01,9.015,9.005,4_000_000,.5),
                (9.015,9.02,9.01,4_000_000,.5)]
        bars=[{'open':o,'close':c,'low':lo,'amount':amount,'rvol':rvol,'baselineSessions':20,
               'complete':True,'vwap':c-.01,'end':(NOW-timedelta(minutes=20-i*5)).isoformat()}
              for i,(o,c,lo,amount,rvol) in enumerate(prices)]
        with patch('scripts.adaptive.market_profile',return_value=profile()):
            out=evaluate_event(event,q,rows(),TradingCalendar(['2026-10-30','2026-11-02'],'synthetic',True),CONFIG,'2026-11-02',bars,NOW)
        self.assertEqual(out['status'],'BUY_READY',out['reasonCodes'])
        self.assertIsNone(out['gates']['limitEvent'])
        self.assertTrue(out['gates']['setup'])
        self.assertTrue(admission_ok(out,CONFIG))

    def test_day_after_still_uses_existing_trend_event(self):
        q=quote(price=11.5)
        calendar=TradingCalendar(['2026-11-02','2026-11-03'],'synthetic',True)
        first,events,_=build_snapshot({},[],[q],{q['symbol']:rows()},calendar,CONFIG,NOW,[],[])
        q['quoteTimestamp']='2026-11-03T10:00:00+08:00'
        _,second,_=build_snapshot(first,events,[q],{q['symbol']:rows()},calendar,CONFIG,datetime.fromisoformat(q['quoteTimestamp']),[],[])
        self.assertEqual(len([e for e in second if e.get('eventType')=='TREND_PULLBACK']),1)

    def test_new_volatility_plan_invalidates_old_quote_version(self):
        q=quote(price=11.5)
        event={'eventId':'t','eventDate':'2026-11-02','eventType':'TREND_PULLBACK','trendSetupVerified':True,
               'symbol':q['symbol'],'segment':q['segment'],'referencePeak':12,'eventVersion':2,
               'buyLevels':[{'id':'A','triggerPrice':1,'maxBuyPrice':1,'touched':True}]}
        out=evaluate_event(event,q,rows(),TradingCalendar(['2026-11-02'],'synthetic',True),CONFIG,'2026-11-02',now=NOW)
        self.assertEqual(out['eventVersion'],3)
        self.assertTrue(out['pendingOrdersInvalidated'])

    def test_missing_recent_daily_bar_blocks_pool_admission(self):
        q=quote(price=11.5)
        event={'eventId':'t','eventDate':'2026-11-02','eventType':'TREND_PULLBACK','trendSetupVerified':True,
               'symbol':q['symbol'],'segment':q['segment'],'referencePeak':12}
        out=evaluate_event(event,q,rows()[:-1],TradingCalendar(['2026-10-30','2026-11-02'],'synthetic',True),CONFIG,'2026-11-02',now=NOW)
        self.assertIn('DAILY_HISTORY_STALE_OR_UNVERIFIED',out['reasonCodes'])
        self.assertFalse(admission_ok(out,CONFIG))

    def test_learning_keeps_setup_and_price_method_groups(self):
        events=[eligible('600001',10,eventType='LIMIT_UP',pricingMethod='ATR14_SWING_SUPPORT'),
                eligible('600002',8,eventType='TREND_PULLBACK',pricingMethod='ATR14_SWING_SUPPORT')]
        report=analyze(events,{},CONFIG)
        self.assertIn('setup:LIMIT_UP',report['groups'])
        self.assertIn('setup:TREND_PULLBACK',report['groups'])
        self.assertEqual(report['closedTradeCount'],0)
        self.assertFalse(report['autoApply'])


if __name__=='__main__':unittest.main()
