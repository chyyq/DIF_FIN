"""Monday-Friday recommendation history, independent from the private ledger."""
from __future__ import annotations

import copy
from datetime import date, timedelta

try:
    from .adaptive import admission_ok, ranking_key
except ImportError:
    from adaptive import admission_ok, ranking_key


def week_start(day: str) -> str:
    current = date.fromisoformat(day)
    return (current - timedelta(days=current.weekday())).isoformat()


def eligible_daily(events, config):
    options = sorted((e for e in events if e.get('eventDayIndex') is not None
                      and 0 <= e['eventDayIndex'] < config.get('candidateSessions', 7)
                      and e.get('status') not in ('CANCEL', 'EXPIRE') and admission_ok(e, config)),
                     key=lambda e: ranking_key(e, config))
    return options


def build_weekly_pool(previous, events, config, as_of, quote_day=None, stamp=None):
    """Append today's choices; never replace another day's membership or evidence.

    days[].candidates are the original recommendation snapshots. The returned
    flat list holds their current event states, including CANCEL/EXPIRE. A new
    event for the same symbol must not rewrite an earlier recommendation.
    """
    monday = week_start(as_of)
    start = date.fromisoformat(monday)
    friday = (start + timedelta(days=4)).isoformat()
    next_monday = (start + timedelta(days=7)).isoformat()
    old = previous.get('pool', {})
    same_week = old.get('mode') == 'WEEKLY_DAILY' and old.get('weekStart') == monday
    days = copy.deepcopy(old.get('days', [])) if same_week else []
    archives = [] if same_week else [dict(e, archiveReason='WEEK_RESET', archivedOn=as_of)
                                    for e in previous.get('candidates', [])]
    # Upgrade only the snapshot date we actually possess. Never manufacture
    # Monday/Tuesday recommendations from Wednesday's current market data.
    legacy_day = str(previous.get('meta', {}).get('generatedAt') or '')[:10]
    if old.get('mode') != 'WEEKLY_DAILY' and legacy_day and monday <= legacy_day <= min(friday, as_of):
        days.append({'date': legacy_day, 'firstRecordedAt': previous['meta']['generatedAt'],
                     'lastUpdatedAt': previous['meta']['generatedAt'], 'migrated': True,
                     'candidates': copy.deepcopy(previous.get('candidates', []))})
    # A weekend run, Monday with Friday quotes, or a source outage cannot
    # relabel old recommendations as a new day's selections.
    can_select = quote_day == as_of and monday <= as_of <= friday
    if can_select:
        day = next((d for d in days if d['date'] == as_of), None)
        if day is None:
            day = {'date': as_of, 'firstRecordedAt': stamp, 'lastUpdatedAt': stamp, 'candidates': []}
            days.append(day)
        day['lastUpdatedAt'] = stamp
        seen = {e['symbol'] for e in day['candidates']}
        sectors = {}
        for e in day['candidates']:
            key = e.get('sector') or 'UNKNOWN'
            sectors[key] = sectors.get(key, 0) + 1
        for event in eligible_daily(events, config):
            if len(day['candidates']) >= config.get('maxCandidates', 10):
                break
            sector = event.get('sector') or 'UNKNOWN'
            if event['symbol'] in seen or sectors.get(sector, 0) >= config.get('maxPerSector', 2):
                continue
            day['candidates'].append(copy.deepcopy(event))
            seen.add(event['symbol'])
            sectors[sector] = sectors.get(sector, 0) + 1
    days.sort(key=lambda d: d['date'])
    current = {e['eventId']: e for e in events}
    members = {}
    for day in days:
        for original in day['candidates']:
            candidate = copy.deepcopy(current.get(original['eventId'], original))
            if original['eventId'] not in current:
                candidate.update(stale=True, status='WATCH', action='WATCH_NO_BROKER_ORDER')
            members[candidate['eventId']] = candidate
    pool = {'mode': 'WEEKLY_DAILY', 'batchId': f'LBR:WEEK:{monday}', 'weekStart': monday,
            'weekEnd': friday, 'startedOn': monday, 'nextRefreshOn': next_monday,
            'selectionVersion': config.get('parameterVersion'), 'retained': same_week,
            'days': days}
    return pool, list(members.values()), archives
