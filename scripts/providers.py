"""Public delayed quotes with bounded requests and explicit provenance.

No credentials, third-party Python packages, or inferred limit prices. The
Eastmoney main-board universe filter supplies segment metadata; code checks are
additional exclusions. Broker/account/announcements are never claimed verified.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib import parse, request

try:
    from .strategy import number
except ImportError:
    from strategy import number

CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
EASTMONEY_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com", "4.push2.eastmoney.com", "60.push2.eastmoney.com", "61.push2.eastmoney.com", "82.push2.eastmoney.com")
PUBLIC_UT = "bd1d9ddb04089700cf9c27f6f7426281"


class PublicProvider:
    def __init__(self, config: dict):
        self.config = config
        self.started = time.monotonic()
        self.deadline = self.started + config.get("maxRuntimeSeconds", 210)
        self.timeout = config.get("httpTimeoutSeconds", 7)
        self.errors: list[str] = []
        self.health: dict[str, dict] = {}
        self.eastmoney_history_failed = False

    def remaining(self) -> float:
        return max(0, self.deadline - time.monotonic())

    def raw(self, url: str) -> bytes:
        if self.remaining() <= 1:
            raise TimeoutError("Pipeline request budget exhausted")
        req = request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "application/json,text/plain,*/*", "Cache-Control": "no-cache"})
        with request.urlopen(req, timeout=min(self.timeout, self.remaining())) as response:
            return response.read()

    def json(self, url: str) -> dict:
        raw = self.raw(url).decode("utf-8", "replace").strip()
        if not raw.startswith("{"):
            match = re.search(r"\((\{.*\})\)\s*;?$", raw, re.S)
            raw = match.group(1) if match else raw
        return json.loads(raw)

    def record(self, source: str, ok: bool, note: str):
        self.health[source] = {"name": source, "ok": ok, "note": note}

    def error(self, context: str, exc: Exception):
        message = re.sub(r"\s+", " ", str(exc)).strip()[:180]
        self.errors.append(f"{context}: {message or type(exc).__name__}")

    def eastmoney(self, path: str, params: dict, *, host: str = "push2.eastmoney.com") -> dict:
        hosts = EASTMONEY_HOSTS if host == "push2.eastmoney.com" else (host,)
        last = None
        for base in hosts:
            if self.remaining() < 2:
                break
            try:
                payload = self.json(f"https://{base}/{path}?{parse.urlencode(params)}")
                if payload.get("data") is None:
                    raise ValueError("No data returned")
                self.record("Eastmoney", True, "Public delayed quote endpoint responded; metadata remains unverified")
                return payload
            except Exception as exc:
                last = exc
        raise RuntimeError(str(last or "No request budget"))

    def universe(self) -> list[dict]:
        """Paginate instead of requesting pz=5000 (servers may silently cap it)."""
        results = {}
        fields = "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f15,f16,f17,f18,f100,f124"
        # Separate provider-classified segments: prefixes alone never authorize one.
        for fs, segment in (("m:1+t:2", "SSE_MAIN"), ("m:0+t:6", "SZSE_MAIN"),
                            ("m:0+t:80", "CHINEXT"), ("m:1+t:23", "STAR")):
            page = 1
            while self.remaining() > 15:
                params = {"pn": page, "pz": 100, "po": 1, "np": 1, "ut": PUBLIC_UT,
                          "fltt": 2, "invt": 2, "fid": "f12", "fs": fs, "fields": fields}
                try:
                    payload = self.eastmoney("api/qt/clist/get", params)
                    data = payload.get("data") or {}
                    rows = data.get("diff") or []
                    if isinstance(rows, dict):
                        rows = list(rows.values())
                    for row in rows:
                        item = normalize_eastmoney(row, segment)
                        if item:
                            results[item["symbol"]] = item
                    total = int(data.get("total") or len(rows))
                    if not rows or page * 100 >= total:
                        break
                    page += 1
                except Exception as exc:
                    self.error(f"Eastmoney universe {segment} page {page}", exc)
                    if not results and self.remaining() > 12:
                        return self.sina_tencent_universe()
                    break
        if results:
            self.record("Eastmoney-universe", True, f"{len(results)} symbols received; pagination can be partial and is reported separately")
        else:
            self.record("Eastmoney-universe", False, "No current universe; previous snapshot retained")
        if not results and self.remaining() > 12:
            return self.sina_tencent_universe()
        return list(results.values())

    def sina_node(self, node: str) -> tuple[list[dict], bool]:
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        count_url = base + "Market_Center.getHQNodeStockCount?" + parse.urlencode({"node": node})
        try:
            count = int(self.raw(count_url).decode("utf-8").strip().strip('"'))
            if count <= 0 or count > 10000:
                raise ValueError("Invalid universe count")
            pages = (count + 79) // 80
            def page(index):
                url = base + "Market_Center.getHQNodeData?" + parse.urlencode({"node": node, "num": 80, "page": index, "sort": "symbol", "asc": 1})
                rows = json.loads(self.raw(url).decode("utf-8"))
                if not isinstance(rows, list):
                    raise ValueError("Sina page is not a quote list")
                return rows
            output = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(page, index) for index in range(1, pages + 1)]
                for future in as_completed(futures):
                    try:
                        for item in future.result():
                            if re.fullmatch(r"(?:sh|sz|bj)\d{6}", str(item.get("symbol", ""))):
                                output[item["symbol"]] = item
                    except Exception as exc:
                        self.error(f"Sina node {node} page", exc)
            return list(output.values()), len(output) >= count
        except Exception as exc:
            self.error(f"Sina node {node}", exc)
            return [], False

    def sina_tencent_universe(self) -> list[dict]:
        """Independent symbol enumeration plus Tencent timestamped executable prices.

        Explicit ChiNext/STAR membership sets are obtained separately. Only a
        complete pair of exclusion lists permits main-board classification; an
        unavailable board list produces UNKNOWN rather than prefix authorization.
        """
        rows, complete = self.sina_node("hs_a")
        if not rows:
            self.record("Sina-universe", False, "Public fallback enumeration unavailable")
            return []
        growth, growth_complete = self.sina_node("cyb")
        star, star_complete = self.sina_node("kcb")
        growth_symbols = {r["symbol"] for r in growth}
        star_symbols = {r["symbol"] for r in star}
        by_symbol = {}
        for row in rows:
            raw_symbol = row["symbol"]
            if not raw_symbol.startswith(("sh", "sz")):
                continue
            suffix = raw_symbol[:2].upper()
            symbol = f"{raw_symbol[2:]}.{suffix}"
            if raw_symbol in growth_symbols:
                segment = "CHINEXT"
            elif raw_symbol in star_symbols:
                segment = "STAR"
            elif growth_complete and star_complete:
                segment = "SSE_MAIN" if suffix == "SH" else "SZSE_MAIN"
            else:
                segment = "UNKNOWN"
            by_symbol[symbol] = {"symbol": symbol, "code": raw_symbol[2:], "name": str(row.get("name") or raw_symbol[2:]),
                                 "sector": "UNKNOWN", "segment": segment,
                                 "segmentSource": "Sina hs_a membership minus complete explicit cyb/kcb board membership; unverified metadata",
                                 "securityMetadataVerified": False, "announcementsVerified": False, "accountStateSynced": False}
        prices = self.tencent_quotes(sorted(by_symbol))
        output = [{**by_symbol[q["symbol"]], **q} for q in prices]
        self.record("Sina-universe", bool(output), f"Enumerated {len(rows)} symbols; complete={complete}; explicit board exclusions complete={growth_complete and star_complete}")
        if not complete:
            self.errors.append("Sina universe incomplete; missing previous symbols remain stale")
        if not growth_complete or not star_complete:
            self.errors.append("Sina explicit board membership incomplete; unknown segments cannot enter pool")
        return output

    def details(self, quote: dict) -> dict:
        if quote.get("limitPriceVerified") is True:
            return quote
        try:
            code = quote["code"]
            secid = f"{1 if quote['symbol'].endswith('.SH') else 0}.{code}"
            data = self.eastmoney("api/qt/stock/get", {"secid": secid, "ut": PUBLIC_UT, "fltt": 2,
                    "fields": "f43,f44,f45,f46,f47,f48,f51,f52,f57,f58,f60,f86,f127,f170"}).get("data") or {}
            upper, lower = number(data.get("f51")), number(data.get("f52"))
            out = {**quote, "limitUpPrice": upper, "limitDownPrice": lower,
                   "limitPriceSource": "Eastmoney reported daily upper/lower limit", "limitPriceVerified": bool(upper and lower and upper > lower > 0)}
            # Existing quote timestamp/scale are preserved; detail is used for actual limits.
            return out
        except Exception as exc:
            self.error(f"Daily limits {quote.get('symbol')}", exc)
            return {**quote, "limitPriceVerified": False}

    def history(self, symbol: str, limit: int = 160, *, adjusted: bool = True) -> list[dict]:
        code = symbol.split(".")[0]
        secid = f"{1 if symbol.endswith('.SH') else 0}.{code}"
        params = {"secid": secid, "ut": PUBLIC_UT, "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101",
                  "fqt": "1" if adjusted else "0", "end": "20500101", "lmt": limit}
        try:
            if self.eastmoney_history_failed:
                raise RuntimeError("Primary history source failed earlier this run; trying independent source")
            data = self.eastmoney("api/qt/stock/kline/get", params, host="push2his.eastmoney.com").get("data") or {}
            rows = []
            for line in data.get("klines") or []:
                p = line.split(",")
                if len(p) >= 7 and number(p[2], 0) > 0:
                    rows.append({"date": p[0], "open": number(p[1]), "close": number(p[2]), "high": number(p[3]), "low": number(p[4]), "volume": number(p[5]), "amount": number(p[6]), "adjusted": adjusted})
            return rows
        except Exception as exc:
            if not self.eastmoney_history_failed:
                self.error(f"Daily history {symbol}", exc)
            self.eastmoney_history_failed = True
            try:
                prefix = symbol.split(".")[1].lower()
                key = prefix + code
                mode = "qfq" if adjusted else ""
                query = parse.urlencode({"param": f"{key},day,,,{limit},{mode}"})
                last = None
                for base in ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"):
                    try:
                        data = self.json(base + query).get("data", {}).get(key, {})
                        if not data:
                            raise ValueError("No Tencent history data")
                        break
                    except Exception as request_exc:
                        last = request_exc
                else:
                    raise RuntimeError(str(last or "No Tencent history source"))
                lines = data.get("qfqday") if adjusted else data.get("day")
                if not lines:
                    lines = data.get("day") or data.get("qfqday") or []
                result = [{"date": p[0], "open": number(p[1]), "close": number(p[2]), "high": number(p[3]),
                           "low": number(p[4]), "volume": number(p[5]), "amount": None,
                           "adjusted": adjusted, "source": "Tencent historical daily"}
                          for p in lines if len(p) >= 6 and number(p[2], 0) > 0]
                if result:
                    self.record("Tencent-history", True, "Actual historical daily bars; no intraday confirmation")
                return result
            except Exception as fallback_exc:
                self.error(f"Tencent daily history {symbol}", fallback_exc)
                return []

    def calendar(self) -> tuple[list[str], str]:
        """Observed sessions from actual Shanghai/Shenzhen index daily bars."""
        for symbol in ("000001.SH", "399001.SZ"):
            rows = self.history(symbol, 700, adjusted=False)
            if rows:
                source = "Tencent" if rows[0].get("source", "").startswith("Tencent") else "Eastmoney"
                return sorted(set(row["date"] for row in rows)), f"{source} index actual daily-bar dates (observed, no future schedule)"
        # Independent Tencent history fallback returns actual bars, never weekdays.
        try:
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + parse.urlencode({"param": "sh000001,day,,,700,qfq"})
            data = self.json(url).get("data", {}).get("sh000001", {})
            rows = data.get("day") or data.get("qfqday") or []
            dates = sorted(set(str(row[0]) for row in rows if row and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row[0]))))
            if dates:
                self.record("Tencent-calendar", True, "Observed Shanghai index daily-bar dates")
                return dates, "Tencent index actual daily-bar dates (observed, no future schedule)"
        except Exception as exc:
            self.error("Tencent calendar", exc)
        return [], "unavailable"

    def tencent_quotes(self, symbols: list[str]) -> list[dict]:
        out = []
        batches = [symbols[start:start + 75] for start in range(0, len(symbols), 75)]
        def one(batch):
            codes = [s.split(".")[1].lower() + s.split(".")[0] for s in batch if s.endswith((".SH", ".SZ"))]
            raw = self.raw("https://qt.gtimg.cn/q=" + ",".join(codes)).decode("gb18030", "replace")
            return parse_tencent(raw)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one, batch) for batch in batches]
            for future in as_completed(futures):
                try:
                    out.extend(future.result())
                except Exception as exc:
                    self.error("Tencent fallback quotes", exc)
        self.record("Tencent", bool(out), f"{len(out)} requested symbols received with reported daily limits; security/account metadata unverified")
        return out

    def enrich_many(self, quotes: list[dict]) -> dict[str, tuple[dict, list[dict]]]:
        def one(q):
            return self.details(q), self.history(q["symbol"], self.config.get("minimumHistorySessions", 120) + 30)
        output = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(one, q): q for q in quotes}
            for future in as_completed(futures):
                q = futures[future]
                try:
                    output[q["symbol"]] = future.result()
                except Exception as exc:
                    self.error(f"Enrich {q['symbol']}", exc)
                    output[q["symbol"]] = (q, [])
        return output


def normalize_eastmoney(row: dict, segment: str) -> dict | None:
    code = str(row.get("f12") or "")
    price = number(row.get("f2"), 0)
    market = str(row.get("f13"))
    if not re.fullmatch(r"\d{6}", code) or price <= 0 or market not in ("0", "1"):
        return None
    stamp = number(row.get("f124"))
    timestamp = datetime.fromtimestamp(stamp, CN_TZ).isoformat() if stamp and stamp > 1_000_000_000 else None
    name = str(row.get("f14") or code)
    return {"symbol": f"{code}.{'SH' if market == '1' else 'SZ'}", "code": code, "name": name,
            "segment": segment, "segmentSource": "Eastmoney market+security-type universe filter", "sector": str(row.get("f100") or "UNKNOWN"),
            "price": price, "pct": number(row.get("f3")), "high": number(row.get("f15")), "low": number(row.get("f16")),
            "open": number(row.get("f17")), "preClose": number(row.get("f18")), "amount": number(row.get("f6")),
            "turnover": number(row.get("f8")), "quoteTimestamp": timestamp, "source": "Eastmoney",
            "isST": "ST" in name.upper() or "退" in name,
            "securityMetadataVerified": False, "announcementsVerified": False,
            "accountStateSynced": False, "stale": timestamp is None}


def parse_tencent(raw: str) -> list[dict]:
    out = []
    for prefix, code, payload in re.findall(r'v_(sh|sz)(\d{6})="([^"]*)"', raw):
        p = payload.split("~")
        if len(p) < 35 or number(p[3], 0) <= 0:
            continue
        stamp = None
        if re.fullmatch(r"\d{14}", p[30]):
            stamp = datetime.strptime(p[30], "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ).isoformat()
        upper = number(p[47]) if len(p) > 48 else None
        lower = number(p[48]) if len(p) > 48 else None
        out.append({"symbol": f"{code}.{prefix.upper()}", "code": code, "name": p[1], "price": number(p[3]),
                    "preClose": number(p[4]), "open": number(p[5]), "pct": number(p[32]),
                    "high": number(p[33]), "low": number(p[34]), "quoteTimestamp": stamp,
                    "amount": number(p[37]) * 10000 if len(p) > 37 and number(p[37]) is not None else None,
                    "source": "Tencent", "stale": stamp is None,
                    "limitUpPrice": upper, "limitDownPrice": lower,
                    "limitPriceVerified": bool(upper and lower and upper > lower > 0),
                    "limitPriceSource": "Tencent reported daily upper/lower limit fields 47/48",
                    "securityMetadataVerified": False, "announcementsVerified": False,
                    "accountStateSynced": False})
    return out
