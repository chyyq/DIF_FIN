"""Strict research readiness report; never reconstruct unavailable intraday fills.

The three methods require timestamped decisions and actual/executable fill data.
Daily high/low bars alone cannot prove signal ordering, absorption or execution.
This entry point reports missing inputs rather than inventing an A/B/C backtest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .update_data import ROOT, load_json, write_json
except ImportError:
    from update_data import ROOT, load_json, write_json


def readiness(events: list[dict]) -> dict:
    checks = {
        "eventUniverse": bool(events),
        "timestampedFiveMinuteBaselines": bool(events) and all(e.get("intradayBars") for e in events),
        "decisionTimeHistoricalMetadata": bool(events) and all(e.get("asOfMetadataVerified") is True for e in events),
        "executableFillAndFeeModel": bool(events) and all(e.get("executionSimulationVerified") is True for e in events),
        "timeOrderedSplit": len(events) >= 100,
    }
    return {"status": "RESEARCH_INPUTS_PRESENT_REQUIRES_REVIEW" if all(checks.values()) else "BLOCKED_MISSING_DATA",
            "eventCount": len(events), "checks": checks,
            "methods": [{"id": "A", "definition": "Price touch only; research baseline, no auto orders", "metrics": None},
                        {"id": "B", "definition": "A + same-slot volume contraction + no new low", "metrics": None},
                        {"id": "C", "definition": "B + VWAP/rebound reclaim within entry cap", "metrics": None}],
            "requiredGroups": ["T0 / T1-T6", "8-10 / 10-12 / 12-14 / 14-16 / 16-18 / 18+%", "sector", "1/2/3/5/7 holding sessions"],
            "parameterOptimization": False,
            "message": "No A/B/C performance metrics are published without verified intraday, point-in-time metadata and executable fill assumptions. This is a data-readiness report, not a completed backtest."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=ROOT / "data/events.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/backtest-readiness.json")
    args = parser.parse_args()
    source = load_json(args.events, {"events": []})
    result = readiness(source.get("events", []) if isinstance(source, dict) else source)
    write_json(args.output, result)
    print(json.dumps({"status": result["status"], "eventCount": result["eventCount"]}))
