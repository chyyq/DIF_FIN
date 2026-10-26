"""Auditable descriptive learning from ALL events and human-reported fills.

Usage: python scripts/learning.py --ledger exports/trades.json
Nothing modifies strategy thresholds. Touch, confirmed, filled and actually
closed outcomes are distinct. An unfilled event never becomes a losing trade.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

try:
    from .strategy import number
    from .update_data import ROOT, load_json, write_json, parameter_hash
except ImportError:
    from strategy import number
    from update_data import ROOT, load_json, write_json, parameter_hash


def drawdown_bin(value) -> str:
    n = number(value)
    if n is None:
        return "UNKNOWN"
    for low, high in ((0, 8), (8, 10), (10, 12), (12, 14), (14, 16), (16, 18)):
        if low <= n < high:
            return f"{low}-{high}%"
    return "18%+" if n >= 18 else "UNKNOWN"


def normalize_export(payload: dict) -> dict:
    """Accept canonical exports plus the site's nested storage export envelope."""
    source = payload.get("ledger") or payload.get("state") or payload.get("data") or payload
    if not isinstance(source, dict):
        raise ValueError("Export must contain a ledger object")
    canonical = source.get("schema") == "lbr-portfolio"
    if canonical and (source.get("version") != 1 or source.get("strategyId") != "LBR_MAIN_V1"):
        raise ValueError("Unsupported portfolio schema version or strategy")
    events = []
    snapshots = {}
    for raw in source.get("events") or source.get("samples") or source.get("eventSnapshots") or []:
        if raw.get("snapshots"):
            for snapshot in raw["snapshots"]:
                snapshots[(raw.get("id") or raw.get("eventId"), snapshot.get("id"))] = snapshot.get("data", {})
            latest = sorted(raw["snapshots"], key=lambda s: s.get("recordedAt", ""))[-1]
            events.append({**latest.get("data", {}), "eventId": raw.get("id") or raw.get("eventId"),
                           "symbol": latest.get("data", {}).get("symbol") or raw.get("symbol"),
                           "snapshotRecordedAt": latest.get("recordedAt"), "snapshotCount": len(raw["snapshots"])})
        else:
            events.append(raw)
    fills = []
    for raw in source.get("fills") or source.get("trades") or source.get("transactions") or []:
        fill = dict(raw)
        if raw.get("priceCents") is not None:
            fill["price"] = raw["priceCents"] / 100
        if raw.get("feesCents") is not None:
            fill["fees"] = raw["feesCents"] / 100
        fill.setdefault("quantity", raw.get("qty"))
        fill.setdefault("timestamp", raw.get("executedAt"))
        if raw.get("side") == "BUY" and (raw.get("eventId"), raw.get("eventSnapshotId")) in snapshots:
            fill["eventSnapshot"] = snapshots[(raw["eventId"], raw["eventSnapshotId"])]
        fills.append(fill)
    return {"canonical": canonical, "fills": fills,
            "positions": source.get("positions") or [], "events": events,
            "audit": source.get("audit") or source.get("auditLog") or []}


