"""Deterministic, as-of LBR calculations. No broker requests are implemented.

Money is calculated with Decimal and rounded down to the configured tick.
Calendar operations fail closed outside an explicitly observed/verified calendar.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from statistics import median
from typing import Any


def dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Expected a finite decimal number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite decimal number")
    return result


def number(value: Any, default: Any = None) -> float | None:
    try:
        return float(dec(value))
    except ValueError:
        return default


def tick_down(value: Any, tick: Any = "0.01") -> float:
    step = dec(tick)
    if step <= 0:
        raise ValueError("Tick must be positive")
    return float((dec(value) / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def pct_change(price: Any, base: Any) -> float | None:
    if number(base, 0) <= 0:
        return None
    return round(float((dec(price) / dec(base) - 1) * 100), 6)


def plan_levels(peak: Any, config: dict, *, support: Any = None,
                atr: Any = None, limit_down: Any = None) -> list[dict]:
    h = dec(peak)
    if h <= 0:
        raise ValueError("Peak must be positive")
    tick = config.get("tick", 0.01)
    levels = []
    for name, drawdown, cap_peak in (
        ("A", config.get("triggerA", .11), config.get("entryCapPeakA", .905)),
        ("B", config.get("triggerB", .13), config.get("entryCapPeakB", .885)),
    ):
        touch = dec(tick_down(h * (1 - dec(drawdown)), tick))
        cap = dec(tick_down(min(touch * (1 + dec(config.get("entryCapBuffer", .015))), h * dec(cap_peak)), tick))
        tp1 = tick_down(touch + dec(config.get("tp1Fraction", .382)) * (h - touch), tick)
        tp2 = tick_down(touch + dec(config.get("tp2Fraction", .618)) * (h - touch), tick)
        stop = stop_reference(support, touch, atr, tick)
        rr = reward_risk(cap, tp1, tp2, stop, tick, config.get("sellSplit", [.5, .5]))
        reachable = limit_down is None or touch >= dec(limit_down)
        levels.append({
            "id": name, "drawdownPct": float(dec(drawdown) * 100),
            "triggerPrice": float(touch), "maxBuyPrice": float(cap),
            "tp1": tp1, "tp2": tp2,
            "tp1Pct": pct_change(tp1, cap), "tp2Pct": pct_change(tp2, cap),
            "plannedStop": stop, "rewardRisk": rr,
            "status": "WAIT_CONFIRM" if reachable else "UNREACHABLE_TODAY",
            "reachableToday": reachable, "sellSplit": config.get("sellSplit", [.5, .5]),
        })
    return levels


def stop_reference(support: Any, pivot_low: Any, atr: Any,
                   tick: Any = .01) -> float | None:
    if support is None or atr is None or number(support, 0) <= 0 or number(atr, -1) < 0 or number(pivot_low, 0) <= 0:
        return None
    low = dec(pivot_low)
    raw = min(dec(support), low) - max(dec("0.5") * dec(atr), dec("0.01") * low)
    return tick_down(raw, tick) if raw > 0 else None


def reward_risk(entry: Any, tp1: Any, tp2: Any, stop: Any,
                tick: Any = .01, split: list | None = None) -> float | None:
    if stop is None or dec(entry) <= dec(stop):
        return None
    portions = split or [.5, .5]
    reward = dec(portions[0]) * (dec(tp1) - dec(entry)) + dec(portions[1]) * (dec(tp2) - dec(entry))
    return round(float(reward / max(dec(entry) - dec(stop), dec(tick))), 6)


class TradingCalendar:
    def __init__(self, sessions: list[str], source: str = "unknown", verified: bool = False):
        valid = []
        for item in sessions:
            parsed = date.fromisoformat(item).isoformat()
            valid.append(parsed)
        self.sessions = sorted(set(valid))
        self.source = source
        self.verified = bool(verified)

    def index(self, start: str, as_of: str) -> int | None:
        """Zero-based session index, allowing a weekend as-of after the last session."""
        if start not in self.sessions or as_of < start:
            return None
        # Only count observed/verified sessions. The caller exposes coverageThrough.
        return sum(start < day <= as_of for day in self.sessions)

    def deadline(self, start: str, length: int) -> str | None:
        if length < 1:
            raise ValueError("Session length must be positive")
        if start not in self.sessions:
            return None
        offset = self.sessions.index(start) + length - 1
        return self.sessions[offset] if offset < len(self.sessions) else None

    def next_session(self, current: str) -> str | None:
        return next((day for day in self.sessions if day > current), None)

    def as_dict(self) -> dict:
        return {"sessions": self.sessions, "source": self.source, "verified": self.verified,
                "coverageThrough": self.sessions[-1] if self.sessions else None,
                "futureDatesEstimated": False}


def trend_metrics(rows: list[dict], event_date: str) -> dict:
    """Pre-event completed closes only: today's high never leaks into the trend gate."""
    prior = sorted((r for r in rows if r.get("date", "") < event_date and number(r.get("close"), 0) > 0), key=lambda r: r["date"])
    closes = [float(r["close"]) for r in prior]
    out = {"historySessions": len(prior), "available": len(prior) >= 65,
           "passed": False, "ma20": None, "ma60": None, "atr14": None,
           "support": None, "score": None}
    if len(closes) < 65:
        return out
    mean = lambda seq: sum(seq) / len(seq)
    ma20, ma60 = mean(closes[-20:]), mean(closes[-60:])
    old20 = mean(closes[-25:-5])
    lows = [number(r.get("low")) for r in prior[-20:]]
    valid_lows = [v for v in lows if v and v > 0]
    tr = []
    for old, current in zip(prior[-15:-1], prior[-14:]):
        if all(number(current.get(k), 0) > 0 for k in ("high", "low")):
            tr.append(max(float(current["high"]) - float(current["low"]), abs(float(current["high"]) - float(old["close"])), abs(float(current["low"]) - float(old["close"]))))
    rising_lows = len(valid_lows) == 20 and min(valid_lows[-10:]) >= min(valid_lows[:10])
    checks = [ma20 > old20, ma20 > ma60, rising_lows]
    out.update({"passed": sum(checks) >= 2, "ma20": round(ma20, 6), "ma60": round(ma60, 6),
                "ma20Slope": (ma20 / old20 - 1) if old20 else None,
                "atr14": round(mean(tr), 6) if len(tr) == 14 else None,
                "support": min(valid_lows) if valid_lows else None,
                "score": round(100 * sum(checks) / 3, 2), "checks": checks})
    return out


