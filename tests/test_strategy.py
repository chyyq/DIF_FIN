"""Deterministic synthetic fixtures; no fixture is shipped as a recommendation."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.strategy import (TradingCalendar, absorption_metrics, dec, evaluate_event,
    freeze_fill_targets, hard_veto, manual_fill, number, pct_change, plan_levels,
    position_status, reward_risk, rotate_pool, stop_reference, tick_down, trend_metrics)
from scripts.providers import normalize_eastmoney, parse_tencent
from scripts.update_data import (append_audit, build_snapshot, load_json, parameter_hash,
    run, stale_snapshot, write_json)
from scripts.learning import analyze, descriptive, drawdown_bin, extract_closed_trades, normalize_export
from scripts.backtest import readiness

CONFIG = json.loads((ROOT / "config/strategy.json").read_text(encoding="utf-8"))
SESSIONS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09", "2026-10-12",
            "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19",
            "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
CAL = TradingCalendar(SESSIONS, "unit-test explicit exchange-session fixture", True)
NOW = datetime.fromisoformat("2026-10-23T10:00:00+08:00")


def history(count=130):
    start = date(2026, 1, 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "open": 9.30 + i * .01,
             "close": 9.30 + i * .01, "high": 9.4 + i * .01,
             "low": 9.20 + i * .01, "amount": 200_000_000} for i in range(count)]


def quote(**changes):
    q = {"symbol": "600000.SH", "code": "600000", "name": "FIXTURE ONLY",
         "segment": "SSE_MAIN", "price": 9.85, "high": 11, "low": 9.70,
         "preClose": 10, "amount": 200_000_000, "limitUpPrice": 11,
         "limitDownPrice": 9, "limitPriceVerified": True, "sector": "TEST",
         "quoteTimestamp": NOW.isoformat(), "securityMetadataVerified": True,
         "announcementsVerified": True, "sectorVerifiedStable": True,
         "structureVerified": True, "accountStateSynced": True, "stale": False,
         "eventLowVerified": True, "eventLowAsOf": 9.70}
    q.update(changes)
    return q


def event(**changes):
    e = {"strategyId": CONFIG["strategyId"], "symbol": "600000.SH", "code": "600000",
         "name": "FIXTURE ONLY", "segment": "SSE_MAIN", "sector": "TEST", "eventId": "600000.SH:2026-10-23:01",
         "eventDate": "2026-10-23", "eventVersion": 1, "limitUpPrice": 11,
         "referencePeak": 11, "limitEventVerified": True, "status": "WATCH"}
    e.update(changes)
    return e


def bars():
    values = [(10, 9.75, 9.70, 10_000_000, 2), (9.75, 9.76, 9.70, 8_000_000, 1.9),
              (9.76, 9.77, 9.71, 4_000_000, .5), (9.77, 9.80, 9.72, 4_000_000, .5),
              (9.80, 9.81, 9.73, 4_000_000, .5)]
    return [{"open": o, "close": c, "low": low, "amount": amount, "rvol": rvol,
             "baselineSessions": 20, "complete": True, "vwap": c - .01,
             "end": (NOW - timedelta(minutes=20 - 5 * i)).isoformat()}
            for i, (o, c, low, amount, rvol) in enumerate(values)]


def buy(fill_id="b1", **changes):
    f = {"id": fill_id, "side": "BUY", "symbol": "600000.SH", "tradeDate": "2026-10-12",
         "price": 9.80, "quantity": 100, "fees": 1, "entryPeak": 11,
         "entryPivotLow": 9.70, "support": 9.60, "atr14": .2,
         "eventId": "e1"}
    f.update(changes)
    return f


class PriceTests(unittest.TestCase):
    def test_exact_a_b_prices(self):
        a, b = plan_levels(11, CONFIG)
        self.assertEqual((a["triggerPrice"], a["maxBuyPrice"], a["tp1"], a["tp2"]), (9.79, 9.93, 10.25, 10.53))
        self.assertEqual((b["triggerPrice"], b["maxBuyPrice"], b["tp1"], b["tp2"]), (9.57, 9.71, 10.11, 10.45))

    def test_decimal_tick_no_binary_rounding(self):
        self.assertEqual(tick_down("9.93"), 9.93)
        self.assertEqual(tick_down("9.939999"), 9.93)
        self.assertEqual(tick_down("10.12", ".05"), 10.10)

    def test_nonfinite_rejected(self):
        for value in ("NaN", "Infinity", "bad", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): dec(value)

    def test_bad_peak_rejected(self):
        for value in (0, -1):
            with self.assertRaises(ValueError): plan_levels(value, CONFIG)

    def test_bad_tick_rejected(self):
        with self.assertRaises(ValueError): tick_down(10, 0)

    def test_unreachable_not_clamped(self):
        a, b = plan_levels(11, CONFIG, limit_down=10)
        self.assertFalse(a["reachableToday"])
        self.assertEqual(a["triggerPrice"], 9.79)
        self.assertEqual(b["status"], "UNREACHABLE_TODAY")

    def test_stop_formula(self):
        self.assertEqual(stop_reference(9.5, 9.6, .3), 9.35)
        self.assertEqual(stop_reference(10, 9.6, .1), 9.50)

    def test_missing_support_blocks(self):
        self.assertIsNone(stop_reference(None, 9.6, .3))
        self.assertIsNone(plan_levels(11, CONFIG)[0]["rewardRisk"])

    def test_payoff_formula_and_bad_stop(self):
        self.assertEqual(reward_risk(10, 11, 12, 9), 1.5)
        self.assertIsNone(reward_risk(10, 11, 12, 10))

    def test_targets_freeze_from_entry_pivot(self):
        f = freeze_fill_targets(11, 9.7, 9.8, 9.6, .2, CONFIG)
        self.assertEqual((f["tp1"], f["tp2"], f["stop"]), (10.19, 10.50, 9.50))
        self.assertEqual(f["entryPeak"], 11)

    def test_target_below_entry_review(self):
        self.assertTrue(freeze_fill_targets(11, 9.7, 10.5, 9.6, .2, CONFIG)["reviewRequired"])


class CalendarTests(unittest.TestCase):
    def test_holiday_not_weekday_count(self):
        self.assertEqual(CAL.index("2026-09-30", "2026-10-09"), 1)
        self.assertEqual(CAL.deadline("2026-09-30", 7), "2026-10-16")

    def test_unknown_future_deadline(self):
        self.assertIsNone(CAL.deadline("2026-10-23", 7))

    def test_unknown_start(self):
        self.assertIsNone(CAL.index("2026-10-01", "2026-10-23"))

    def test_weekend_asof(self):
        self.assertEqual(CAL.index("2026-10-22", "2026-10-24"), 1)

    def test_dedup_sorted(self):
        self.assertEqual(TradingCalendar([SESSIONS[1], SESSIONS[0], SESSIONS[1]]).sessions, SESSIONS[:2])

    def test_no_weekday_fallback(self):
        self.assertIsNone(TradingCalendar([]).deadline("2026-10-23", 7))
        self.assertEqual(TradingCalendar([]).sessions, [])


class GateTests(unittest.TestCase):
    def test_daily_only_always_watch(self):
        out = evaluate_event(event(), quote(), history(), CAL, CONFIG, "2026-10-23", now=NOW)
        self.assertEqual(out["status"], "WATCH")
        self.assertIn("INTRADAY_CONFIRMATION_UNAVAILABLE", out["reasonCodes"])

    def test_fully_verified_synthetic_signal_can_be_ready(self):
        out = evaluate_event(event(), quote(), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["reasonCodes"], [])
        self.assertEqual(out["status"], "BUY_READY")

    def test_incomplete_bars_ignored(self):
        values = bars()
        values[-1]["complete"] = False
        self.assertFalse(absorption_metrics(values, CONFIG, True)["available"])

    def test_daily_volume_ratio_is_not_intraday_baseline(self):
        values = bars()
        values[-1].pop("baselineSessions")
        self.assertFalse(absorption_metrics(values, CONFIG, True)["available"])

    def test_panic_absorption(self):
        out = absorption_metrics(bars(), CONFIG, True)
        self.assertEqual(out["pattern"], "PANIC_ABSORPTION")
        self.assertTrue(out["confirmed"])

    def test_new_lows_penalized(self):
        values = bars()
        values[-1]["low"] = 9
        self.assertFalse(absorption_metrics(values, CONFIG, True)["noNewLow"])

    def test_lookahead_history_excluded(self):
        past = trend_metrics(history(), "2026-10-23")
        future = history() + [{"date": "2026-10-24", "close": 1000000}]
        self.assertEqual(past, trend_metrics(future, "2026-10-23"))

    def test_short_history_watch(self):
        out = evaluate_event(event(), quote(), history(60), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "WATCH")
        self.assertIn("INSUFFICIENT_HISTORY", out["reasonCodes"])

    def test_chinext_segment_reject(self):
        out = evaluate_event(event(), quote(segment="CHINEXT"), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "CANCEL")

    def test_chinext_prefix_integrity_check(self):
        self.assertIn("EXCLUDED_BOARD_CODE", hard_veto(quote(code="300001"), CONFIG))

    def test_unknown_event_segment_cannot_bypass_quote_gate(self):
        out = evaluate_event(event(segment="UNKNOWN"), quote(), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "CANCEL")

    def test_missing_current_limits_cannot_be_buy_ready(self):
        out = evaluate_event(event(), quote(limitPriceVerified=False, limitDownPrice=None), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "WATCH")
        self.assertIn("CURRENT_DAILY_LIMITS_UNVERIFIED", out["reasonCodes"])

    def test_other_symbol_quote_cannot_trigger_event(self):
        out = evaluate_event(event(), quote(symbol="600001.SH"), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "CANCEL")

    def test_future_quote_not_accepted_for_entry(self):
        out = evaluate_event(event(), quote(quoteTimestamp=(NOW + timedelta(minutes=1)).isoformat()), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "WATCH")

    def test_st_reject(self):
        self.assertIn("SPECIAL_RISK_SECURITY", hard_veto(quote(name="*ST TEST"), CONFIG))

    def test_suspended_reject(self):
        self.assertIn("SUSPENDED", hard_veto(quote(suspended=True), CONFIG))

    def test_price_not_touch_cap_no_chase(self):
        out = evaluate_event(event(), quote(price=10), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "WATCH")

    def test_extreme_drawdown_no_passive_buy(self):
        out = evaluate_event(event(), quote(price=9), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["riskLevel"], "EXTREME")
        self.assertNotEqual(out["status"], "BUY_READY")

    def test_signal_ttl(self):
        out = evaluate_event(event(), quote(), history(), CAL, CONFIG, "2026-10-23", bars(), NOW + timedelta(seconds=181))
        self.assertEqual(out["status"], "WATCH")
        self.assertIn("SIGNAL_STALE_OR_UNTIMESTAMPED", out["reasonCodes"])

    def test_anchor_version_increments(self):
        out = evaluate_event(event(), quote(high=11.2), history(), CAL, CONFIG, "2026-10-23")
        self.assertEqual(out["eventVersion"], 2)
        self.assertEqual(out["referencePeak"], 11.2)
        self.assertTrue(out["pendingOrdersInvalidated"])

    def test_expiry_after_seven_sessions(self):
        e = event(eventDate="2026-10-13")
        out = evaluate_event(e, quote(), history(), CAL, CONFIG, "2026-10-23")
        self.assertEqual(out["eventDayIndex"], 8)
        self.assertEqual(out["status"], "EXPIRE")

    def test_unverified_event_watch(self):
        out = evaluate_event(event(limitEventVerified=False), quote(), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertEqual(out["status"], "WATCH")

    def test_t0_daily_low_before_touch_not_signal(self):
        out = evaluate_event(event(), quote(eventLowVerified=False), history(), CAL, CONFIG, "2026-10-23", bars(), NOW)
        self.assertFalse(out["buyLevels"][0]["touched"])
        self.assertEqual(out["status"], "WATCH")

    def test_new_peak_invalidates_old_touch(self):
        old = event(buyLevels=[{"id": "A", "touched": True}])
        out = evaluate_event(old, quote(high=12, price=11, eventLowVerified=False), history(), CAL, CONFIG, "2026-10-23")
        self.assertFalse(out["buyLevels"][0]["touched"])


class PoolTests(unittest.TestCase):
    def make_event(self, code, sector="TEST", day="2026-10-23"):
        return event(symbol=f"{code}.SH", eventId=f"{code}:{day}", eventDayIndex=0, sector=sector)

    def test_no_padding(self):
        pool, candidates, _ = rotate_pool({}, [self.make_event("600000")], CAL, CONFIG, "2026-10-23")
        self.assertEqual(len(candidates), 1)

    def test_max_two_sector(self):
        events = [self.make_event(f"60000{i}") for i in range(5)]
        _, candidates, _ = rotate_pool({}, events, CAL, CONFIG, "2026-10-23")
        self.assertEqual(len(candidates), 2)

    def test_max_ten(self):
        events = [self.make_event(f"6000{i:02}", f"sector{i}") for i in range(15)]
        _, candidates, _ = rotate_pool({}, events, CAL, CONFIG, "2026-10-23")
        self.assertEqual(len(candidates), 10)

    def test_same_symbol_once(self):
        e = self.make_event("600000")
        _, candidates, _ = rotate_pool({}, [e, e], CAL, CONFIG, "2026-10-23")
        self.assertEqual(len(candidates), 1)

    def test_freeze_membership_update_price(self):
        e = self.make_event("600000")
        old = {"pool": {"batchId": "b1", "startedOn": "2026-10-20"}, "candidates": [e]}
        new = {**e, "price": 10}
        pool, candidates, _ = rotate_pool(old, [new, self.make_event("600001")], CAL, CONFIG, "2026-10-23")
        self.assertTrue(pool["retained"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["price"], 10)

    def test_seven_session_rotation_archives(self):
        old = {"pool": {"batchId": "b1", "startedOn": "2026-10-14"}, "candidates": [self.make_event("600000")]}
        pool, candidates, archive = rotate_pool(old, [self.make_event("600001")], CAL, CONFIG, "2026-10-23")
        self.assertFalse(pool["retained"])
        self.assertEqual(candidates[0]["symbol"], "600001.SH")
        self.assertEqual(len(archive), 1)


class LedgerTests(unittest.TestCase):
    def test_multiple_buys_merge_row_and_keep_lots(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        ledger = manual_fill(ledger, buy("b2", price=10, quantity=200), CAL, CONFIG)
        self.assertEqual(len(ledger["positions"]), 1)
        self.assertEqual(ledger["positions"][0]["quantity"], 300)
        self.assertEqual(len(ledger["positions"][0]["lots"]), 2)
        self.assertAlmostEqual(ledger["positions"][0]["averagePrice"], 2982 / 300)

    def test_fill_idempotency(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        self.assertEqual(manual_fill(ledger, buy(), CAL, CONFIG), ledger)

    def test_conflicting_fill_id_rejected(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        with self.assertRaises(ValueError):
            manual_fill(ledger, buy(price=10), CAL, CONFIG)

    def test_same_day_sell_blocked(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        with self.assertRaises(ValueError):
            manual_fill(ledger, {"id": "s1", "side": "SELL", "symbol": "600000.SH", "tradeDate": "2026-10-12", "quantity": 100, "price": 10}, CAL, CONFIG)

    def test_partial_sell_actual_remaining(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        ledger = manual_fill(ledger, {"id": "s1", "side": "SELL", "symbol": "600000.SH", "tradeDate": "2026-10-13", "quantity": 50, "price": 10.2}, CAL, CONFIG)
        self.assertEqual(ledger["positions"][0]["quantity"], 50)
        self.assertEqual(ledger["positions"][0]["lots"][0]["remaining"], 50)
        self.assertAlmostEqual(ledger["positions"][0]["realizedPnl"], 19.5)

    def test_addition_does_not_reset_clock(self):
        ledger = manual_fill({}, buy(), CAL, CONFIG)
        ledger = manual_fill(ledger, buy("b2", tradeDate="2026-10-14"), CAL, CONFIG)
        self.assertEqual(ledger["positions"][0]["firstFillDate"], "2026-10-12")
        self.assertEqual(ledger["positions"][0]["deadline"], "2026-10-20")

    def test_invalid_quantity_blocked(self):
        with self.assertRaises(ValueError): manual_fill({}, buy(quantity=0), CAL, CONFIG)

    def test_seventh_day_alert(self):
        position = manual_fill({}, buy(), CAL, CONFIG)["positions"][0]
        status = position_status(position, quote(quoteTimestamp="2026-10-20T10:00:00+08:00"), CAL, CONFIG, "2026-10-20")
        self.assertIn("DAY_7_REVIEW_REQUIRED", status["alerts"])
        self.assertEqual(status["quantity"], 100)

    def test_stop_touch_no_fake_sell(self):
        position = manual_fill({}, buy(), CAL, CONFIG)["positions"][0]
        status = position_status(position, quote(price=9, quoteTimestamp="2026-10-13T10:00:00+08:00"), CAL, CONFIG, "2026-10-13")
        self.assertIn("STOP_TOUCHED_MANUAL_SELL", status["alerts"])
        self.assertEqual(status["quantity"], 100)

    def test_same_day_high_does_not_leak_before_fill(self):
        position = manual_fill({}, buy(), CAL, CONFIG)["positions"][0]
        status = position_status(position, quote(price=9.9, high=11.5, low=9, quoteTimestamp="2026-10-12T10:00:00+08:00"), CAL, CONFIG, "2026-10-12")
        self.assertEqual(status["highSinceBuy"], 9.9)
        self.assertEqual(status["lowSinceBuy"], 9.8)

    def test_failed_quote_does_not_erase_extrema(self):
        position = manual_fill({}, buy(), CAL, CONFIG)["positions"][0]
        status = position_status(position, None, CAL, CONFIG, "2026-10-13")
        self.assertEqual(status["highSinceBuy"], position["highSinceBuy"])
        self.assertIn("QUOTE_STALE_OR_MISSING", status["alerts"])

    def test_manual_fill_missing_anchors_still_recorded(self):
        f = buy()
        f.pop("entryPivotLow")
        ledger = manual_fill({}, f, CAL, CONFIG)
        self.assertEqual(ledger["positions"][0]["quantity"], 100)
        self.assertIsNone(ledger["positions"][0]["lots"][0]["tp1"])
        self.assertIn("TARGETS_AWAIT_VERIFIED_ANCHORS", position_status(ledger["positions"][0], quote(quoteTimestamp="2026-10-13T10:00:00+08:00"), CAL, CONFIG, "2026-10-13")["alerts"])

    def test_future_quote_does_not_change_position_extrema(self):
        position = manual_fill({}, buy(), CAL, CONFIG)["positions"][0]
        status = position_status(position, quote(price=100), CAL, CONFIG, "2026-10-13")
        self.assertEqual(status["highSinceBuy"], 9.8)
        self.assertIn("QUOTE_FUTURE_OR_UNTIMESTAMPED", status["alerts"])


class PipelineTests(unittest.TestCase):
    def test_exact_limit_not_pct_heuristic(self):
        q = quote(high=10.99, pct=9.9)
        snap, events, _ = build_snapshot({}, [], [q], {}, CAL, CONFIG, NOW, [], [])
        self.assertEqual(events, [])

    def test_rejected_event_still_audited(self):
        q = quote(name="ST TEST")
        snap, events, audit = build_snapshot({}, [], [q], {q["symbol"]: history()}, CAL, CONFIG, NOW, [], [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "CANCEL")
        self.assertEqual(len(snap["candidates"]), 0)
        self.assertTrue(any(row["type"] == "EVENT_EVALUATED" for row in audit))

    def test_failed_refresh_retains_data_stale(self):
        previous = {"meta": {"generatedAt": "old", "quoteTimestamp": "source"}, "candidates": [event(status="BUY_READY")]}
        out = stale_snapshot(previous, CONFIG, NOW, ["network"], [])
        self.assertTrue(out["meta"]["stale"])
        self.assertEqual(out["meta"]["quoteTimestamp"], "source")
        self.assertEqual(out["candidates"][0]["status"], "WATCH")
        self.assertEqual(previous["candidates"][0]["status"], "BUY_READY")

    def test_offline_cli_preserves_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "config/strategy.json", CONFIG)
            write_json(root / "data/quotes.json", {"meta": {"quoteTimestamp": "old"}, "quotes": {"600000.SH": quote()}})
            self.assertEqual(run(root, offline=True, now=NOW), 0)
            out = load_json(root / "data/quotes.json", {})
            self.assertTrue(out["meta"]["stale"])
            self.assertEqual(out["quotes"]["600000.SH"]["price"], 9.85)

    def test_audit_append_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            append_audit(path, [{"a": 1}])
            append_audit(path, [{"a": 2}])
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_parameter_hash_changes(self):
        self.assertNotEqual(parameter_hash(CONFIG), parameter_hash({**CONFIG, "triggerA": .12}))

    def test_normalize_quote_missing_time_stale(self):
        q = normalize_eastmoney({"f12": "600000", "f13": 1, "f2": 10, "f14": "TEST"}, "SSE_MAIN")
        self.assertTrue(q["stale"])
        self.assertFalse(q["securityMetadataVerified"])

    def test_tencent_parser_real_layout(self):
        p = [""] * 40
        for index, value in {1: "TEST", 3: "10", 4: "9.8", 5: "9.9", 30: "20261023145959", 32: "2.04", 33: "10.1", 34: "9.7", 37: "12345"}.items(): p[index] = value
        raw = 'v_sh600000="' + "~".join(p) + '";'
        q = parse_tencent(raw)[0]
        self.assertEqual(q["symbol"], "600000.SH")
        self.assertEqual(q["amount"], 123450000)
        self.assertTrue(q["quoteTimestamp"].startswith("2026-10-23"))


class LearningTests(unittest.TestCase):
    def canonical_fixture(self):
        events = [{"id": "e1", "symbol": "600000", "snapshots": [
            {"id": "snapshot1", "recordedAt": "2026-10-12T01:00:00.000Z", "data": event(eventId="e1", eventDate="2026-10-12")}]},
            {"id": "unfilled", "symbol": "600001", "snapshots": [
            {"id": "snapshot2", "recordedAt": "2026-10-12T01:00:00.000Z", "data": event(eventId="unfilled", symbol="600001.SH")}]}]
        fills = [{"id": "b1", "symbol": "600000", "name": "FIXTURE", "side": "BUY", "priceCents": 980,
                  "feesCents": 100, "qty": 100, "executedAt": "2026-10-12T02:00:00.000Z", "tradeDate": "2026-10-12",
                  "eventId": "e1", "eventSnapshotId": "snapshot1", "entry": {"highCents": 1100, "lowCents": 970, "tp1Cents": 1019, "tp2Cents": 1050, "stopCents": 950}},
                 {"id": "s1", "symbol": "600000", "name": "FIXTURE", "side": "SELL", "priceCents": 1020,
                  "feesCents": 100, "qty": 100, "executedAt": "2026-10-13T02:00:00.000Z", "tradeDate": "2026-10-13",
                  "allocations": [{"buyFillId": "b1", "qty": 100, "entryFeesCents": 100, "costCents": 98100}]}]
        return {"schema": "lbr-portfolio", "version": 1, "strategyId": "LBR_MAIN_V1",
                "config": {"maxHoldSessions": 7, "buyFractions": [.3, .3, .4]}, "events": events, "fills": fills, "quotes": {}, "observations": []}

    def test_canonical_export_integration_cents_allocations_snapshots(self):
        result = analyze([], self.canonical_fixture(), CONFIG)
        self.assertEqual(result["validationErrors"], [])
        self.assertEqual(result["independentEventCount"], 2)
        self.assertEqual(result["closedTradeCount"], 1)
        self.assertEqual(result["unfilledEventCount"], 1)
        self.assertEqual(result["closedTrades"][0]["pnl"], 38)
        self.assertEqual(result["summary"]["positiveFraction"], 1)

    def test_canonical_lot_cost_tampering_rejected(self):
        source = self.canonical_fixture()
        source["fills"][1]["allocations"][0]["costCents"] = 1
        result = analyze([], source, CONFIG)
        self.assertEqual(result["closedTradeCount"], 0)
        self.assertIn("Modified buy-cost", result["validationErrors"][0])

    def test_canonical_partial_exit_not_closed(self):
        source = self.canonical_fixture()
        source["fills"][1]["qty"] = 50
        source["fills"][1]["allocations"][0].update(qty=50, entryFeesCents=50, costCents=49050)
        result = analyze([], source, CONFIG)
        self.assertEqual(result["closedTradeCount"], 0)

    def test_canonical_same_symbol_events_use_lot_attribution(self):
        source = self.canonical_fixture()
        second = {**source["fills"][0], "id": "b2", "eventId": "e2", "priceCents": 1000}
        source["events"].append({"id": "e2", "symbol": "600000", "snapshots": [{"id": "s2", "recordedAt": "2026-10-12T02:00:00.000Z", "data": event(eventId="e2")}]})
        source["fills"].insert(1, second)
        # Sell only b1: e1 closes while e2 stays open, although symbol net qty > 0.
        result = analyze([], source, CONFIG)
        self.assertEqual(result["closedTradeCount"], 1)
        self.assertEqual(result["closedTrades"][0]["eventIds"], ["e1"])
        self.assertEqual(next(e for e in result["events"] if e["eventId"] == "e2")["outcome"], "OPEN_OR_PARTIAL")

    def test_canonical_t_plus_1_rejected(self):
        source = self.canonical_fixture()
        source["fills"][1]["tradeDate"] = "2026-10-12"
        result = analyze([], source, CONFIG)
        self.assertEqual(result["closedTradeCount"], 0)
        self.assertIn("T+1", result["validationErrors"][0])

    def test_canonical_snapshots_latest_asof(self):
        source = self.canonical_fixture()
        source["events"][0]["snapshots"].append({"id": "new", "recordedAt": "2026-10-13T02:00:00.000Z", "data": event(eventId="e1", eventVersion=2)})
        normalized = normalize_export(source)
        self.assertEqual(normalized["events"][0]["eventVersion"], 2)
        self.assertEqual(normalized["events"][0]["snapshotCount"], 2)

    def test_rule_candidates_private_descriptive_and_blocked(self):
        result = analyze([], self.canonical_fixture(), CONFIG)
        research = result["ruleResearch"]
        self.assertTrue(research["candidateRules"])
        self.assertEqual(research["parameterHash"], parameter_hash(CONFIG))
        self.assertEqual(research["activeParameterChanges"], [])
        self.assertTrue(all(rule["blocked"] and not rule["autoApply"] for rule in research["candidateRules"]))

    def test_one_day_100_events_not_out_of_sample(self):
        result = analyze([event(eventId=f"e{i}") for i in range(100)], {}, CONFIG)
        research = result["ruleResearch"]
        self.assertIsNone(research["validationSplitDate"])
        self.assertEqual(research["status"], "BLOCKED_INSUFFICIENT_VALIDATION")
        self.assertTrue(all("NO_TIME_SEPARATED_VALIDATION" in rule["blockReasons"] for rule in research["candidateRules"]))

    def test_unfilled_not_loss(self):
        result = analyze([event(eventId="e1")], {}, CONFIG)
        self.assertEqual(result["unfilledEventCount"], 1)
        self.assertEqual(result["closedTradeCount"], 0)
        self.assertIsNone(result["summary"]["positiveFraction"])

    def test_actual_closed_after_fees(self):
        fills = [buy(), {"id": "s1", "side": "SELL", "symbol": "600000.SH", "tradeDate": "2026-10-13", "quantity": 100, "price": 10.2, "fees": 1, "eventId": "e1"}]
        closed, errors = extract_closed_trades({"fills": fills})
        self.assertEqual(errors, [])
        self.assertEqual(closed[0]["pnl"], 38)

    def test_partial_exit_not_closed_win(self):
        fills = [buy(), {"id": "s1", "side": "SELL", "symbol": "600000.SH", "tradeDate": "2026-10-13", "quantity": 50, "price": 11}]
        self.assertEqual(extract_closed_trades({"fills": fills})[0], [])

    def test_duplicate_fill_not_double_counted(self):
        result = extract_closed_trades({"fills": [buy(), buy(), {"id": "s1", "side": "SELL", "symbol": "600000.SH", "tradeDate": "2026-10-13", "quantity": 100, "price": 10}]})
        self.assertEqual(len(result[0]), 1)

    def test_less_than_30_no_optimization(self):
        result = analyze([event(eventId=f"e{i}") for i in range(29)], {}, CONFIG)
        self.assertEqual(result["mode"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(result["parameterChanges"], [])

    def test_30_events_descriptive_only(self):
        result = analyze([event(eventId=f"e{i}") for i in range(30)], {}, CONFIG)
        self.assertEqual(result["mode"], "DESCRIPTIVE_ONLY")
        self.assertFalse(result["autoApply"])

    def test_100_still_requires_walk_forward(self):
        result = analyze([event(eventId=f"e{i}") for i in range(100)], {}, CONFIG)
        self.assertEqual(result["mode"], "WALK_FORWARD_RESEARCH_REQUIRED")

    def test_event_observations_deduplicated(self):
        result = analyze([event(eventId="e1"), event(eventId="e1", eventVersion=2)], {}, CONFIG)
        self.assertEqual(result["independentEventCount"], 1)

    def test_nested_export_accepted(self):
        result = analyze([], {"ledger": {"events": [event()]}}, CONFIG)
        self.assertEqual(result["independentEventCount"], 1)

    def test_unmatched_sell_excluded(self):
        trades, errors = extract_closed_trades({"fills": [{"symbol": "600000.SH", "side": "SELL", "quantity": 100, "price": 10}]})
        self.assertEqual(trades, [])
        self.assertEqual(len(errors), 1)

    def test_drawdown_bins(self):
        self.assertEqual(drawdown_bin(10), "10-12%")
        self.assertEqual(drawdown_bin(18), "18%+")

    def test_backtest_reports_missing_data(self):
        result = readiness([event()])
        self.assertEqual(result["status"], "BLOCKED_MISSING_DATA")
        self.assertTrue(all(m["metrics"] is None for m in result["methods"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
