import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.recommendation_review import (analyze, audit_records, collect_first_snapshots,
                                           plan_observations, review_record, audit_recommendation_sources)

CALENDAR = ['2026-11-02','2026-11-03','2026-11-04','2026-11-05','2026-11-06',
            '2026-11-09','2026-11-10','2026-11-11','2026-11-12']


def candidate(**changes):
    return {**{'eventId':'600001:original','symbol':'600001.SH','status':'WATCH','price':10,
               'quoteTimestamp':'2026-11-02T15:00:00+08:00',
               'eventDate':'2026-10-23','buyLevels':[{'id':'B','triggerPrice':9.5,'maxBuyPrice':9.6,
               'tp1':10.2,'plannedStop':9.2,'payoffPassed':True,'structurePlanValid':True}]}, **changes}


def source(c=None, stamp='2026-11-02T16:00:00+08:00', **changes):
    return {'snapshot':{'meta':{'generatedAt':stamp,'parameterVersion':'test'},
                        'candidates':[candidate() if c is None else c]},'source':'synthetic saved snapshot',**changes}


def histories():
    return {'600001.SH':[{'date':d,'open':10,'close':10.1+i*.1,'high':10.3+i*.1,
                         'low':9.4,'adjusted':False} for i,d in enumerate(CALENDAR)]}