def absorption_metrics(bars: list[dict], config: dict, sector_ok: bool | None = None) -> dict:
    """Use completed, timestamped five-minute bars with genuine same-slot RVOL.

    `rvol` must come from 20 historical same-slot medians, not a daily volume ratio.
    Missing baseline and orderbook liquidity are never silently manufactured.
    """
    complete = [b for b in bars if b.get("complete") is True]
    out = {"available": False, "score": None, "confirmed": False,
           "reclaimed": False, "pattern": None, "reason": "MISSING_5MIN_BASELINES"}
    if len(complete) < 5 or any(number(b.get(k)) is None for b in complete[-5:] for k in ("open", "close", "low", "amount", "rvol")):
        return out
    if any(b.get("baselineSessions", 0) < 20 for b in complete[-5:]):
        return out
    latest = complete[-3:]
    if any(number(b.get("amount"), 0) <= 0 for b in complete[-5:]):
        return out
    tick = float(config.get("tick", .01))
    prior_low = min(float(b["low"]) for b in complete[:-3])
    no_low = all(float(b["low"]) >= prior_low - tick - 1e-9 for b in latest)
    higher = float(latest[-1]["low"]) >= float(latest[0]["low"]) - tick - 1e-9
    panic_candidates = [b for b in complete[:-2] if float(b["rvol"]) >= config.get("panicRvolMinimum", 1.8) and float(b["close"]) < float(b["open"])]
    panic = max(panic_candidates, key=lambda b: float(b["amount"]), default=None)
    recent_amount = sum(float(b["amount"]) for b in complete[-2:]) / 2
    contraction = bool(panic and recent_amount / float(panic["amount"]) <= config.get("shrinkThreshold", .65))
    orderly = all(float(b["rvol"]) < config.get("shrinkThreshold", .65) for b in complete[-2:])
    impact = lambda b: abs(float(b["close"]) / float(b["open"]) - 1) / float(b["amount"]) if float(b["open"]) > 0 else float("inf")
    early_impact = impact(panic) if panic else max(impact(b) for b in complete[:-2])
    late_impact = sum(impact(b) for b in complete[-2:]) / 2
    decay = early_impact > 0 and late_impact <= early_impact * .7
    reclaim = all(number(b.get("vwap"), 0) > 0 and float(b["close"]) >= float(b["vwap"]) for b in complete[-2:])
    rebound_break = bool(complete[-1].get("brokeConfirmedReboundHigh"))
    score = 20 * no_low + 20 * decay + 20 * (contraction or orderly) + 15 * higher + 15 * (reclaim or rebound_break) + 10 * (sector_ok is True)
    out.update({"available": True, "score": score, "confirmed": score >= config.get("absorptionScoreMin", 70),
                "reclaimed": reclaim or rebound_break, "pattern": "PANIC_ABSORPTION" if contraction else "ORDERLY_PULLBACK" if orderly else "UNCONFIRMED",
                "reason": None, "noNewLow": no_low, "higherLow": higher,
                "contraction": contraction or orderly, "impactDecay": decay,
                "signalTimestamp": complete[-1].get("end")})
    return out


