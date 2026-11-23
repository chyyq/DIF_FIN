"""Five rolling weekday recommendation slots, independent from the private ledger."""
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


def _saved_slots(previous, as_of):
    """Keep the newest known snapshot for each weekday, without inventing dates."""
    old = previous.get('pool', {})
    saved = copy.deepcopy(old.get('days', []))
    # Old releases without daily history prove only their actual snapshot date.
    # A dated record may be old; that is shown as retained history, not relabelled.
    legacy_day = str(previous.get('meta', {}).get('generatedAt') or '')[:10]
    if old.get('mode') != 'WEEKLY_DAILY' and legacy_day and previous.get('candidates'):
        saved.append({'date': legacy_day, 'firstRecordedAt': previous['meta']['generatedAt'],
                      'lastUpdatedAt': previous['meta']['generatedAt'], 'migrated': True,
                      'candidates': copy.deepcopy(previous.get('candidates', []))})
    slots = {}
    for entry in saved:
        try:
            day = date.fromisoformat(entry['date'])
        except (ValueError, TypeError, KeyError):
            continue
        weekday = day.weekday()
        if day.isoformat() != entry['date'] or weekday >= 5 or day.isoformat() > as_of:
            continue
        if weekday not in slots or entry['date'] > slots[weekday]['date']:
            slots[weekday] = {**entry, 'weekday': weekday}
    return slots


def build_weekly_pool(previous, events, config, as_of, quote_day=None, stamp=None):
    """Replace only today's weekday slot after a successful current-day refresh.

    days[].candidates are the original recommendation snapshots. The returned
    flat list holds their current event states, including CANCEL/EXPIRE. A new
    event for the same symbol must not rewrite an earlier recommendation. Old
    Tuesday-Friday records survive a new Monday; holidays/outages keep all slots.
    """
    monday = week_start(as_of)
    start = date.fromisoformat(monday)
    friday = (start + timedelta(days=4)).isoformat()
    next_monday = (start + timedelta(days=7)).isoformat()
    today = date.fromisoformat(as_of)
    slots = _saved_slots(previous, as_of)
    archives = []
    # A weekend run, Monday with Friday quotes, or a source outage cannot
    # relabel old recommendations as a new day's selections.
    can_select = quote_day == as_of and today.weekday() < 5
    if can_select:
        weekday = today.weekday()
        day = slots.get(weekday)
        if day is None or day['date'] != as_of:
            if day:
                archives.extend(dict(e, archiveReason='WEEKDAY_SLOT_REPLACED',
                                     recommendationDate=day['date'], replacementDate=as_of,
                                     weekday=weekday, archivedOn=as_of) for e in day['candidates'])
            day = {'date': as_of, 'weekday': weekday, 'firstRecordedAt': stamp,
                   'lastUpdatedAt': stamp, 'candidates': []}
            slots[weekday] = day
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
    # Weekday order is intentional: new Monday can precede last week's Tuesday.
    days = [slots[weekday] for weekday in range(5) if weekday in slots]
    current = {e['eventId']: e for e in events}
    members = {}
    for day in days:
        for original in day['candidates']:
            candidate = copy.deepcopy(current.get(original['eventId'], original))
            if original['eventId'] not in current:
                candidate.update(stale=True)
                if candidate.get('status') not in ('CANCEL', 'EXPIRE'):
                    candidate.update(status='WATCH', action='WATCH_NO_BROKER_ORDER')
            members[candidate['eventId']] = candidate
    next_weekday = today + timedelta(days=1)
    while next_weekday.weekday() >= 5:
        next_weekday += timedelta(days=1)
    pool = {'mode': 'WEEKLY_DAILY', 'rotation': 'WEEKDAY_ROLLING',
            'batchId': f'LBR:ROLLING:{monday}', 'weekStart': monday,
            'weekEnd': friday, 'startedOn': min((d['date'] for d in days), default=monday),
            'nextRefreshOn': next_weekday.isoformat(), 'nextMonday': next_monday,
            'selectionVersion': config.get('parameterVersion'),
            'retained': bool(previous.get('pool', {}).get('days')), 'days': days}
    return pool, list(members.values()), archives