def canonical_event_trades(ledger: dict) -> tuple[list[dict], list[str]]:
    """Replay the website's integer-cent lot allocations, attributed to buy events.

    The same symbol can hold multiple events. Sale attribution follows
    allocations.buyFillId and never the sell row's optional eventId.
    """
    book, event_book, seen = {}, {}, {}
    errors = []
    for index, fill in enumerate(sorted(ledger.get("fills", []), key=lambda f: (f.get("executedAt", ""), f.get("side") != "BUY", f.get("id", "")))):
        try:
            fid = fill["id"]
            if fid in seen:
                if seen[fid] != fill:
                    raise ValueError("Conflicting duplicate fill id")
                continue
            seen[fid] = fill
            qty, price, fees = fill["qty"], fill["priceCents"], fill["feesCents"]
            if any(not isinstance(v, int) or isinstance(v, bool) for v in (qty, price, fees)) or qty <= 0 or price <= 0 or fees < 0:
                raise ValueError("Invalid integer-cent fill")
            day = fill["tradeDate"]
            if fill["side"] == "BUY":
                eid = fill["eventId"]
                book[fid] = {**fill, "sold": 0}
                record = event_book.setdefault(eid, {"symbol": fill["symbol"], "eventIds": [eid], "openedOn": day,
                                                     "closedOn": None, "buyCostCents": 0, "netProceedsCents": 0,
                                                     "buyQty": 0, "sellQty": 0, "fillIds": set()})
                record["buyCostCents"] += price * qty + fees
                record["buyQty"] += qty
                record["openedOn"] = min(record["openedOn"], day)
                record["fillIds"].add(fid)
            elif fill["side"] == "SELL":
                allocations = fill.get("allocations") or []
                total = 0
                prepared = []
                allocated_ids = set()
                for allocation in allocations:
                    bid, amount = allocation["buyFillId"], allocation["qty"]
                    lot = book.get(bid)
                    if not isinstance(amount, int) or amount <= 0 or not lot or lot["symbol"] != fill["symbol"] or lot["tradeDate"] >= day or bid in allocated_ids:
                        raise ValueError("Invalid sell lot or T+1 violation")
                    if lot["sold"] + amount > lot["qty"]:
                        raise ValueError("Oversold lot")
                    expected_fee = lot["feesCents"] * (lot["sold"] + amount) // lot["qty"] - lot["feesCents"] * lot["sold"] // lot["qty"]
                    expected_cost = lot["priceCents"] * amount + expected_fee
                    if allocation.get("entryFeesCents") != expected_fee or allocation.get("costCents") != expected_cost:
                        raise ValueError("Modified buy-cost/fee allocation")
                    exit_fee = fees * (total + amount) // qty - fees * total // qty
                    prepared.append((lot, amount, exit_fee))
                    total += amount
                    allocated_ids.add(bid)
                if total != qty:
                    raise ValueError("Sell allocation quantity mismatch")
                # Commit after validating the entire sell, preventing partial replay.
                for lot, amount, exit_fee in prepared:
                    lot["sold"] += amount
                    record = event_book[lot["eventId"]]
                    record["sellQty"] += amount
                    record["netProceedsCents"] += price * amount - exit_fee
                    record["closedOn"] = day
                    record["fillIds"].add(fid)
            else:
                raise ValueError("Unknown fill side")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Canonical fill row {index}: {exc}; excluded")
    closed = []
    for record in event_book.values():
        if record["buyQty"] == record["sellQty"] and record["buyQty"]:
            cost, proceeds = record["buyCostCents"], record["netProceedsCents"]
            closed.append({"symbol": record["symbol"], "eventIds": record["eventIds"], "openedOn": record["openedOn"],
                           "closedOn": record["closedOn"], "buyCost": cost / 100, "sellProceeds": proceeds / 100,
                           "pnl": (proceeds - cost) / 100, "netReturnPct": round((proceeds - cost) / cost * 100, 6),
                           "fillCount": len(record["fillIds"]), "source": "ACTUAL_CANONICAL_LOT_ALLOCATIONS"})
    return closed, errors


def extract_closed_trades(ledger: dict) -> tuple[list[dict], list[str]]:
    """FIFO realized round-trips, while retaining partial exits as open samples.

    A closed round-trip starts at zero shares and ends when the actual net holding
    reaches zero. Partial exits are not mislabeled as closed winning trades.
    """
    if ledger.get("canonical"):
        return canonical_event_trades(ledger)
    errors = []
    book = {}
    closed = []
    seen = set()
    fills = sorted(ledger.get("fills", []), key=lambda f: str(f.get("timestamp") or f.get("tradeDate") or f.get("date") or ""))
    for index, raw in enumerate(fills):
        fill_id = raw.get("id") or raw.get("fillId")
        if fill_id and fill_id in seen:
            continue
        if fill_id:
            seen.add(fill_id)
        symbol = raw.get("symbol") or raw.get("code")
        side = str(raw.get("side") or raw.get("type") or "").upper()
        qty = number(raw.get("quantity", raw.get("qty")))
        price = number(raw.get("price"))
        fees = number(raw.get("fees", 0), 0)
        day = str(raw.get("tradeDate") or raw.get("date") or raw.get("timestamp") or "")[:10]
        if not symbol or side not in ("BUY", "SELL") or not qty or qty <= 0 or not price or price <= 0 or fees < 0:
            errors.append(f"Invalid fill row {index}; excluded")
            continue
        record = book.setdefault(symbol, {"quantity": 0, "buyCost": 0, "sellProceeds": 0, "buyQuantity": 0, "eventIds": [], "openedOn": None, "fills": [], "invalid": False})
        if side == "BUY":
            if record["quantity"] == 0:
                record.update({"buyCost": 0, "sellProceeds": 0, "buyQuantity": 0, "eventIds": [], "openedOn": day, "fills": [], "invalid": False})
            record["quantity"] += qty
            record["buyQuantity"] += qty
            record["buyCost"] += qty * price + fees
            event_id = raw.get("eventId") or raw.get("event_id")
            if event_id and event_id not in record["eventIds"]:
                record["eventIds"].append(event_id)
        else:
            if qty > record["quantity"] + 1e-9:
                errors.append(f"Unmatched sell row {index}; cycle excluded")
                record["invalid"] = True
                continue
            record["quantity"] -= qty
            record["sellProceeds"] += qty * price - fees
        record["fills"].append(raw)
        if side == "SELL" and abs(record["quantity"]) < 1e-9:
            if not record["invalid"] and record["buyCost"] > 0:
                pnl = record["sellProceeds"] - record["buyCost"]
                closed.append({"symbol": symbol, "eventIds": list(record["eventIds"]),
                               "openedOn": record["openedOn"], "closedOn": day,
                               "buyCost": round(record["buyCost"], 6), "sellProceeds": round(record["sellProceeds"], 6),
                               "pnl": round(pnl, 6), "netReturnPct": round(pnl / record["buyCost"] * 100, 6),
                               "fillCount": len(record["fills"]), "source": "ACTUAL_MANUAL_FILLS"})
    return closed, errors