def hard_veto(quote: dict, config: dict) -> list[str]:
    reasons = []
    if not re.fullmatch(r"\d{6}", str(quote.get("code", ""))):
        reasons.append("INVALID_SECURITY_CODE")
    segment = quote.get("segment")
    if segment not in config.get("allowedSegments", ["SSE_MAIN", "SZSE_MAIN"]):
        reasons.append("EXCLUDED_SEGMENT")
    # Prefix is a secondary integrity check, never the source of segment metadata.
    if str(quote.get("code", "")).startswith(("30", "68", "4", "8", "92")):
        reasons.append("EXCLUDED_BOARD_CODE")
    if config.get("excludeST", True) and (quote.get("isST") is True or "ST" in str(quote.get("name", "")).upper() or "退" in str(quote.get("name", ""))):
        reasons.append("SPECIAL_RISK_SECURITY")
    for key, reason in (("suspended", "SUSPENDED"), ("noLimitDay", "NO_LIMIT_DAY"),
                        ("majorNegative", "MAJOR_NEGATIVE"), ("structureBroken", "STRUCTURE_BROKEN"),
                        ("marketHalt", "MARKET_RISK_HALT"), ("otherStrategyOccupied", "OTHER_STRATEGY_OCCUPIED"),
                        ("corporateActionChanged", "ANCHOR_RECALC_REQUIRED")):
        if quote.get(key) is True:
            reasons.append(reason)
    if number(quote.get("price"), 0) <= 0:
        reasons.append("INVALID_QUOTE")
    return reasons


