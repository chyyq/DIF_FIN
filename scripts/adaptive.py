"""As-of volatility/support research. No estimated win probabilities or orders.

All monetary anchors use unadjusted completed daily bars. The legacy fixed-grid
calculator remains in strategy.py for comparison, never as a missing-data fallback.
"""
from __future__ import annotations

from math import floor
from statistics import median, pstdev

try:
    from .strategy import number, pct_change, reward_risk, stop_reference, tick_down
except ImportError:
    from strategy import number, pct_change, reward_risk, stop_reference, tick_down


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No observations for percentile")
    point = (len(ordered) - 1) * fraction
    lower = int(point)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (point - lower)


def market_profile(rows: list[dict], decision_date: str, config: dict) -> dict:
    """Only completed bars strictly before the decision date; never today's low."""
    out = {"available": False, "reason": "VOLATILITY_HISTORY_MISSING", "asOf": None}
    prior = sorted((r for r in rows if r.get("date", "") < decision_date), key=lambda r: r["date"])
    lookback = config.get("adaptivePricing", {}).get("lookbackSessions", 20)
    selected = prior[-(lookback + 1):]
    if len(selected) < lookback + 1 or len({r["date"] for r in selected}) != len(selected):
        return out
    for row in selected:
        if row.get("adjusted") is True:
            return {**out, "reason": "ADJUSTED_HISTORY_NOT_EXECUTABLE"}
        if any(number(row.get(k), 0) <= 0 for k in ("open", "high", "low", "close")):
            return out
        if float(row["low"]) > min(float(row["open"]), float(row["close"])) or float(row["high"]) < max(float(row["open"]), float(row["close"])):
            return {**out, "reason": "INVALID_OHLC_HISTORY"}
    closes = [float(r["close"]) for r in selected]
    trs = [max(float(r["high"]) - float(r["low"]), abs(float(r["high"]) - closes[i]), abs(float(r["low"]) - closes[i])) for i, r in enumerate(selected[1:])]
    atr = sum(trs[-14:]) / 14
    if atr <= 0:
        return {**out, "reason": "ZERO_VOLATILITY_HISTORY"}
    recent = selected[1:]
    pullbacks = []
    for i, row in enumerate(recent):
        peak = max(float(r["high"]) for r in recent[max(0, i - 4):i + 1])
        pullbacks.append(max(0, (peak - float(row["close"])) / peak))
    pivots = [float(recent[i]["low"]) for i in range(2, len(recent) - 2)
              if float(recent[i]["low"]) <= min(float(r["low"]) for r in recent[i - 2:i + 3])]
    # The ten-session low is a conservative research support proxy when no
    # completed two-sided pivot exists. It is not a verified structural fact.
    support = pivots[-1] if pivots else min(float(r["low"]) for r in recent[-10:])
    declines = 0
    for old, current in reversed(list(zip(closes, closes[1:]))):
        if current >= old:
            break
        declines += 1
    return {"available": True, "reason": None, "asOf": selected[-1]["date"],
            "historySessions": len(prior), "atr14": round(atr, 6), "atrPct": round(100 * atr / closes[-1], 6),
            "close": closes[-1], "ma10": sum(closes[-10:]) / 10, "ma20": sum(closes[-20:]) / 20,
            "support": support, "supportKind": "CONFIRMED_DAILY_PIVOT" if pivots else "TEN_SESSION_LOW_PROXY",
            "recentPeak": max(float(r["high"]) for r in recent),
            "return5Pct": pct_change(closes[-1], closes[-6]), "return10Pct": pct_change(closes[-1], closes[-11]),
            "realizedDailyVolPct": 100 * pstdev([b / a - 1 for a, b in zip(closes, closes[1:])]),
            "consecutiveDeclines": declines, "pullbackMedian": median(pullbacks), "pullback75": percentile(pullbacks, .75)}