class ReviewTests(unittest.TestCase):
    def record(self,c=None):
        return collect_first_snapshots([source(c)])[0]

    def test_first_saved_candidate_is_immutable_and_not_replaced_by_later_anchor(self):
        sources=[source(candidate(price=99,referencePeak=100),stamp='2026-11-10T16:00:00+08:00'),source()]
        original=copy.deepcopy(sources)
        result=collect_first_snapshots(sources)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['candidate']['price'],10)
        self.assertEqual(sources,original)

    def test_commit_time_can_delay_but_never_advance_availability(self):
        record=collect_first_snapshots([source(committedAt='2026-11-03T02:00:00Z')])[0]
        result=review_record(record,histories(),CALENDAR,'2026-11-13')
        self.assertEqual(result['entryBenchmarkDate'],'2026-11-04')

    def test_forward_window_uses_recommendation_not_old_event_date_and_excludes_today(self):
        result=review_record(self.record(),histories(),CALENDAR,'2026-11-12')
        self.assertEqual(result['sessions'],CALENDAR[1:8])
        self.assertEqual(result['seventhSessionDate'],'2026-11-11')
        self.assertFalse(result['actualFillKnown'])

    def test_future_data_and_decision_day_extremes_cannot_change_result(self):
        rows=histories();baseline=review_record(self.record(),rows,CALENDAR,'2026-11-12')
        rows['600001.SH'][0].update(high=1e9,low=.01)
        rows['600001.SH'].append({'date':'2026-11-20','close':1e9})
        self.assertEqual(review_record(self.record(),rows,CALENDAR,'2026-11-12'),baseline)

    def test_maturing_and_missing_bar_do_not_extend_to_later_stock_bars(self):
        self.assertEqual(review_record(self.record(),histories(),CALENDAR,'2026-11-05')['followUpStatus'],'MATURING_OR_CALENDAR_NOT_COVERED')
        rows=histories();rows['600001.SH'].pop(3)
        result=review_record(self.record(),rows,CALENDAR,'2026-11-13')
        self.assertEqual(result['followUpStatus'],'INCOMPLETE_DAILY_HISTORY')
        self.assertEqual(result['missingSessions'],['2026-11-05'])

    def test_adjusted_duplicate_and_nonfinite_history_are_rejected(self):
        for change in ('adjusted','duplicate','nan'):
            rows=histories()
            if change=='adjusted':rows['600001.SH'][1]['adjusted']=True
            elif change=='duplicate':rows['600001.SH'].append(copy.deepcopy(rows['600001.SH'][1]))
            else:rows['600001.SH'][1]['close']=float('nan')
            self.assertEqual(review_record(self.record(),rows,CALENDAR,'2026-11-13')['followUpStatus'],'INVALID_OR_ADJUSTED_HISTORY')

    def test_cancelled_and_synthetic_events_cannot_become_successful_trades(self):
        self.assertEqual(review_record(self.record(candidate(status='CANCEL')),histories(),CALENDAR,'2026-11-13')['followUpStatus'],'EXCLUDED_INITIAL_CANCEL_EXPIRE_OR_STALE')
        self.assertEqual(collect_first_snapshots([source(candidate(isSyntheticExample=True))]),[])

    def test_entry_day_and_both_stop_target_ambiguity_do_not_create_win_labels(self):
        level=candidate()['buyLevels'][0]
        result=plan_observations(level,[{'date':'2026-11-03','low':9.1,'high':10.5},
                                        {'date':'2026-11-04','low':9,'high':10.5}])
        self.assertTrue(result['sameDayOrderingUnknown'])
        self.assertTrue(result['laterTargetAndStopSameDay'])
        self.assertFalse(result['executionInferred'])
        self.assertNotIn('win',result)

    def test_hypothetical_costs_are_deducted_but_not_called_realized_returns(self):
        result=review_record(self.record(),histories(),CALENDAR,'2026-11-13',cost_bps=30)
        self.assertAlmostEqual(result['costAdjustedBenchmarkPct'],result['nextOpenToSeventhClosePct']-.3,places=4)
        self.assertIsNone(result['realizedTradeReturnPct'])

    def test_overlapping_symbols_are_excluded_from_validation_and_tuning_stays_blocked(self):
        sources=[source(),source(candidate(eventId='later',quoteTimestamp='2026-11-03T15:00:00+08:00'),stamp='2026-11-03T16:00:00+08:00')]
        report=analyze(sources,histories(),CALENDAR,'2026-11-13')
        self.assertEqual(report['validation']['overlappingValidationEventsExcluded'],1)
        self.assertFalse(report['optimization']['winRateImprovementEstablished'])
        self.assertEqual(report['optimization']['activeParameterChanges'],[])

    def test_archived_audit_order_and_checksum_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=root/'audit-archive';archive.mkdir()
            raw=b'{"type":"old"}\n'
            (archive/'part.jsonl.gz').write_bytes(gzip.compress(raw))
            manifest={'segments':[{'file':'part.jsonl.gz','sha256':hashlib.sha256(raw).hexdigest()}]}
            (archive/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            (root/'audit.jsonl').write_text('{"type":"new"}\n',encoding='utf-8')
            self.assertEqual([x['type'] for x in audit_records(root)],['old','new'])
            manifest['segments'][0]['sha256']='bad';(archive/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            with self.assertRaises(ValueError):list(audit_records(root))

    def test_old_quote_is_not_made_fresh_by_a_later_saved_file(self):
        record=collect_first_snapshots([source(committedAt='2026-11-04T02:00:00Z')])[0]
        result=review_record(record,histories(),CALENDAR,'2026-11-13')
        self.assertFalse(result['eligibleObservation'])
        self.assertEqual(result['followUpStatus'],'EXCLUDED_STALE_QUOTE_AT_KNOWN_TIME')

    def test_future_recommendation_event_is_not_included_in_past_report(self):
        report=analyze([source(),source(candidate(eventId='future'),stamp='2026-11-25T10:00:00+08:00')],histories(),CALENDAR,'2026-11-13')
        self.assertEqual(report['summary']['distinctRecordedEvents'],1)

    def test_explicit_admission_audit_is_used_but_later_event_updates_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audit.jsonl'
            records=[{'type':'EVENT_EVALUATED','asOf':'2026-11-02T10:00:00+08:00','snapshot':candidate(price=99)},
                     {'type':'RECOMMENDATION_RECORDED','asOf':'2026-11-02T16:00:00+08:00','parameterVersion':'test','snapshot':candidate()}]
            p.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
            first=collect_first_snapshots(list(audit_recommendation_sources(p)))
            self.assertEqual(first[0]['candidate']['price'],10)
            self.assertEqual(first[0]['parameterVersion'],'test')

    def test_real_publisher_compaction_stays_readable_without_losing_repeated_records(self):
        from scripts.publish_data import compact_audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            values=[{'type':'RECOMMENDATION_RECORDED','asOf':'2026-11-02T16:00:00+08:00',
                     'snapshot':{'eventId':str(i)}} for i in [1,2,1,2,3]]
            (root/'audit.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in values),encoding='utf-8')
            compact_audit(root,segment_bytes=250)
            self.assertEqual(list(audit_records(root)),values)


if __name__=='__main__':unittest.main()