def evaluate_event(event: dict, quote: dict, history: list[dict], calendar: TradingCalendar,
                   config: dict, as_of: str, bars: list[dict] | None = None,
                   now: datetime | None = None) -> dict:
    result = copy.deepcopy(event)
    result.update({k: quote.get(k) for k in ("price", "high", "low", "quoteTimestamp", "limitDownPrice") if quote.get(k) is not None})
    result["eventDayIndex"] = calendar.index(result["eventDate"], as_of)
    result["expiresOn"] = calendar.deadline(result["eventDate"], config.get("candidateSessions", 7))
    observed_peak = max(number(event.get("referencePeak"), 0), number(quote.get("high"), 0), number(quote.get("price"), 0))
    anchor_changed = observed_peak > number(event.get("referencePeak"), 0) + .0000001
    if anchor_changed:
        result["eventVersion"] = int(event.get("eventVersion", 1)) + 1
        result["reanchoredFrom"] = event.get("referencePeak")
        result["pendingOrdersInvalidated"] = True
    result["referencePeak"] = observed_peak
    price = number(quote.get("price"), 0)
    result["drawdownPct"] = round((1 - price / observed_peak) * 100, 6) if observed_peak and price else None
    trend = trend_metrics(history, result["eventDate"])
    absorption = absorption_metrics(bars or [], config, quote.get("sectorVerifiedStable"))
    result["trendScore"] = trend.get("score")
    result["metrics"] = trend
    result["absorption"] = absorption
    result["buyLevels"] = plan_levels(observed_peak, config, support=trend.get("support"), atr=trend.get("atr14"), limit_down=quote.get("limitDownPrice")) if observed_peak else []
    vetoes = hard_veto(quote, config)
    if event.get("symbol") != quote.get("symbol"):
        vetoes.append("INSTRUMENT_MISMATCH")
    if event.get("segment") not in config.get("allowedSegments", []) or event.get("segment") != quote.get("segment"):
        vetoes.append("EVENT_SEGMENT_UNVERIFIED_OR_CHANGED")
    reasons = list(vetoes)
    age = result["eventDayIndex"]
    if age is None:
        reasons.append("CALENDAR_UNAVAILABLE")
    if age is not None and age >= config.get("candidateSessions", 7):
        reasons.append("EVENT_EXPIRED")
    if trend["historySessions"] < config.get("minimumHistorySessions", 120):
        reasons.append("INSUFFICIENT_HISTORY")
    if trend["available"] and not trend["passed"]:
        vetoes.append("TREND_FAILED")
        reasons.append("TREND_FAILED")
    elif not trend["available"]:
        reasons.append("TREND_UNAVAILABLE")
    event_verified = event.get("limitEventVerified") is True and number(event.get("limitUpPrice"), 0) > 0
    if not event_verified:
        reasons.append("LIMIT_PRICE_UNVERIFIED")
    if (quote.get("limitPriceVerified") is not True or number(quote.get("limitUpPrice"), 0) <= 0
            or number(quote.get("limitDownPrice"), 0) <= 0):
        reasons.append("CURRENT_DAILY_LIMITS_UNVERIFIED")
    if now:
        try:
            quote_time = datetime.fromisoformat(str(quote.get("quoteTimestamp", "")).replace("Z", "+00:00"))
            fresh_quote = quote_time.tzinfo is not None and 0 <= (now - quote_time).total_seconds() <= config.get("signalTtlSeconds", 180)
        except (ValueError, TypeError):
            fresh_quote = False
        if not fresh_quote:
            reasons.append("QUOTE_NOT_FRESH_FOR_ENTRY")
    else:
        reasons.append("DECISION_TIMESTAMP_UNAVAILABLE")
    for key, reason in (("securityMetadataVerified", "SECURITY_METADATA_UNVERIFIED"),
                        ("announcementsVerified", "ANNOUNCEMENTS_UNVERIFIED"),
                        ("sectorVerifiedStable", "SECTOR_UNVERIFIED"),
                        ("structureVerified", "STRUCTURE_UNVERIFIED"),
                        ("accountStateSynced", "ACCOUNT_STATE_UNSYNCED")):
        if quote.get(key) is not True:
            reasons.append(reason)
    if not absorption["available"]:
        reasons.append("INTRADAY_CONFIRMATION_UNAVAILABLE")
    if number(quote.get("amount"), 0) < config.get("minimumAmount", 100_000_000):
        reasons.append("LIQUIDITY_FLOOR_FAILED")
    if quote.get("stale") is True:
        reasons.append("STALE_QUOTE")
    dd = result["drawdownPct"]
    if dd is not None and dd > 16:
        reasons.append("EXTREME_DRAWDOWN_NO_NEW_ENTRY")
    result["riskLevel"] = "EXTREME" if dd is not None and dd > 16 else "DEEP" if dd is not None and dd >= 14 else "NORMAL"
    gates = {"universe": not bool(vetoes), "trend": trend["passed"], "limitEvent": event_verified,
             "structure": quote.get("structureVerified") is True and not quote.get("structureBroken", False),
             "absorption": absorption["available"] and absorption["confirmed"], "payoff": False}
    for level in result["buyLevels"]:
        previous = next((x for x in event.get("buyLevels", []) if x.get("id") == level["id"]), {})
        quote_day = str(quote.get("quoteTimestamp") or "")[:10]
        valid_day_low = quote_day > result["eventDate"] and number(quote.get("low")) is not None
        verified_event_low = quote.get("eventLowVerified") is True and number(quote.get("eventLowAsOf")) is not None
        # T0 daily low can precede the first limit touch. It proves no post-touch
        # drawdown unless timestamped event-relative data explicitly verifies it.
        touched = (bool(previous.get("touched")) and not anchor_changed) or bool(price and price <= level["triggerPrice"])
        touched = touched or (valid_day_low and float(quote["low"]) <= level["triggerPrice"])
        touched = touched or (verified_event_low and float(quote["eventLowAsOf"]) <= level["triggerPrice"])
        level["touched"] = touched
        level["payoffPassed"] = level["rewardRisk"] is not None and level["rewardRisk"] >= config.get("minimumRewardRisk", 1.5)
        gates["payoff"] = gates["payoff"] or level["payoffPassed"]
    signal_fresh = False
    if now and absorption.get("signalTimestamp"):
        try:
            stamp = datetime.fromisoformat(absorption["signalTimestamp"].replace("Z", "+00:00"))
            signal_fresh = stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= config.get("signalTtlSeconds", 180)
        except (ValueError, TypeError):
            pass
    if absorption["available"] and not signal_fresh:
        reasons.append("SIGNAL_STALE_OR_UNTIMESTAMPED")
    eligible = []
    for level in result["buyLevels"]:
        score_min = config.get("deepAbsorptionScoreMin", 80) if result["riskLevel"] == "DEEP" else config.get("absorptionScoreMin", 70)
        if (level["touched"] and level["reachableToday"] and price and price <= level["maxBuyPrice"]
                and level["payoffPassed"] and absorption["available"] and absorption["score"] >= score_min and absorption["reclaimed"]):
            eligible.append(level)
    if not gates["payoff"]:
        reasons.append("PAYOFF_UNAVAILABLE_OR_INSUFFICIENT")
    result["gates"] = gates
    result["hardVetoReasons"] = vetoes
    result["reasonCodes"] = list(dict.fromkeys(reasons))
    result["status"] = "EXPIRE" if "EVENT_EXPIRED" in reasons else "CANCEL" if vetoes else "WATCH"
    # Public delayed providers lack mandatory metadata/account/intraday, so remain WATCH.
    if not reasons and all(gates.values()) and eligible and signal_fresh:
        result["status"] = "BUY_READY"
        for level in eligible:
            level["status"] = "BUY_READY"
    result["action"] = {"WATCH": "WATCH_NO_BROKER_ORDER", "BUY_READY": "MANUAL_CONFIRM_REQUIRED", "CANCEL": "CANCEL_PENDING_BUY", "EXPIRE": "EXPIRE_UNFILLED"}[result["status"]]
    result["compositeScore"] = None  # Never invent missing board/orderbook components.
    result["entryMode"] = "MANUAL_RECORD_ONLY"
    return result


