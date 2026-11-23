"""Audit the first recorded recommendation, never a later reanchored event.

This is a nominal-price follow-up of recommendations, not actual trade win rate.
Only later completed, explicitly unadjusted daily bars may enter the outcomes.
No guessed intraday execution, fees, corporate-action facts or parameter uplift.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from math import isfinite

ROOT = Path(__file__).resolve().parents[1]
CN = timezone(timedelta(hours=8))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def audit_records(path):
    """Read an old JSONL or the losslessly archived segments plus current tail.

    An archive manifest is evidence, not a shell instruction. Resolve every
    segment beneath the archive directory; verify its uncompressed digest.
    No record is removed or reclassified as a recommendation without a snapshot.
    """
    path = Path(path)
    if path.is_dir():
        manifest = path/'audit-archive'/'manifest.json'
        if manifest.is_file():
            archive = manifest.parent.resolve()
            for segment in read_json(manifest).get('segments', []):
                target = (archive/segment['file']).resolve()
                if not target.is_relative_to(archive):
                    raise ValueError('Audit segment must stay inside archive directory')
                raw = gzip.decompress(target.read_bytes())
                if hashlib.sha256(raw).hexdigest() != segment['sha256']:
                    raise ValueError('Audit segment checksum mismatch')
                for line in raw.decode('utf-8-sig').splitlines():
                    if line.strip():
                        yield json.loads(line)
        tail = path/'audit.jsonl'
        if tail.is_file():
            yield from audit_records(tail)
        return
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Recommendation evidence needs a timezone')
    return result


def audit_recommendation_sources(path):
    """Only explicit initial-admission facts, never later EVENT_EVALUATED rows."""
    for index, record in enumerate(audit_records(path)):
        if record.get('type') != 'RECOMMENDATION_RECORDED' or not isinstance(record.get('snapshot'), dict):
            continue
        known = record.get('asOf')
        try:
            timestamp(known)
        except (ValueError, TypeError):
            continue
        candidate = record['snapshot']
        yield {'snapshot': {'meta': {'generatedAt': known, 'parameterVersion': record.get('parameterVersion')},
                            'candidates': [candidate]},
               'source': f'audit:RECOMMENDATION_RECORDED:{index}'}


def collect_first_snapshots(sources):
    """Sources contain actual saved latest.json files, not events.json history.

    Later snapshots may change anchors/status. Retain the first immutable
    candidate per eventId. A commit timestamp can delay observation, never move
    it earlier than generatedAt. Pool day snapshots carry their own sealed time.
    """
    records = []
    for source in sources:
        snapshot = source['snapshot']
        generated = timestamp(snapshot['meta']['generatedAt'])
        commit = timestamp(source['committedAt']) if source.get('committedAt') else generated
        known = max(generated, commit)
        base = {'source': source.get('source'), 'parameterVersion': snapshot['meta'].get('parameterVersion')}
        for candidate in snapshot.get('candidates', []):
            records.append({**base, 'knownAt': known.isoformat(), 'candidate': copy.deepcopy(candidate)})
        for day in snapshot.get('pool', {}).get('days', []):
            if not day.get('firstRecordedAt'):
                continue
            recorded = timestamp(day['firstRecordedAt'])
            if recorded > generated or recorded.astimezone(CN).date().isoformat() != day.get('date'):
                continue
            for candidate in day.get('candidates', []):
                records.append({**base, 'knownAt': recorded.isoformat(), 'candidate': copy.deepcopy(candidate)})
    records.sort(key=lambda r: (timestamp(r['knownAt']), r['candidate'].get('eventId', '')))
    first = {}
    for record in records:
        candidate = record['candidate']
        if candidate.get('isSyntheticExample') or str(candidate.get('eventId', '')).startswith('DEMO:'):
            continue
        event_id = candidate.get('eventId')
        if event_id and event_id not in first:
            first[event_id] = record
    return list(first.values())


def valid_rows(rows, as_of):
    result = {}
    for row in rows:
        day = row.get('date', '')
        if not day or day >= as_of:
            continue
        date.fromisoformat(day)
        if day in result:
            raise ValueError('Duplicate daily bar')
        if row.get('adjusted') is not False:
            raise ValueError('Only explicitly unadjusted bars are valid')
        values = [float(row[k]) for k in ('open', 'close', 'high', 'low')]
        op, close, high, low = values
        if not all(isfinite(v) for v in values) or min(values) <= 0 or low > min(op, close) or high < max(op, close):
            raise ValueError('Invalid historical OHLC')
        result[day] = row
    return result


def plan_observations(level, future):
    """Touch-only diagnostics; entry-day extrema never establish an execution."""
    trigger, cap = level.get('triggerPrice'), level.get('maxBuyPrice')
    out = {'id': level.get('id'), 'payoffPassedAtRecommendation': level.get('payoffPassed'),
           'entryZoneObserved': False, 'firstZoneDate': None, 'sameDayOrderingUnknown': False,
           'postZoneTarget1Touch': False, 'postZoneStopTouch': False,
           'laterTargetAndStopSameDay': False, 'executionInferred': False}
    if not isinstance(trigger, (int, float)) or not isinstance(cap, (int, float)) or not 0 < trigger <= cap:
        return {**out, 'status': 'INVALID_OR_MISSING_PLAN'}
    touched = next((i for i, bar in enumerate(future) if bar['low'] <= cap and bar['high'] >= trigger), None)
    if touched is None:
        return {**out, 'status': 'NO_ENTRY_ZONE_OBSERVED'}
    entry_bar = future[touched]
    target, stop = level.get('tp1'), level.get('plannedStop')
    out.update(entryZoneObserved=True, firstZoneDate=entry_bar['date'])
    if target and entry_bar['high'] >= target or stop and entry_bar['low'] <= stop:
        out['sameDayOrderingUnknown'] = True
    # Exclude entry-day highs/lows from subsequent target/stop labels. They may
    # have happened before the zone was touched. T+1 is not simulated here.
    for bar in future[touched + 1:]:
        target_hit = bool(target and bar['high'] >= target)
        stop_hit = bool(stop and bar['low'] <= stop)
        out['postZoneTarget1Touch'] |= target_hit
        out['postZoneStopTouch'] |= stop_hit
        out['laterTargetAndStopSameDay'] |= target_hit and stop_hit
    out['status'] = 'ZONE_TOUCH_ONLY_NO_FILL_OR_WIN_LABEL'
    return out


def review_record(record, histories, calendar, as_of, cost_bps=30):
    c = record['candidate']
    known = timestamp(record['knownAt']).astimezone(CN)
    day = known.date().isoformat()
    result = {'eventId': c['eventId'], 'symbol': c.get('symbol'), 'knownAt': record['knownAt'],
              'recommendationDate': day, 'eventDate': c.get('eventDate'), 'source': record.get('source'),
              'parameterVersion': record.get('parameterVersion'), 'setupType': c.get('eventType', 'LIMIT_UP'),
              'statusAtFirstSnapshot': c.get('status'), 'signalPrice': c.get('price'),
              'priceSourceTimestamp': c.get('quoteTimestamp'),
              'qualityPlanEligibleAtFirstSnapshot': any(l.get('payoffPassed') is True and l.get('structurePlanValid', True) for l in c.get('buyLevels', [])),
              'eligibleObservation': c.get('status') in ('WATCH', 'BUY_READY') and not c.get('stale'),
              'actualFillKnown': False, 'realizedTradeReturnPct': None, 'corporateActionsVerified': False}
    if not result['eligibleObservation']:
        return {**result, 'followUpStatus': 'EXCLUDED_INITIAL_CANCEL_EXPIRE_OR_STALE'}
    # A file uploaded later does not make its old quote fresh. Determine the
    # latest completed source session using only the known clock time/calendar.
    try:
        quote_time = timestamp(c.get('quoteTimestamp')).astimezone(CN)
    except (ValueError, TypeError):
        return {**result, 'eligibleObservation': False, 'followUpStatus': 'EXCLUDED_MISSING_QUOTE_TIMESTAMP'}
    completed_source = max((d for d in calendar if d < day or d == day and (known.hour, known.minute) >= (15, 5)), default=None)
    if quote_time > known or completed_source is None or quote_time.date().isoformat() < completed_source:
        return {**result, 'eligibleObservation': False, 'followUpStatus': 'EXCLUDED_STALE_QUOTE_AT_KNOWN_TIME'}
    expected = sorted({d for d in calendar if day < d < as_of})[:7]
    if len(expected) < 7:
        return {**result, 'followUpStatus': 'MATURING_OR_CALENDAR_NOT_COVERED', 'availableSessionCount': len(expected)}
    try:
        rows = valid_rows(histories.get(c.get('symbol'), []), as_of)
    except (ValueError, KeyError, TypeError):
        return {**result, 'followUpStatus': 'INVALID_OR_ADJUSTED_HISTORY'}
    missing = [d for d in expected if d not in rows]
    if missing:
        return {**result, 'followUpStatus': 'INCOMPLETE_DAILY_HISTORY', 'missingSessions': missing}
    future = [rows[d] for d in expected]
    signal_price = c.get('price')
    if not isinstance(signal_price, (int, float)) or not isfinite(signal_price) or signal_price <= 0:
        return {**result, 'followUpStatus': 'INVALID_SIGNAL_PRICE'}
    end, opening = future[-1]['close'], future[0]['open']
    gross = (end / opening - 1) * 100
    return {**result, 'followUpStatus': 'COMPLETE_NOMINAL_PRICE_FOLLOWUP',
            'sessions': expected, 'entryBenchmarkDate': expected[0], 'entryBenchmarkOpen': opening,
            'seventhSessionDate': expected[-1], 'seventhSessionClose': end,
            'signalToSeventhClosePct': round((end / signal_price - 1) * 100, 4),
            'nextOpenToSeventhClosePct': round(gross, 4),
            'hypotheticalCostBps': cost_bps, 'costAdjustedBenchmarkPct': round(gross - cost_bps / 100, 4),
            'maxFavorablePriceMovePct': round((max(b['high'] for b in future) / signal_price - 1) * 100, 4),
            'maxAdversePriceMovePct': round((min(b['low'] for b in future) / signal_price - 1) * 100, 4),
            'planObservations': [plan_observations(l, future) for l in c.get('buyLevels', [])]}


def distribution(records):
    complete = [r for r in records if r.get('followUpStatus') == 'COMPLETE_NOMINAL_PRICE_FOLLOWUP']
    values = [r['costAdjustedBenchmarkPct'] for r in complete]
    return {'completeEventCount': len(values), 'uniqueSymbols': len({r['symbol'] for r in records}),
            'meanCostAdjustedBenchmarkPct': round(mean(values), 4) if values else None,
            'medianCostAdjustedBenchmarkPct': round(median(values), 4) if values else None,
            'positiveBenchmarkFraction': sum(v > 0 for v in values) / len(values) if values else None,
            'positiveSignalCloseFraction': sum(r['signalToSeventhClosePct'] > 0 for r in complete) / len(complete) if complete else None,
            'meanSignalToSeventhClosePct': round(mean(r['signalToSeventhClosePct'] for r in complete), 4) if complete else None,
            'isActualTradeWinRate': False}


def analyze(sources, histories, calendar, as_of, cost_bps=30):
    date.fromisoformat(as_of)
    first = [r for r in collect_first_snapshots(sources)
             if timestamp(r['knownAt']).astimezone(CN).date().isoformat() <= as_of]
    records = [review_record(r, histories, calendar, as_of, cost_bps) for r in first]
    completed = [r for r in records if r['followUpStatus'] == 'COMPLETE_NOMINAL_PRICE_FOLLOWUP']
    dates = sorted({r['recommendationDate'] for r in completed})
    split = dates[max(1, int(len(dates) * .7))] if len(dates) >= 2 else None
    train = [r for r in completed if split is None or r['recommendationDate'] < split]
    validation_raw = [r for r in completed if split and r['recommendationDate'] >= split]
    symbols = {r['symbol'] for r in train}
    validation = [r for r in validation_raw if r['symbol'] not in symbols]
    blockers = []
    if len(completed) < 30:
        blockers.append('FEWER_THAN_30_COMPLETE_RECOMMENDATION_EVENTS')
    if len({r['symbol'] for r in completed}) < 20:
        blockers.append('FEWER_THAN_20_INDEPENDENT_SYMBOLS')
    if len(train) < 20 or len(validation) < 10:
        blockers.append('INSUFFICIENT_TIME_SEPARATED_TRAIN_AND_VALIDATION')
    if len(dates) < 10:
        blockers.append('FEWER_THAN_10_RECOMMENDATION_DATES')
    blockers.extend(['NO_ACTUAL_FILL_OR_EXIT_LABELS', 'INTRADAY_EXECUTION_AND_ORDERING_UNVERIFIED',
                     'CORPORATE_ACTIONS_AND_MARKET_REGIMES_UNVERIFIED'])
    return {'schemaVersion': 1, 'asOf': as_of, 'strategyId': 'LBR_MAIN_V1',
            'mode': 'NOMINAL_DAILY_FOLLOWUP_NOT_TRADE_BACKTEST', 'autoApply': False,
            'summary': {'snapshotCount': len(sources), 'distinctRecordedEvents': len(records),
                        'distinctSymbols': len({r['symbol'] for r in records}),
                        'firstWatchOrReadyCount': sum(r['statusAtFirstSnapshot'] in ('WATCH', 'BUY_READY') for r in records),
                        'eligibleObservationCount': sum(r['eligibleObservation'] for r in records),
                        'initialBuyReadyCount': sum(r['statusAtFirstSnapshot'] == 'BUY_READY' for r in records),
                        'qualityEligiblePlanCount': sum(r['eligibleObservation'] and r['qualityPlanEligibleAtFirstSnapshot'] for r in records),
                        'eligibleEntryZoneObservedEvents': sum(any(l.get('payoffPassedAtRecommendation') is True and l['entryZoneObserved'] for l in r.get('planObservations', [])) for r in completed),
                        'eligibleEntryZoneNotObservedEvents': sum(r['qualityPlanEligibleAtFirstSnapshot'] and not any(l.get('payoffPassedAtRecommendation') is True and l['entryZoneObserved'] for l in r.get('planObservations', [])) for r in completed),
                        'followUpStatuses': dict(Counter(r['followUpStatus'] for r in records)),
                        'allObservationBenchmark': distribution(completed),
                        'qualityEligiblePlanBenchmark': distribution([r for r in completed if r['qualityPlanEligibleAtFirstSnapshot']]),
                        'byParameterVersion': {str(version): distribution([r for r in completed if r['parameterVersion'] == version]) for version in sorted({r['parameterVersion'] for r in records}, key=str)}},
            'records': records,
            'validation': {'method': 'Chronological recommendation-date 70/30 split; overlapping symbols removed from validation',
                           'splitDate': split, 'training': distribution(train), 'validation': distribution(validation),
                           'overlappingValidationEventsExcluded': len(validation_raw) - len(validation)},
            'optimization': {'status': 'BLOCKED_INSUFFICIENT_OUT_OF_SAMPLE_EVIDENCE',
                             'activeParameterChanges': [], 'blockReasons': blockers, 'winRateImprovementEstablished': False},
            'qualityFindings': [{'code': 'OBSERVATION_WITHOUT_ELIGIBLE_PLAN',
                                 'count': sum(r['eligibleObservation'] and not r['qualityPlanEligibleAtFirstSnapshot'] for r in records),
                                 'eventIds': [r['eventId'] for r in records if r['eligibleObservation'] and not r['qualityPlanEligibleAtFirstSnapshot']],
                                 'action': 'Require full admission on every new/replacement event; retain original event snapshots for rolling slots'}],
            'limitations': ['Every event uses its earliest saved recommendation snapshot; later anchors and outcomes cannot rewrite it',
                            'The first follow-up bar is strictly after the known recommendation date; today is excluded',
                            'WATCH means observation, not an executable buy signal; zero actual fill/exit labels are inferred',
                            'Repeated events for one symbol and overlapping seven-session windows are not independent trades',
                            'Daily entry-zone or target touches do not prove execution, sequencing, T+1 exits or a winning trade',
                            'Raw-price changes exclude verified dividend/split adjustments; benchmark costs are configurable assumptions',
                            'Failed refresh/publication periods create an observation gap, not zero-return or invented recommendation samples']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots', type=Path, default=ROOT/'data', help='Directory with saved latest snapshots or snapshot-manifest.json and snapshots/')
    parser.add_argument('--history', type=Path, default=ROOT/'data/history-cache.json', help='Raw history map, or folder of {symbol, rows} JSON files')
    parser.add_argument('--calendar', type=Path, help='JSON of actual session dates, or {rows:[{date:...}]} index evidence')
    parser.add_argument('--audit', type=Path, help='Optional audit JSONL/GZ or data directory with archive manifest; only explicit RECOMMENDATION_RECORDED facts are used')
    parser.add_argument('--as-of', required=True, help='Exclusive completion date, Shanghai YYYY-MM-DD')
    parser.add_argument('--cost-bps', type=float, default=30)
    parser.add_argument('--output', type=Path, default=ROOT/'data/recommendation-review.json')
    args = parser.parse_args()
    if not 0 <= args.cost_bps <= 1000:
        raise ValueError('Invalid hypothetical round-trip cost')
    manifest = args.snapshots/'snapshot-manifest.json'
    if manifest.is_file():
        sources = [{'snapshot': read_json(args.snapshots/'snapshots'/(m['sha']+'.json')), **m}
                   for m in read_json(manifest) if not m.get('error')]
    else:
        paths = [args.snapshots/'latest.json'] if (args.snapshots/'latest.json').is_file() else sorted(args.snapshots.glob('*.json'))
        sources = [{'snapshot': read_json(p), 'source': p.name} for p in paths if 'meta' in read_json(p)]
    if args.audit:
        sources.extend(audit_recommendation_sources(args.audit))
    if args.history.is_dir():
        evidence = [read_json(p) for p in sorted(args.history.glob('*.json'))]
        histories = {x['symbol']: x['rows'] for x in evidence}
        provenance = [{'symbol': x['symbol'], 'source': x.get('source'), 'adjustment': x.get('adjustment')} for x in evidence]
    else:
        histories, provenance = read_json(args.history), []
    cal = read_json(args.calendar) if args.calendar else (sources[-1]['snapshot'].get('calendar', {}).get('sessions', []) if sources else [])
    calendar = [r['date'] for r in cal.get('rows', [])] if isinstance(cal, dict) and 'rows' in cal else cal.get('sessions', []) if isinstance(cal, dict) else cal
    report = analyze(sources, histories, calendar, args.as_of, args.cost_bps)
    report['sources'] = {'snapshotManifest': [{k: m.get(k) for k in ('source', 'sha256', 'committedAt')} for m in sources],
                         'dailyHistory': provenance}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