def falling_risk(profile: dict, price: float, config: dict) -> tuple[list[str], dict]:
    if not config.get("riskFilters", {}).get("enabled", False):
        return [], {}
    if not profile.get("available"):
        return [], {"available": False}
    rules = config.get("riskFilters", {})
    atr_pct = profile["atrPct"]
    threshold5 = min(rules.get("maxFiveDayLossPct", 10), max(rules.get("minFiveDayLossPct", 6), 2.5 * atr_pct))
    threshold10 = min(rules.get("maxTenDayLossPct", 16), max(rules.get("minTenDayLossPct", 8), 4 * atr_pct))
    peak_loss = max(0, 100 * (1 - price / profile["recentPeak"]))
    peak_limit = min(rules.get("maxPeakDrawdownPct", 22), max(rules.get("minPeakDrawdownPct", 12), 6 * atr_pct))
    reasons = []
    if profile["return5Pct"] < -threshold5 or profile["return10Pct"] < -threshold10:
        reasons.append("RECENT_RAPID_DECLINE")
    if peak_loss > peak_limit:
        reasons.append("RECENT_PEAK_COLLAPSE")
    if atr_pct > rules.get("maxAtrPct", 7):
        reasons.append("VOLATILITY_TOO_HIGH")
    if price < profile["support"] - rules.get("supportBreakAtr", .5) * profile["atr14"]:
        reasons.append("DAILY_SUPPORT_BROKEN")
    if profile["consecutiveDeclines"] >= rules.get("maxConsecutiveDeclines", 4) and price < profile["ma20"]:
        reasons.append("PERSISTENT_DECLINE_BELOW_MA20")
    if pct_change(price, profile["close"]) < -threshold5:
        reasons.append("CURRENT_SESSION_SELL_OFF")
    return reasons, {"available": True, "fiveDayLossLimitPct": threshold5, "tenDayLossLimitPct": threshold10,
                     "recentPeakDrawdownPct": peak_loss, "recentPeakLossLimitPct": peak_limit}


def adaptive_levels(peak: float, profile: dict, config: dict, limit_down=None) -> list[dict]:
    if not profile.get("available") or peak <= 0:
        return []
    rules = config.get("adaptivePricing", {})
    atr = profile["atr14"]
    a = min(rules.get("maxDrawdownA", .10), max(rules.get("minDrawdownA", .02),
            rules.get("atrMultiplierA", 1.5) * atr / peak, profile["pullbackMedian"]))
    b = min(rules.get("maxDrawdownB", .14), max(rules.get("minDrawdownB", .035),
            rules.get("atrMultiplierB", 2.5) * atr / peak, profile["pullback75"], a + .5 * atr / peak))
    if b <= a:
        return []
    tick = config.get("tick", .01)
    levels = []
    for name, dd in (("A", a), ("B", b)):
        trigger = tick_down(peak * (1 - dd), tick)
        # A nearby daily support/MA adds context; it never moves an otherwise
        # invalid deep entry up to an unrelated level to force an eligible plan.
        supports = [p for p in (profile["support"], profile["ma10"], profile["ma20"]) if 0 < p < peak]
        nearby = min(supports, key=lambda p: abs(p - trigger), default=None)
        confluence = nearby is not None and abs(nearby - trigger) <= .5 * atr
        cap = tick_down(min(trigger * (1 + config.get("entryCapBuffer", .015)), trigger + rules.get("capAtrBuffer", .1) * atr, peak - tick), tick)
        tp1 = tick_down(trigger + config.get("tp1Fraction", .382) * (peak - trigger), tick)
        tp2 = tick_down(trigger + config.get("tp2Fraction", .618) * (peak - trigger), tick)
        stop = stop_reference(profile["support"], trigger, atr, tick)
        structure_ok = trigger >= profile["support"] - .5 * atr
        reachable = limit_down is None or trigger >= limit_down
        levels.append({"id": name, "drawdownPct": round(100 * (1 - trigger / peak), 6),
                       "triggerPrice": trigger, "maxBuyPrice": cap, "tp1": tp1, "tp2": tp2,
                       "tp1Pct": pct_change(tp1, cap), "tp2Pct": pct_change(tp2, cap),
                       "plannedStop": stop, "rewardRisk": reward_risk(cap, tp1, tp2, stop, tick, config.get("sellSplit")),
                       "priceMethod": "ATR14_SWING_SUPPORT", "atr14": atr, "supportReference": profile["support"],
                       "supportConfluence": confluence, "nearestDailySupport": nearby, "structurePlanValid": structure_ok,
                       "reachableToday": reachable, "status": "BELOW_STRUCTURE_SUPPORT" if not structure_ok else "WAIT_CONFIRM" if reachable else "UNREACHABLE_TODAY",
                       "sellSplit": config.get("sellSplit", [.5, .5])})
    return levels