def rotate_pool(previous: dict, events: list[dict], calendar: TradingCalendar,
                config: dict, as_of: str) -> tuple[dict, list[dict], list[dict]]:
    """Freeze membership for seven sessions; prices and versions still update daily."""
    old_pool = previous.get("pool", {})
    age = calendar.index(old_pool.get("startedOn", ""), as_of)
    # An empty initial pool must not lock the site empty for the next seven days.
    due = not previous.get("candidates") or not old_pool.get("batchId") or (age is not None and age >= config.get("candidateSessions", 7))
    candidates = []
    archives = []
    if due:
        archives = [{**e, "batchId": old_pool.get("batchId"), "archiveReason": "BATCH_ROTATED", "archivedOn": as_of} for e in previous.get("candidates", [])]
        active = [e for e in events if e.get("eventDayIndex") is not None and e["eventDayIndex"] < config.get("candidateSessions", 7) and e.get("status") not in ("CANCEL", "EXPIRE")]
        active.sort(key=lambda e: (e.get("eventDate", ""), e.get("trendScore") or 0, e.get("amount") or 0), reverse=True)
        seen = set()
        sectors = {}
        for event in active:
            if event["symbol"] in seen:
                continue
            sector = event.get("sector") or "UNKNOWN"
            # Unknown sectors share one bucket, conservatively cap concentration.
            if sectors.get(sector, 0) >= config.get("maxPerSector", 2):
                continue
            candidates.append(event)
            seen.add(event["symbol"])
            sectors[sector] = sectors.get(sector, 0) + 1
            if len(candidates) >= config.get("maxCandidates", 10):
                break
        started = as_of if as_of in calendar.sessions else (calendar.sessions[-1] if calendar.sessions else None)
        pool = {"batchId": f"LBR:{started or as_of}", "startedOn": started, "sessionIndex": 0 if started else None,
                "refreshAfterSessions": config.get("candidateSessions", 7), "retained": False,
                "nextRefreshOn": calendar.deadline(started, config.get("candidateSessions", 7) + 1) if started else None}
    else:
        by_id = {e["eventId"]: e for e in events}
        candidates = [by_id.get(e["eventId"], e) for e in previous.get("candidates", [])]
        pool = {**old_pool, "sessionIndex": age, "retained": True}
    return pool, candidates, archives