def descriptive(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "medianNetReturnPct": None, "meanNetReturnPct": None,
                "positiveFraction": None, "worstNetReturnPct": None, "bestNetReturnPct": None}
    return {"count": len(values), "medianNetReturnPct": round(median(values), 6),
            "meanNetReturnPct": round(mean(values), 6),
            "positiveFraction": round(sum(v > 0 for v in values) / len(values), 6),
            "worstNetReturnPct": min(values), "bestNetReturnPct": max(values)}


def research_rules(records: list[dict], event_map: dict, closed: list[dict], config: dict) -> dict:
    """Executable shadow-filter candidates, selected on training observations only.

    This is rule research, not a trained predictive model. Chronological 70/30
    splitting leaves validation returns out of training selection. Symbols
    crossing the split are removed from validation to prevent adjacent-event
    leakage. These conservative diagnostics never auto-change recommendations.
    """
    dated = sorted((r for r in records if event_map[r["eventId"]].get("eventDate")),
                   key=lambda r: (event_map[r["eventId"]]["eventDate"], r["eventId"]))
    unique_days = sorted({event_map[r["eventId"]]["eventDate"] for r in dated})
    split_day = unique_days[max(1, int(len(unique_days) * .7))] if len(unique_days) >= 2 else None
    train = [r for r in dated if split_day is None or event_map[r["eventId"]]["eventDate"] < split_day]
    validation_raw = [r for r in dated if split_day is not None and event_map[r["eventId"]]["eventDate"] >= split_day]
    train_symbols = {r.get("symbol") for r in train}
    validation = [r for r in validation_raw if r.get("symbol") not in train_symbols]
    returns = {t["eventIds"][0]: t["netReturnPct"] for t in closed if len(t.get("eventIds", [])) == 1}
    def stats(rows):
        vals = sorted(returns[r["eventId"]] for r in rows if r["eventId"] in returns)
        result = {"eventCount": len(rows), **descriptive(vals)}
        result["tail10NetReturnPct"] = vals[max(0, int((len(vals) - 1) * .1))] if vals else None
        result["dateFrom"] = min((event_map[r["eventId"]].get("eventDate") for r in rows), default=None)
        result["dateTo"] = max((event_map[r["eventId"]].get("eventDate") for r in rows), default=None)
        return result
    overall_train, overall_validation = stats(train), stats(validation)
    candidates = []
    for field in ("drawdownBin", "pattern", "timing", "sector"):
        # Only training data may define or rank a proposed selector.
        for value in sorted({str(r.get(field) or "UNKNOWN") for r in train}):
            own_train = [r for r in train if str(r.get(field) or "UNKNOWN") == value]
            own_val = [r for r in validation if str(r.get(field) or "UNKNOWN") == value]
            a, b = stats(own_train), stats(own_val)
            blocked = []
            if len(event_map) < config.get("walkForwardMinimumEvents", 100):
                blocked.append("FEWER_THAN_100_UNIQUE_EVENTS")
            if len(returns) < config.get("walkForwardMinimumEvents", 100):
                blocked.append("FEWER_THAN_100_ACTUALLY_CLOSED_EVENTS")
            if a["count"] < 30:
                blocked.append("TRAINING_GROUP_FEWER_THAN_30_CLOSED_EVENTS")
            if b["count"] < 10:
                blocked.append("VALIDATION_GROUP_FEWER_THAN_10_CLOSED_EVENTS")
            if split_day is None:
                blocked.append("NO_TIME_SEPARATED_VALIDATION")
            evidence = a["medianNetReturnPct"] is not None and a["medianNetReturnPct"] > 0
            validation_positive = b["medianNetReturnPct"] is not None and b["medianNetReturnPct"] > 0
            if not evidence:
                blocked.append("NO_POSITIVE_TRAINING_MEDIAN")
            if not validation_positive:
                blocked.append("NO_POSITIVE_VALIDATION_MEDIAN")
            if any(not event_map[r["eventId"]].get("marketRegimeVerified") for r in own_train + own_val):
                blocked.append("MARKET_REGIME_COVERAGE_UNVERIFIED")
            if any(r.get("fill") and not r.get("featuresKnownAtFill") for r in own_train + own_val):
                blocked.append("FEATURES_NOT_VERIFIED_AT_FILL")
            candidates.append({"id": f"shadow:{field}:{value}",
                               "selector": {"field": field, "operator": "equals", "value": value},
                               "effect": "SHADOW_FILTER_RESEARCH_ONLY", "training": a, "validation": b,
                               "trainingEvidencePositive": evidence, "validationEvidencePositive": validation_positive,
                               "blocked": bool(blocked), "blockReasons": blocked,
                               "eligibleForShadowReview": not blocked, "autoApply": False})
    candidates.sort(key=lambda c: (c["training"]["medianNetReturnPct"] is not None,
                                    c["training"]["medianNetReturnPct"] or -1e9, c["training"]["count"]), reverse=True)
    return {"status": "BLOCKED_INSUFFICIENT_VALIDATION" if not any(c["eligibleForShadowReview"] for c in candidates) else "SHADOW_REVIEW_CANDIDATES",
            "parameterHash": parameter_hash(config), "parameterVersion": config.get("parameterVersion"),
            "method": "Chronological date split 70/30; overlapping training symbols excluded from validation",
            "training": overall_train, "validation": overall_validation, "validationSplitDate": split_day,
            "validationOverlapEventsExcluded": len(validation_raw) - len(validation),
            "candidateRules": candidates, "autoApply": False, "activeParameterChanges": [],
            "limitations": ["Observed rule filters do not establish causation or future profitability",
                            "No core threshold changes or trained ML predictor are implemented",
                            "Even eligible candidates require review, independent market-stage checks and shadow execution"]}


