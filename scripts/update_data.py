"""Refresh independent LBR data. Usage: python scripts/update_data.py [--offline].

The static site records manual trades locally. The updater never reads private
browser storage or places orders. Import exported ledger JSON into learning.py.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .providers import PublicProvider, CN_TZ
    from .strategy import TradingCalendar, evaluate_event, number, rotate_pool
except ImportError:
    from providers import PublicProvider, CN_TZ
    from strategy import TradingCalendar, evaluate_event, number, rotate_pool

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_audit(path: Path, records: list[dict]):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def parameter_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def stale_snapshot(previous: dict, config: dict, now: datetime, errors: list[str], health: list[dict]) -> dict:
    snapshot = copy.deepcopy(previous)
    old_meta = snapshot.get("meta", {})
    for event in snapshot.get("candidates", []):
        if event.get("status") == "BUY_READY":
            event["status"] = "WATCH"
        event["action"] = "WATCH_NO_BROKER_ORDER"
        event["stale"] = True
        event["reasonCodes"] = list(dict.fromkeys(event.get("reasonCodes", []) + ["STALE_QUOTE"]))
    snapshot["meta"] = {**old_meta, "schemaVersion": 1, "strategyId": config["strategyId"],
                        "generatedAt": now.isoformat(), "quoteTimestamp": old_meta.get("quoteTimestamp"),
                        "lastSuccessfulAt": old_meta.get("lastSuccessfulAt") or old_meta.get("generatedAt"),
                        "tradingDate": old_meta.get("tradingDate"), "stale": True,
                        "mode": "cached-fallback" if previous.get("candidates") else "no-current-data",
                        "errors": errors, "sourceHealth": health, "automaticOrders": False}
    snapshot.setdefault("config", config)
    snapshot.setdefault("candidates", [])
    snapshot.setdefault("calendar", {"sessions": [], "verified": False, "source": "unavailable", "coverageThrough": None})
    snapshot.setdefault("pool", {"batchId": None, "startedOn": None, "sessionIndex": None, "refreshAfterSessions": config.get("candidateSessions", 7), "nextRefreshOn": None, "retained": True})
    snapshot.setdefault("stats", {"candidateCount": 0, "eventCount": 0})
    return snapshot


def build_snapshot(previous: dict, previous_events: list[dict], quotes: list[dict],
                   histories: dict, calendar: TradingCalendar, config: dict,
                   now: datetime, health: list[dict], errors: list[str]) -> tuple[dict, list[dict], list[dict]]:
    stamp = now.isoformat()
    as_of = now.date().isoformat()
    # Quotes may legitimately be from the last session on weekends. Event dates
    # always come from the provider timestamp, never the time the job was run.
    stamps = [q["quoteTimestamp"] for q in quotes if q.get("quoteTimestamp")]
    quote_day = max((s[:10] for s in stamps), default=None)
    trading_date = quote_day or (calendar.sessions[-1] if calendar.sessions else None)
    current = {q["symbol"]: q for q in quotes}
    events = {e["eventId"]: copy.deepcopy(e) for e in previous_events}
    observations = []
    for q in quotes:
        upper = number(q.get("limitUpPrice"), 0)
        high = number(q.get("high"), 0)
        # No 9.8% tolerance or guessed 10% daily limit substitutes.
        if not q.get("limitPriceVerified") or not upper or high < upper - .0000001 or not q.get("quoteTimestamp"):
            continue
        event_day = q["quoteTimestamp"][:10]
        event_id = f"{q['symbol']}:{event_day}:01"
        if event_id not in events:
            events[event_id] = {"strategyId": config["strategyId"], "eventId": event_id, "eventVersion": 1,
                                "symbol": q["symbol"], "code": q["code"], "name": q["name"],
                                "sector": q.get("sector", "UNKNOWN"), "segment": q.get("segment"),
                                "eventDate": event_day, "limitUpPrice": upper, "referencePeak": high,
                                "firstObservedAt": stamp, "firstTouchTime": None,
                                "firstTouchTimeVerified": False, "limitEventVerified": True,
                                "limitEventSource": q.get("limitPriceSource"), "amount": q.get("amount")}
            observations.append({"type": "LIMIT_EVENT_DISCOVERED", "asOf": stamp, "eventId": event_id, "input": q})
    evaluated = []
    for event in events.values():
        quote = current.get(event["symbol"])
        if quote:
            # Preserve event-date limit price; today's limit is a different anchor.
            out = evaluate_event(event, quote, histories.get(event["symbol"], []), calendar, config, as_of, now=now)
        else:
            out = {**event, "stale": True, "status": "WATCH", "action": "WATCH_NO_BROKER_ORDER",
                   "reasonCodes": list(dict.fromkeys(event.get("reasonCodes", []) + ["QUOTE_MISSING"]))}
        evaluated.append(out)
        observations.append({"type": "EVENT_EVALUATED", "asOf": stamp, "eventId": out["eventId"],
                             "parameterHash": parameter_hash(config), "snapshot": out})
    # Superseded events remain in the audit and research universe.
    latest_by_symbol = {}
    for event in sorted(evaluated, key=lambda e: e["eventDate"]):
        latest_by_symbol[event["symbol"]] = event
    for event in evaluated:
        newest = latest_by_symbol[event["symbol"]]
        if event["eventId"] != newest["eventId"] and event.get("status") != "EXPIRE":
            event["status"] = "CANCEL"
            event["action"] = "CANCEL_PENDING_BUY"
            event["reasonCodes"] = list(dict.fromkeys(event.get("reasonCodes", []) + ["SUPERSEDED_LIMIT_EVENT"]))
    pool, candidates, archives = rotate_pool(previous, evaluated, calendar, config, trading_date or as_of)
    # Within a batch, membership is stable by symbol; a fresh event may reanchor
    # that same stock only after invalidating its old plan version.
    for index, candidate in enumerate(candidates):
        newest = latest_by_symbol.get(candidate["symbol"])
        if newest and newest["eventId"] != candidate["eventId"] and newest.get("status") not in ("CANCEL", "EXPIRE"):
            candidates[index] = {**newest, "supersededEventId": candidate["eventId"]}
    observations.extend({"type": "POOL_EVENT_ARCHIVED", "asOf": stamp, "snapshot": item} for item in archives)
    snapshot = {"meta": {"schemaVersion": 1, "strategyId": config["strategyId"], "generatedAt": stamp,
                          "lastSuccessfulAt": stamp, "quoteTimestamp": max(stamps) if stamps else None,
                          "tradingDate": trading_date, "stale": False, "mode": "public-delayed-manual-monitor",
                          "sourceHealth": health, "errors": errors, "automaticOrders": False,
                          "calendarSource": calendar.source, "parameterHash": parameter_hash(config),
                          "parameterVersion": config.get("parameterVersion"),
                          "dataLimits": ["Public quotes can be delayed", "No broker/announcement/orderbook synchronization", "No verified historical intraday baseline: WATCH only", "Future deadlines remain unknown outside observed/verified calendar"]},
                "config": config, "calendar": calendar.as_dict(), "pool": pool, "candidates": candidates,
                "stats": {"candidateCount": len(candidates), "eventCount": len(evaluated), "quoteCount": len(quotes),
                          "watchCount": sum(e.get("status") == "WATCH" for e in candidates),
                          "readyCount": sum(e.get("status") == "BUY_READY" for e in candidates)},
                "learning": {"mode": "DESCRIPTIVE_ONLY", "autoApply": False,
                             "message": "Export all manual fills and unfilled events for offline analysis; no win-rate uplift is claimed."}}
    return snapshot, evaluated, observations


def run(root: Path = ROOT, *, offline: bool = False, now: datetime | None = None) -> int:
    now = now or datetime.now(CN_TZ)
    config = load_json(root / "config/strategy.json", {})
    if not config.get("strategyId"):
        raise ValueError("Missing strategy configuration")
    data = root / "data"
    previous = load_json(data / "latest.json", {})
    old_quotes = load_json(data / "quotes.json", {"quotes": {}})
    events = load_json(data / "events.json", {"events": []}).get("events", [])
    provider = PublicProvider(config)
    quotes = []
    sessions, calendar_source = [], "unavailable"
    histories = {}
    if not offline:
        # Fetch a calendar before spending the remaining budget on universe/enrichment.
        sessions, calendar_source = provider.calendar()
        quotes = provider.universe()
        previous_symbols = sorted(set(old_quotes.get("quotes", {})) | {e["symbol"] for e in events})
        if not quotes and previous_symbols:
            fallback = provider.tencent_quotes(previous_symbols)
            old_map = old_quotes.get("quotes", {})
            quotes = [{**old_map.get(q["symbol"], {}), **q} for q in fallback]
    quotes = [q for q in quotes if q.get("quoteTimestamp") and q["quoteTimestamp"][:10] <= now.date().isoformat()]
    if not quotes:
        errors = provider.errors or (["Offline mode: current quotes were not fetched"] if offline else ["All current quote sources failed"])
        snapshot = stale_snapshot(previous, config, now, errors, list(provider.health.values()))
        write_json(data / "latest.json", snapshot)
        quote_snapshot = copy.deepcopy(old_quotes)
        quote_snapshot["meta"] = {**quote_snapshot.get("meta", {}), "generatedAt": now.isoformat(), "stale": True, "errors": errors}
        for q in quote_snapshot.get("quotes", {}).values():
            q["stale"] = True
        write_json(data / "quotes.json", quote_snapshot)
        append_audit(data / "audit.jsonl", [{"type": "REFRESH_FAILED", "asOf": now.isoformat(), "errors": errors, "retained": True}])
        print(json.dumps({"mode": snapshot["meta"]["mode"], "stale": True, "candidates": len(snapshot["candidates"])}))
        return 0 if offline else 2
    cached_calendar = previous.get("calendar", {})
    sessions = sorted(set(sessions) | set(cached_calendar.get("sessions", [])))
    explicit = load_json(root / "config/trading-calendar.json", {})
    if explicit.get("verified") is True and explicit.get("source") and explicit.get("sessions"):
        sessions = sorted(set(sessions) | set(explicit["sessions"]))
        calendar_source += "; " + explicit["source"]
    # Exchange timestamps from actual trades can extend observed sessions even
    # while an index history endpoint has not published today's daily bar yet.
    traded_dates = {q["quoteTimestamp"][:10] for q in quotes if q.get("quoteTimestamp") and number(q.get("amount"), 0) > 0}
    sessions = sorted(set(sessions) | traded_dates)
    calendar = TradingCalendar(sessions, calendar_source, bool(sessions))
    known_symbols = {e["symbol"] for e in events if e.get("status") not in ("CANCEL", "EXPIRE")
                     and calendar.index(e.get("eventDate", ""), now.date().isoformat()) is not None
                     and calendar.index(e.get("eventDate", ""), now.date().isoformat()) < config.get("candidateSessions", 7)}
    # A broad prefilter only reduces detail requests. It never verifies a touch.
    probes = [q for q in quotes if q["symbol"] in known_symbols or (q.get("segment") in config.get("allowedSegments", []) and number(q.get("preClose"), 0) > 0 and number(q.get("high"), 0) / q["preClose"] >= 1.08)]
    probes.sort(key=lambda q: (q["symbol"] in known_symbols, number(q.get("pct"), -999)), reverse=True)
    enriched = provider.enrich_many(probes[:100]) if provider.remaining() > 8 else {}
    qmap = {q["symbol"]: q for q in quotes}
    for symbol, (quote, history) in enriched.items():
        qmap[symbol] = quote
        histories[symbol] = history
    # For active events outside this run's history budget, retain a known historic
    # cache; failures never transform missing history into a passing trend gate.
    cached_history = load_json(data / "history-cache.json", {})
    for symbol in known_symbols:
        if not histories.get(symbol):
            histories[symbol] = cached_history.get(symbol, [])
    for symbol, rows in histories.items():
        if rows:
            cached_history[symbol] = rows
    quotes = list(qmap.values())
    quote_stamp = max((q["quoteTimestamp"] for q in quotes if q.get("quoteTimestamp")), default=None)
    latest_known_session = max((day for day in calendar.sessions if day <= now.date().isoformat()), default=None)
    for q in quotes:
        day = str(q.get("quoteTimestamp") or "")[:10]
        q["stale"] = not day or bool(latest_known_session and day < latest_known_session)
    snapshot, all_events, audit = build_snapshot(previous, events, quotes, histories, calendar, config, now, list(provider.health.values()), provider.errors)
    source_stamp = snapshot["meta"]["quoteTimestamp"]
    for q in quotes:
        q["stale"] = q.get("stale", False) or not bool(q.get("quoteTimestamp")) or bool(source_stamp and q["quoteTimestamp"][:10] < source_stamp[:10])
    if all(q.get("stale") for q in quotes):
        snapshot["meta"]["stale"] = True
        snapshot["meta"]["mode"] = "stale-source-snapshot"
    quote_fields = ("symbol", "code", "name", "price", "high", "low", "open", "preClose", "pct", "amount", "quoteTimestamp", "source", "stale", "segment", "limitUpPrice", "limitDownPrice")
    quote_map = {q["symbol"]: {key: q.get(key) for key in quote_fields if key in q} for q in quotes}
    # Missing symbols retain their price and original timestamp, visibly stale.
    for symbol, old in old_quotes.get("quotes", {}).items():
        quote_map.setdefault(symbol, {**old, "stale": True})
    write_json(data / "quotes.json", {"meta": {"schemaVersion": 1, "generatedAt": now.isoformat(), "quoteTimestamp": source_stamp,
                                               "tradingDate": snapshot["meta"]["tradingDate"], "stale": snapshot["meta"]["stale"],
                                               "source": "Eastmoney with Tencent cache-symbol fallback", "quoteCount": len(quote_map),
                                               "partial": bool(provider.errors)}, "quotes": quote_map})
    write_json(data / "events.json", {"strategyId": config["strategyId"], "generatedAt": now.isoformat(), "events": all_events})
    write_json(data / "history-cache.json", cached_history)
    write_json(data / "latest.json", snapshot)
    hash_value = parameter_hash(config)
    audits = load_json(data / "parameter-audit.json", {"versions": []})
    if not any(v.get("hash") == hash_value for v in audits["versions"]):
        audits["versions"].append({"hash": hash_value, "effectiveAt": now.isoformat(), "config": config,
                                   "trainingRange": None, "validation": None, "autoOptimized": False})
        write_json(data / "parameter-audit.json", audits)
    append_audit(data / "audit.jsonl", audit + [{"type": "REFRESH_COMPLETE", "asOf": now.isoformat(), "parameterHash": hash_value, "eventCount": len(all_events), "quoteCount": len(quotes)}])
    print(json.dumps({"mode": snapshot["meta"]["mode"], "stale": False, "candidates": len(snapshot["candidates"]), "events": len(all_events), "quotes": len(quotes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Exercise safe cached fallback without network access")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        sys.exit(run(args.root.resolve(), offline=args.offline))
    except Exception as exc:
        message = f"LBR refresh aborted: {type(exc).__name__}: {exc}"
        try:
            root = args.root.resolve()
            now = datetime.now(CN_TZ)
            config = {"strategyId": "LBR_MAIN_V1", **load_json(root / "config/strategy.json", {})}
            prior = load_json(root / "data/latest.json", {})
            write_json(root / "data/latest.json", stale_snapshot(prior, config, now, [message], []))
            old = load_json(root / "data/quotes.json", {"quotes": {}})
            old["meta"] = {**old.get("meta", {}), "stale": True, "errors": [message]}
            for item in old.get("quotes", {}).values():
                item["stale"] = True
            write_json(root / "data/quotes.json", old)
            append_audit(root / "data/audit.jsonl", [{"type": "REFRESH_ABORTED", "asOf": now.isoformat(), "error": message, "retained": True}])
        except Exception as recovery_error:
            print(f"Unable to mark cached data stale: {type(recovery_error).__name__}", file=sys.stderr)
        print(message, file=sys.stderr)
        sys.exit(2)