def freeze_fill_targets(peak: Any, pivot_low: Any, entry: Any,
                        support: Any, atr: Any, config: dict) -> dict:
    """Known-at-fill anchors are immutable; additions retain per-lot targets."""
    h, low, e = dec(peak), dec(pivot_low), dec(entry)
    if h <= 0 or low <= 0 or e <= 0 or low > h:
        raise ValueError("Invalid fill anchors")
    tick = config.get("tick", .01)
    tp1 = tick_down(low + dec(config.get("tp1Fraction", .382)) * (h - low), tick)
    tp2 = tick_down(low + dec(config.get("tp2Fraction", .618)) * (h - low), tick)
    stop = stop_reference(support, low, atr, tick)
    rr = reward_risk(e, tp1, tp2, stop, tick, config.get("sellSplit"))
    return {"entryPeak": float(h), "entryPivotLow": float(low), "entryPrice": float(e),
            "tp1": tp1, "tp2": tp2, "stop": stop, "rewardRisk": rr,
            "reviewRequired": tp1 <= float(e) or rr is None or rr < config.get("minimumRewardRisk", 1.5)}


def manual_fill(ledger: dict, fill: dict, calendar: TradingCalendar, config: dict) -> dict:
    """Record an actual human-reported fill, merge symbol rows, and preserve lot truth."""
    out = copy.deepcopy(ledger)
    fills = out.setdefault("fills", [])
    if not fill.get("id"):
        raise ValueError("Fill id required for idempotency")
    existing = next((item for item in fills if item.get("id") == fill["id"]), None)
    if existing is not None:
        if existing != fill:
            raise ValueError("Conflicting duplicate fill id")
        return out
    if fill.get("side") not in ("BUY", "SELL"):
        raise ValueError("Fill side must be BUY or SELL")
    quantity, price = dec(fill.get("quantity")), dec(fill.get("price"))
    if quantity <= 0 or quantity != quantity.to_integral_value() or price <= 0 or dec(fill.get("fees", 0)) < 0:
        raise ValueError("Actual positive integer quantity and price required")
    day = fill.get("tradeDate")
    if day not in calendar.sessions:
        raise ValueError("Fill date must be a verified/observed trading session")
    if not fill.get("symbol"):
        raise ValueError("Symbol required")
    positions = out.setdefault("positions", [])
    matching = [p for p in positions if p["symbol"] == fill["symbol"] and p.get("quantity", 0) > 0]
    if len(matching) > 1:
        raise ValueError("Existing ledger has duplicate open symbol positions")
    position = matching[0] if matching else None
    if fill["side"] == "BUY":
        if fill.get("entryPeak") is not None and fill.get("entryPivotLow") is not None:
            anchors = freeze_fill_targets(fill["entryPeak"], fill["entryPivotLow"], price, fill.get("support"), fill.get("atr14"), config)
        else:
            # Actual trade truth is kept even when research anchors are missing.
            anchors = {"entryPeak": number(fill.get("entryPeak")), "entryPivotLow": number(fill.get("entryPivotLow")),
                       "entryPrice": float(price), "tp1": None, "tp2": None, "stop": None,
                       "rewardRisk": None, "reviewRequired": True, "reason": "FILL_ANCHORS_UNVERIFIED"}
        if position is None:
            position = {"symbol": fill["symbol"], "name": fill.get("name", fill["symbol"]), "quantity": 0,
                        "cost": 0, "firstFillDate": day, "lots": [], "realizedPnl": 0}
            positions.append(position)
        position["quantity"] += int(quantity)
        position["cost"] = float(dec(position["cost"]) + quantity * price + dec(fill.get("fees", 0)))
        position["averagePrice"] = float(dec(position["cost"]) / dec(position["quantity"]))
        position["firstFillDate"] = min(position["firstFillDate"], day)
        position["deadline"] = calendar.deadline(position["firstFillDate"], config.get("holdingSessions", 7))
        position["lots"].append({"fillId": fill["id"], "date": day, "quantity": int(quantity), "remaining": int(quantity), "fees": float(dec(fill.get("fees", 0))), **anchors})
        position["highSinceBuy"] = max(number(position.get("highSinceBuy"), 0), float(price))
        position["lowSinceBuy"] = min(number(position.get("lowSinceBuy"), float(price)), float(price))
    else:
        if position is None or quantity > dec(position["quantity"]):
            raise ValueError("Sell quantity exceeds open holding")
        sellable = sum(lot["remaining"] for lot in position["lots"] if lot["date"] < day)
        if quantity > sellable:
            raise ValueError("T+1: same-day bought shares are not sellable")
        left = int(quantity)
        cost_removed = dec(0)
        for lot in position["lots"]:
            if lot["date"] >= day or not left:
                continue
            used = min(left, lot["remaining"])
            cost_removed += dec(used) * dec(lot["entryPrice"]) + dec(lot["fees"]) * dec(used) / dec(lot["quantity"])
            lot["remaining"] -= used
            left -= used
        position["quantity"] -= int(quantity)
        position["cost"] = float(dec(position["cost"]) - cost_removed)
        position["realizedPnl"] = float(dec(position["realizedPnl"]) + quantity * price - dec(fill.get("fees", 0)) - cost_removed)
        position["averagePrice"] = float(dec(position["cost"]) / dec(position["quantity"])) if position["quantity"] else 0
        if not position["quantity"]:
            position["closedOn"] = day
    fills.append(copy.deepcopy(fill))
    out.setdefault("audit", []).append({"type": "MANUAL_FILL_RECORDED", "fillId": fill["id"], "symbol": fill["symbol"], "tradeDate": day})
    return out