def admission_ok(event: dict, config: dict) -> bool:
    if not config.get("selectionRules", {}).get("strictAdmission", False):
        return True
    if not event.get("gates", {}).get("trend") or not event.get("gates", {}).get("dailyHistory") or not event.get("volatility", {}).get("available"):
        return False
    price = number(event.get("price"), 0)
    if event.get("riskVetoReasons") or event.get("stale") or not 0 < price <= config.get("selectionRules", {}).get("maxPrice", 60):
        return False
    if number(event.get("amount"), 0) < config.get("minimumAmount", 100_000_000):
        return False
    if event.get("metrics", {}).get("historySessions", 0) < config.get("minimumHistorySessions", 120):
        return False
    return any(level.get("structurePlanValid") and level.get("payoffPassed") and level.get("reachableToday") for level in event.get("buyLevels", []))


def ranking_key(event: dict, config: dict) -> tuple:
    """Quality and verified readiness first; lower nominal prices within a band."""
    price = number(event.get("price"), float("inf"))
    score = number(event.get("trendScore"), 0)
    band = config.get("selectionRules", {}).get("qualityBandSize", 10)
    quality = floor(score / max(1, band))
    return (0 if event.get("status") == "BUY_READY" else 1, -quality,
            price if config.get("selectionRules", {}).get("preferLowerPrice", True) else 0,
            -number(event.get("amount"), 0), event.get("symbol", ""))


def history_probes(quotes: list[dict], known_symbols: set[str], cached: dict, config: dict) -> list[dict]:
    """Bounded enrichment, not a claim that every stock's trend was analyzed."""
    maximum = config.get("selectionRules", {}).get("maxPrice", 60)
    allowed = [q for q in quotes if q.get("segment") in config.get("allowedSegments", [])
               and 0 < number(q.get("price"), 0) <= maximum
               and number(q.get("amount"), 0) >= config.get("minimumAmount", 100_000_000)
               and not q.get("isST") and "ST" not in q.get("name", "").upper() and "退" not in q.get("name", "")]
    existing = [q for q in allowed if q["symbol"] in known_symbols]
    touches = [q for q in allowed if q.get("limitPriceVerified") and number(q.get("high"), 0) >= number(q.get("limitUpPrice"), float("inf"))]
    # Wide prefilter admits down-day pullbacks as well as up days. Exact trend
    # eligibility is established only after actual historical bars are fetched.
    trends = [q for q in allowed if q not in touches and -5 <= number(q.get("pct"), 0) <= 6]
    key = lambda q: (q["symbol"] not in cached, number(q.get("price"), float("inf")), -number(q.get("amount"), 0), q["symbol"])
    limit = config.get("selectionRules", {}).get("maxHistorySymbols", 120)
    ordered = sorted(existing, key=key)[:limit // 3] + sorted(touches, key=key)[:limit // 3] + sorted(trends, key=key)
    result, seen = [], set()
    for quote in ordered:
        if quote["symbol"] not in seen:
            result.append(quote)
            seen.add(quote["symbol"])
        if len(result) == limit:
            break
    return result