def analyze(events: list[dict], export: dict, config: dict) -> dict:
    ledger = normalize_export(export)
    # Latest observation per event, including all rejected/unfilled observations.
    event_map = {}
    for event in events + ledger["events"]:
        event_id = event.get("eventId") or event.get("event_id")
        if event_id:
            event_map[event_id] = {**event_map.get(event_id, {}), **event}
    independent = len(event_map)
    closed, errors = extract_closed_trades(ledger)
    fills_by_event = defaultdict(list)
    for fill in ledger["fills"]:
        event_id = fill.get("eventId") or fill.get("event_id")
        if event_id:
            fills_by_event[event_id].append(fill)
    all_records = []
    groups = defaultdict(lambda: {"events": set(), "closedTrades": []})
    for event_id, event in event_map.items():
        own_closed = [trade for trade in closed if event_id in trade["eventIds"]]
        levels = event.get("buyLevels", [])
        touched = any(level.get("touched") is True for level in levels)
        confirmed = event.get("status") == "BUY_READY" or any(level.get("status") == "BUY_READY" for level in levels)
        filled = any(str(f.get("side", f.get("type", ""))).upper() == "BUY" for f in fills_by_event[event_id])
        own_buys = sorted((f for f in fills_by_event[event_id] if str(f.get("side", f.get("type", ""))).upper() == "BUY"),
                          key=lambda f: str(f.get("timestamp") or f.get("tradeDate") or ""))
        fill_snapshot = own_buys[0].get("eventSnapshot") if own_buys else None
        feature_source = fill_snapshot if isinstance(fill_snapshot, dict) else event if not filled else {}
        b = drawdown_bin(feature_source.get("drawdownPct"))
        pattern = (feature_source.get("absorption") or {}).get("pattern") or "UNVERIFIED"
        buy_dates = [str(f.get("tradeDate") or f.get("timestamp") or "")[:10] for f in fills_by_event[event_id] if str(f.get("side", f.get("type", ""))).upper() == "BUY"]
        first_buy = min((day for day in buy_dates if day), default=None)
        timing = "T0" if first_buy and first_buy == event.get("eventDate") else "T1_T6" if first_buy and event.get("eventDate") and first_buy > event["eventDate"] else "UNFILLED" if not filled else "UNKNOWN"
        record = {"eventId": event_id, "symbol": event.get("symbol"), "status": event.get("status"),
                  "touch": touched, "confirm": confirmed, "fill": filled,
                  "outcome": "CLOSED" if own_closed else "OPEN_OR_PARTIAL" if filled else "UNFILLED",
                  "drawdownBin": b, "pattern": pattern, "timing": timing, "sector": feature_source.get("sector", "UNKNOWN"),
                  "featuresKnownAtFill": isinstance(fill_snapshot, dict),
                  "closedNetReturnPct": own_closed[0]["netReturnPct"] if len(own_closed) == 1 else None,
                  "reasonCodes": event.get("reasonCodes", [])}
        all_records.append(record)
        for key in (f"drawdown:{b}", f"pattern:{pattern}", f"timing:{timing}", f"sector:{event.get('sector', 'UNKNOWN')}"):
            groups[key]["events"].add(event_id)
            # A round trip spanning two events is excluded from event-group returns
            # so its profit cannot be counted twice or arbitrarily attributed.
            groups[key]["closedTrades"].extend(t for t in own_closed if len(t["eventIds"]) == 1)
    min_events = config.get("minimumLearningEvents", 30)
    walk_min = config.get("walkForwardMinimumEvents", 100)
    tier = "INSUFFICIENT_SAMPLE" if independent < min_events else "DESCRIPTIVE_ONLY" if independent < walk_min else "WALK_FORWARD_RESEARCH_REQUIRED"
    summaries = {}
    for key, group in groups.items():
        unique_trades = {(t["symbol"], t["openedOn"], t["closedOn"]): t for t in group["closedTrades"]}
        count = len(group["events"])
        summaries[key] = {"eventCount": count, **descriptive([t["netReturnPct"] for t in unique_trades.values()]),
                          "sampleSufficient": count >= min_events,
                          "confidence": "NOT_ESTIMATED"}
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "strategyId": config.get("strategyId", "LBR_MAIN_V1"),
            "mode": tier, "autoApply": False, "parameterChanges": [], "independentEventCount": independent,
            "closedTradeCount": len(closed), "unfilledEventCount": sum(r["outcome"] == "UNFILLED" for r in all_records),
            "missingEventAttributionCount": sum(not trade["eventIds"] for trade in closed),
            "summary": descriptive([trade["netReturnPct"] for trade in closed]), "groups": summaries,
            "events": all_records, "closedTrades": closed, "validationErrors": errors,
            "ruleResearch": research_rules(all_records, event_map, closed, config),
            "limitations": ["Manual-fill selection bias remains; rejected and unfilled events are retained",
                            "Unfilled events have no realized return and are excluded from trade win ratios",
                            "Below 30 independent events: code/data checks only; no stable win-rate inference",
                            "30-99 events: descriptive statistics only; at least 100 needs time-ordered out-of-sample validation",
                            "No automatic parameter optimization or promised improvement in winning probability"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, help="User-exported complete manual trade JSON")
    parser.add_argument("--events", type=Path, default=ROOT / "data/events.json")
    parser.add_argument("--output", type=Path, default=ROOT / "learning/reports/latest.json",
                        help="Private local report; never use the published data directory for personal trade reports")
    args = parser.parse_args()
    export = load_json(args.ledger, {}) if args.ledger else {}
    events = load_json(args.events, {"events": []})
    if isinstance(events, dict):
        events = events.get("events", [])
    config = load_json(ROOT / "config/strategy.json", {})
    result = analyze(events, export, config)
    write_json(args.output, result)
    print(json.dumps({"mode": result["mode"], "events": result["independentEventCount"], "closedTrades": result["closedTradeCount"]}))


if __name__ == "__main__":
    main()