def position_status(position: dict, quote: dict | None, calendar: TradingCalendar,
                    config: dict, as_of: str) -> dict:
    out = copy.deepcopy(position)
    age = calendar.index(position["firstFillDate"], as_of)
    out["holdingSessionIndex"] = age
    out["deadline"] = calendar.deadline(position["firstFillDate"], config.get("holdingSessions", 7))
    out["sellableQuantity"] = sum(lot["remaining"] for lot in position.get("lots", []) if lot["date"] < as_of)
    out["alerts"] = []
    if age is None:
        out["alerts"].append("CALENDAR_UNAVAILABLE")
    if age is not None and age >= config.get("holdingSessions", 7) - 1:
        out["alerts"].append("DAY_7_REVIEW_REQUIRED")
    if not quote or quote.get("stale"):
        out["alerts"].append("QUOTE_STALE_OR_MISSING")
        return out
    if not quote.get("quoteTimestamp") or str(quote["quoteTimestamp"])[:10] > as_of:
        out["alerts"].append("QUOTE_FUTURE_OR_UNTIMESTAMPED")
        return out
    p = float(quote["price"])
    # A day-high/low can predate a same-day fill. Only use last quote until next day.
    same_day = position["firstFillDate"] == as_of
    high = p if same_day else number(quote.get("high"), p)
    low = p if same_day else number(quote.get("low"), p)
    out["highSinceBuy"] = max(number(position.get("highSinceBuy"), p), high)
    out["lowSinceBuy"] = min(number(position.get("lowSinceBuy"), p), low)
    out["price"] = p
    for lot in position.get("lots", []):
        if lot.get("remaining", 0) <= 0:
            continue
        if lot.get("stop") is not None and p <= lot["stop"]:
            out["alerts"].append("STOP_TOUCHED_MANUAL_SELL")
        elif lot.get("tp2") is not None and p >= lot["tp2"]:
            out["alerts"].append("TP2_TOUCHED_MANUAL_SELL")
        elif lot.get("tp1") is not None and p >= lot["tp1"]:
            out["alerts"].append("TP1_TOUCHED_MANUAL_SELL")
        elif lot.get("tp1") is None:
            out["alerts"].append("TARGETS_AWAIT_VERIFIED_ANCHORS")
    if out["alerts"] and out["sellableQuantity"] == 0:
        out["alerts"].append("T_PLUS_1_LOCKED")
    if quote.get("suspended") or quote.get("atLimitDown"):
        out["alerts"].append("EXIT_FILL_NOT_GUARANTEED")
    out["alerts"] = list(dict.fromkeys(out["alerts"]))
    # Alerts never mutate quantities or invent fills.
    return out
