#!/usr/bin/env python3
"""
BTC／MSTR 指標監控（第二階段）

每次執行會：
1. 從 Coinbase 抓 BTC-USD K 線，合成 4H／日／週
2. 從 SEC 自動解析 Strategy 的 8-K 和季報，推算持倉，算 mNAV 與 Net BPS
3. 抓 ETF 資金流（Farside）、IBIT 成交量、BTC.D（CoinGecko）
4. 檢查價位、放量突破、mNAV 門檻與背離、巨量發行、可轉債，必要時發 Telegram
5. 輸出 docs/data.json 給網頁讀，SEC 解析結果存在 data/

用法：
  python3 monitor.py            正式執行
  python3 monitor.py --dry-run  照常抓數據，但警報只印出來、不發 Telegram
  python3 monitor.py --demo     不連網，用模擬數據產生 docs/data.json（本機預覽用）

只用 Python 標準函式庫，不需要 pip install。
"""

import argparse
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

import sec_strategy as sec

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
DATA_PATH = os.path.join(ROOT, "docs", "data.json")
STRATEGY_STORE = os.path.join(ROOT, "data", "strategy.json")
BTCD_STORE = os.path.join(ROOT, "data", "btcd.json")
ETF_STORE = os.path.join(ROOT, "data", "etf.json")

UTC = dt.timezone.utc
TPE = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; btc-mstr-monitor/1.0)"
CB_BASE = "https://api.exchange.coinbase.com/products/BTC-USD"

H1, H4, D1 = 3600, 4 * 3600, 86400
W1 = 7 * D1


# ---------------------------------------------------------------- 小工具

def now_ts():
    return int(time.time())


def iso_utc(ts):
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tpe_str(ts, fmt="%Y/%m/%d %H:%M"):
    return dt.datetime.fromtimestamp(ts, TPE).strftime(fmt)


def utc_date(ts):
    return dt.datetime.fromtimestamp(ts, UTC).date()


def date_ts(d):
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def usd(x, digits=0):
    return f"{x:,.{digits}f}"


def pct(a, b):
    """a 相對 b 的百分比差"""
    return (a / b - 1) * 100


def http_get(url, accept="application/json", timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # 網路或 HTTP 錯誤都重試
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]} 讀取失敗：{last}")


def http_json(url, **kw):
    return json.loads(http_get(url, **kw))


def http_post_json(url, payload, headers=None, timeout=20):
    h = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj, compact=False):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- BTC（Coinbase）

def cb_candles(granularity, count):
    """抓 Coinbase K 棒，由舊到新。每次請求上限約 300 根，所以分段往回抓。"""
    bars = {}
    end = now_ts()
    remaining = count
    while remaining > 0:
        n = min(290, remaining)
        start = end - n * granularity
        q = urllib.parse.urlencode({
            "granularity": granularity, "start": iso_utc(start), "end": iso_utc(end)})
        rows = http_json(f"{CB_BASE}/candles?{q}")
        if isinstance(rows, dict):
            raise RuntimeError(f"Coinbase 回傳錯誤：{rows.get('message', rows)}")
        if not rows:
            break
        for t, low, high, op, close, vol in rows:
            bars[int(t)] = {"time": int(t), "open": float(op), "high": float(high),
                            "low": float(low), "close": float(close), "volume": float(vol)}
        end = start
        remaining -= n
        time.sleep(0.35)  # 避免撞到速率限制
    return [bars[k] for k in sorted(bars)]


def cb_ticker():
    return float(http_json(f"{CB_BASE}/ticker")["price"])


def bucket_week(ts):
    """週線以週一 00:00 UTC 開始，跟 TradingView 一樣"""
    d = utc_date(ts)
    return date_ts(d - dt.timedelta(days=d.weekday()))


def bucket_h4(ts):
    return ts - ts % H4


def aggregate(bars, bucket):
    out = []
    for b in bars:
        k = bucket(b["time"])
        if out and out[-1]["time"] == k:
            o = out[-1]
            o["high"] = max(o["high"], b["high"])
            o["low"] = min(o["low"], b["low"])
            o["close"] = b["close"]
            o["volume"] += b["volume"]
        else:
            out.append(dict(b, time=k))
    # 資料如果從區間中間開始，第一根不完整，丟掉
    if out and bars and out[0]["time"] < bars[0]["time"]:
        out.pop(0)
    return out


def closed_only(bars, period, now):
    return [b for b in bars if b["time"] + period <= now]


def round_bars(bars):
    return [{"time": b["time"], "open": round(b["open"], 2), "high": round(b["high"], 2),
             "low": round(b["low"], 2), "close": round(b["close"], 2),
             "volume": round(b["volume"], 2)} for b in bars]


# ---------------------------------------------------------------- MSTR

def mstr_yahoo():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=2y&interval=1d"
    res = http_json(url)["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    hist = [{"date": utc_date(t).isoformat(), "close": round(float(c), 4)}
            for t, c in zip(ts, closes) if c is not None]
    price = meta.get("regularMarketPrice")
    if price is None and hist:
        price = hist[-1]["close"]
    if price is None:
        raise RuntimeError("Yahoo 沒有回傳 MSTR 價格")
    return {"price": float(price), "time": int(meta.get("regularMarketTime") or now_ts()),
            "source": "Yahoo（約延遲 15 分鐘）", "history": hist}


def mstr_official_latest():
    """官方市場最新成交（含盤前 04:00 與盤後到 20:00 ET），Yahoo 一分鐘線"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=1d&interval=1m&includePrePost=true"
    r = http_json(url)["chart"]["result"][0]
    ts = r.get("timestamp") or []
    closes = r["indicators"]["quote"][0].get("close") or []
    for t, c in reversed(list(zip(ts, closes))):
        if c is not None:
            return {"price": float(c), "time": int(t)}
    raise RuntimeError("Yahoo 一分鐘線沒有資料")


def perp_binance():
    j = http_json("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=MSTRUSDT", timeout=10, retries=0)
    return {"venue": "Binance MSTRUSDT 永續", "price": float(j["lastPrice"]),
            "volume_24h_usd": float(j.get("quoteVolume") or 0), "time": int(j["closeTime"]) // 1000}


def perp_hyperliquid():
    meta, ctxs = http_post_json("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
    for u, c in zip(meta.get("universe", []), ctxs):
        if u.get("name") in ("xyz:MSTR", "MSTR"):
            px = c.get("midPx") or c.get("markPx")
            return {"venue": "Hyperliquid xyz:MSTR 永續", "price": float(px),
                    "volume_24h_usd": float(c.get("dayNtlVlm") or 0), "time": now_ts()}
    raise RuntimeError("Hyperliquid 找不到 xyz:MSTR")


def mstr_quote(cfg, errors):
    """官方市場有最新成交就用官方價（最合規）；休市時改用 24 小時永續合約中成交量最大的一家"""
    mc = cfg.get("mstr_24h", {})
    fresh_s = float(mc.get("official_fresh_minutes", 20)) * 60
    hist = None
    for fn in (mstr_yahoo, mstr_stooq):
        try:
            hist = fn()
            break
        except Exception as e:
            errors.append(f"MSTR（{fn.__name__}）：{e}")
    official = None
    try:
        official = mstr_official_latest()
    except Exception:
        if hist:
            official = {"price": hist["price"], "time": hist["time"]}
    perps = []
    if mc.get("enabled", True):
        for name in mc.get("venues", ["binance", "hyperliquid"]):
            fn = {"binance": perp_binance, "hyperliquid": perp_hyperliquid}.get(name)
            if not fn:
                continue
            try:
                perps.append(fn())
            except Exception:
                pass  # 某家連不上（例如 Binance 擋美國伺服器）就略過
    perp = max(perps, key=lambda p: p["volume_24h_usd"]) if perps else None
    if not official and not perp:
        return None
    now = now_ts()
    if official and now - official["time"] <= fresh_s:
        price, kind, source, t = official["price"], "official", "Nasdaq 官方成交（Yahoo，含盤前盤後）", official["time"]
    elif perp:
        price, kind, source, t = perp["price"], "perp", f"{perp['venue']}（美股休市，24 小時參考價）", perp["time"]
    else:
        price, kind, source, t = official["price"], "official_stale", "Nasdaq 官方最後成交（休市中）", official["time"]
    out = {"price": price, "time": t, "source": source, "kind": kind,
           "history": (hist or {}).get("history", []), "official": official, "perp": perp, "perps": perps}
    if official and perp:
        out["premium_pct"] = round((perp["price"] / official["price"] - 1) * 100, 2)
    return out


def mstr_stooq():
    txt = http_get("https://stooq.com/q/l/?s=mstr.us&f=sd2t2ohlcv&h&e=csv", accept="text/csv")
    lines = [line for line in txt.strip().splitlines() if line.strip()]
    rec = dict(zip(lines[0].lower().split(","), lines[1].split(",")))
    price = float(rec["close"])  # 沒資料時是 N/D，會丟 ValueError
    return {"price": price, "time": now_ts(), "source": "Stooq（延遲報價）", "history": []}


# ---------------------------------------------------------------- Strategy 指標

def pick_snapshot(snaps, date_str, allow_backfill=False):
    """找出在 date_str 當天有效的持倉快照（as_of <= date_str 中最新的一筆）"""
    ordered = sorted(snaps, key=lambda s: s["as_of"])
    best = None
    for s in ordered:
        if s["as_of"] <= date_str:
            best = s
    if best is None and allow_backfill and ordered:
        best = ordered[0]
    return best


def strategy_metrics(s, btc, mstr):
    pref = sum(float(v) for v in s.get("preferred_usd", {}).values())
    debt, cash = float(s["debt_usd"]), float(s["cash_usd"])
    shares, hold = float(s["shares_outstanding"]), float(s["btc_holdings"])
    btc_val = hold * btc
    net_val = btc_val - debt - pref + cash          # 簡報 p.224 的淨 BTC 市值
    net_bps_usd = net_val / shares                   # p.228
    mcap = mstr * shares
    ev = mcap + debt + pref - cash
    return {
        "btc_value": round(btc_val),
        "net_btc_value": round(net_val),
        "preferred_total": round(pref),
        "market_cap": round(mcap),
        "net_bps_usd": round(net_bps_usd, 2),
        "net_bps_sats": round(net_val / btc / shares * 1e8),
        "bps_sats": round(hold / shares * 1e8),
        "mnav_deck": round(mstr / net_bps_usd, 4) if net_bps_usd > 0 else None,  # p.229
        "mnav_old": round(mcap / btc_val, 4),        # p.219 舊算法
        "mnav_ev": round(ev / btc_val, 4),           # 企業價值算法，跟官方儀表板同一類
    }


def mnav_history(mstr_hist, btc_daily, snapshot_for):
    """snapshot_for(date_str) 回傳那天有效的持倉快照（或 None）"""
    btc_by_date = {utc_date(b["time"]).isoformat(): b["close"] for b in btc_daily}
    out = []
    for row in mstr_hist:
        btc = btc_by_date.get(row["date"])
        s = snapshot_for(row["date"]) if btc else None
        if not s:
            continue
        m = strategy_metrics(s, btc, row["close"])["mnav_deck"]
        if m:
            out.append({"time": date_ts(dt.date.fromisoformat(row["date"])), "value": m})
    return out


# ---------------------------------------------------------------- 價位、量能、時間窗口

def level_view(levels, price):
    lv = sorted(levels, key=lambda x: x["price"])
    below = [lev for lev in lv if lev["price"] <= price]
    above = [lev for lev in lv if lev["price"] > price]
    return {
        "below": below[-1] if below else None,
        "above": above[0] if above else None,
        "distances": [{"name": lev["name"], "price": lev["price"],
                       "pct": round(pct(lev["price"], price), 2)} for lev in lv],
    }


def volume_ratio(closed, n=20):
    if len(closed) < n + 1:
        return None
    avg = sum(b["volume"] for b in closed[-n - 1:-1]) / n
    return round(closed[-1]["volume"] / avg, 2) if avg else None


def last_friday(y, m):
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    d = nxt - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return d


def quarter_windows(cfg, today):
    qw = cfg.get("quarter_windows", {})
    before = int(qw.get("rollover_days_before_expiry", 14))
    follow = [int(x) for x in qw.get("followup_days_of_month", [10, 20])]
    windows = []
    for y in range(today.year - 2, today.year + 2):
        for m in (3, 6, 9, 12):
            exp = last_friday(y, m)
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            windows.append({
                "quarter": f"{y} Q{m // 3}",
                "rollover": (exp - dt.timedelta(days=before)).isoformat(),
                "expiry": exp.isoformat(),
                "followups": [dt.date(ny, nm, d).isoformat() for d in follow],
            })

    events = []
    for w in windows:
        events.append({"date": w["rollover"], "label": "換季日"})
        events.append({"date": w["expiry"], "label": "交割日"})
        for f in w["followups"]:
            events.append({"date": f, "label": f"{int(f[-2:])} 日"})
    t = today.isoformat()
    upcoming = sorted([e for e in events if e["date"] >= t], key=lambda e: e["date"])[:4]
    for e in upcoming:
        e["days"] = (dt.date.fromisoformat(e["date"]) - today).days

    current = None
    for w in windows:
        end = w["followups"][-1] if w["followups"] else w["expiry"]
        if w["rollover"] <= t <= end:
            marks = [("換季日", w["rollover"]), ("交割日", w["expiry"])] + \
                    [(f"{int(f[-2:])} 日", f) for f in w["followups"]]
            phase = [lab for lab, d in marks if d <= t][-1]
            current = {"quarter": w["quarter"], "phase_from": phase}
    # 網頁畫底色只需要最近兩年
    cutoff = (today - dt.timedelta(days=760)).isoformat()
    return {"list": [w for w in windows if w["expiry"] >= cutoff],
            "upcoming": upcoming, "current": current}


# ---------------------------------------------------------------- 警報

class Alerter:
    def __init__(self, state, dry_run):
        self.state = state
        self.dry_run = dry_run
        self.state.setdefault("sent", {})
        self.state.setdefault("log", [])

    def seen(self, key):
        return key in self.state["sent"]

    def cooldown_ok(self, key, hours):
        t = self.state["sent"].get(key)
        return t is None or now_ts() - t >= hours * 3600

    def push(self, key, text):
        ok = telegram_send(text, self.dry_run)
        if ok or self.dry_run or not telegram_configured():
            # 沒設定 Telegram 時也記錄，避免每次重複印同一則
            self.state["sent"][key] = now_ts()
            self.state["log"].insert(0, {"time": now_ts(), "text": text})
            del self.state["log"][30:]

    def prune(self, days=45):
        cut = now_ts() - days * D1
        self.state["sent"] = {k: v for k, v in self.state["sent"].items() if v >= cut}


def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def telegram_send(text, dry_run=False):
    if dry_run or not telegram_configured():
        print("—— 警報（未發送）——\n" + text + "\n")
        return False
    data = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
        "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        print(f"Telegram 發送失敗：{e}", file=sys.stderr)
        return False


def check_level_crosses(alerter, levels, closed, tf_label, tf_key):
    """用「已收盤」的 K 棒判斷跌破／站上，避免盤中插針誤報"""
    if len(closed) < 2:
        return
    prev, last = closed[-2], closed[-1]
    for lev in levels:
        p = lev["price"]
        if prev["close"] >= p > last["close"]:
            direction, verb = "down", "收盤跌破"
        elif prev["close"] < p <= last["close"]:
            direction, verb = "up", "收盤站上"
        else:
            continue
        key = f"cross:{tf_key}:{last['time']}:{p}:{direction}"
        if alerter.seen(key):
            continue
        alerter.push(key, (
            f"BTC {tf_label}{verb}{lev['name']} {usd(p)}\n"
            f"收盤 {usd(last['close'])}，前一根 {usd(prev['close'])}\n"
            f"K 棒開始於 {tpe_str(last['time'])}（台北）"))


def check_approach(alerter, levels, price, threshold, cooldown_h):
    for lev in levels:
        dist = abs(pct(price, lev["price"]))
        if dist > threshold:
            continue
        key = f"near:{lev['price']}"
        if alerter.cooldown_ok(key, cooldown_h):
            side = "上方" if price >= lev["price"] else "下方"
            alerter.push(key, (
                f"BTC 接近{lev['name']} {usd(lev['price'])}\n"
                f"現價 {usd(price)}，在價位{side} {dist:.2f}%"))


def check_mnav(alerter, state, m, thresholds, btc, mstr, note=""):
    last = state.get("last_mnav")
    if m is not None and last is not None:
        for th in thresholds:
            if last >= th > m:
                direction, verb = "down", "跌破"
            elif last < th <= m:
                direction, verb = "up", "升破"
            else:
                continue
            key = f"mnav:{th}:{direction}"
            if alerter.cooldown_ok(key, 12):
                alerter.push(key, (
                    f"mNAV {verb} {th}（簡報算法）\n"
                    f"目前 {m:.3f}，MSTR {mstr:.2f}{note}，BTC {usd(btc)}"))
    if m is not None:
        state["last_mnav"] = m


# ---------------------------------------------------------------- 排行訊號


# ---------------------------------------------------------------- 其他數據來源

def eur_usd_rate(default):
    try:
        res = http_json("https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X?range=5d&interval=1d")
        return float(res["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return float(default)


def _paren_num(s):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "—", "–"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _farside_rows_from_markdown(md):
    head, rows = None, []
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if "Total" in cells and "IBIT" in cells:
            head = cells
            continue
        if head and cells and re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}", cells[0]):
            i_t = max(i for i, c in enumerate(head) if c == "Total")
            i_i = head.index("IBIT")
            if len(cells) > i_t and _paren_num(cells[i_t]) is not None:
                d = dt.datetime.strptime(cells[0], "%d %b %Y").date().isoformat()
                rows.append({"date": d, "total": _paren_num(cells[i_t]), "ibit": _paren_num(cells[i_i])})
    return rows


def _find_flow_rows(obj):
    """在 SoSoValue 回傳的 JSON 裡找出「日期 + 淨流入」的列，不依賴固定欄位名稱"""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = obj[0].keys()
        dk = next((k for k in keys if k.lower() in ("date", "trade_date", "tradedate", "day")), None)
        nk = next((k for k in keys if re.search(r"net.?inflow", k, re.I) and not re.search(r"cum", k, re.I)), None)
        if dk and nk:
            return obj, dk, nk
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find_flow_rows(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_flow_rows(v)
            if r:
                return r
    return None


def sosovalue_flows(key):
    base = "https://openapi.sosovalue.com/openapi/v1/etfs/summary-history?symbol=BTC&country_code=US&limit="
    h = {"x-soso-api-key": key}
    attempts = [
        lambda: http_get_json_h(base + "1000", h),
        lambda: http_post_json("https://openapi.sosovalue.com/api/v1/etf/historicalInflowChart",
                               {"type": "us-btc-spot"}, h),
        lambda: http_get_json_h(base + "300", h),
        lambda: http_get_json_h(base + "40", h),
    ]
    best, last = [], None
    for a in attempts:
        try:
            found = _find_flow_rows(a())
            if not found:
                raise RuntimeError("回傳格式找不到淨流入欄位")
            rows, dk, nk = found
            keys = rows[0].keys()
            ak = next((k for k in keys if re.search(r"(total)?.?net.?assets?|aum", k, re.I)
                       and not re.search(r"inflow", k, re.I)), None)
            out = []
            for r in rows:
                v = r.get(nk)
                if v is None:
                    continue
                raw = str(r.get(dk))
                d = raw[:10]
                if re.fullmatch(r"\d{13}", raw):
                    d = dt.datetime.fromtimestamp(int(raw) / 1000, UTC).date().isoformat()
                elif re.fullmatch(r"\d{10}", raw):
                    d = dt.datetime.fromtimestamp(int(raw), UTC).date().isoformat()
                rec = {"date": d, "total": round(float(v) / 1e6, 1), "ibit": None}
                if ak and r.get(ak) not in (None, ""):
                    try:
                        rec["aum_b"] = round(float(r[ak]) / 1e9, 2)
                    except (TypeError, ValueError):
                        pass
                out.append(rec)
            out.sort(key=lambda x: x["date"])
            if len(out) > len(best):
                best = out
            if len(best) >= 250:  # 拿到夠長的歷史就不用再試
                break
        except Exception as e:
            last = e
    if best:
        return best
    raise RuntimeError(f"SoSoValue：{str(last)[:60]}")


def http_get_json_h(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def etf_flows():
    errs = []
    key = os.environ.get("SOSOVALUE_API_KEY", "").strip()
    if key:
        try:
            return sosovalue_flows(key), "SoSoValue"
        except Exception as e:
            errs.append(str(e)[:70])
    try:
        return farside_flows(), "Farside"
    except Exception:
        errs.append("Farside 擋掉了 GitHub 的伺服器")
    raise RuntimeError("；".join(errs))


def farside_flows():
    """Farside 的比特幣現貨 ETF 每日資金流（百萬美元）。直接抓被擋時，改用 r.jina.ai 轉抓。"""
    try:
        return _farside_direct()
    except Exception as e:
        first = str(e)[:60]
    try:
        md = http_get("https://r.jina.ai/https://farside.co.uk/btc/", accept="text/plain", timeout=40)
        rows = _farside_rows_from_markdown(md)
        if rows:
            return rows
        raise RuntimeError("轉抓結果裡找不到資金流表")
    except Exception as e2:
        raise RuntimeError(f"直接抓：{first}；轉抓：{str(e2)[:60]}")


def _farside_direct():
    html = http_get("https://farside.co.uk/btc/", accept="text/html")
    tables, _ = sec.parse_html(html)
    for t in tables:
        head = next((r for r in t if any(c.strip() == "Total" for c in r)
                     and any(c.strip() == "IBIT" for c in r)), None)
        if not head:
            continue
        i_total = max(i for i, c in enumerate(head) if c.strip() == "Total")
        i_ibit = next(i for i, c in enumerate(head) if c.strip() == "IBIT")
        rows = []
        for r in t:
            if not r or not re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}", r[0].strip()):
                continue
            if len(r) <= i_total:
                continue
            total = _paren_num(r[i_total])
            if total is None:
                continue
            d = dt.datetime.strptime(r[0].strip(), "%d %b %Y").date().isoformat()
            rows.append({"date": d, "total": total, "ibit": _paren_num(r[i_ibit])})
        if rows:
            return rows
    raise RuntimeError("Farside 頁面格式變了，找不到資金流表")


def ibit_volume_ratio():
    res = http_json("https://query1.finance.yahoo.com/v8/finance/chart/IBIT?range=3mo&interval=1d")
    r = res["chart"]["result"][0]
    vols = [v for v in (r["indicators"]["quote"][0].get("volume") or []) if v]
    ts = r.get("timestamp") or []
    # 美股盤中時最後一根還沒收完，先拿掉
    if ts and utc_date(ts[-1]) == dt.datetime.now(UTC).date() and dt.datetime.now(UTC).hour < 21:
        vols = vols[:-1]
    if len(vols) < 21:
        return None
    avg = sum(vols[-21:-1]) / 20
    return round(vols[-1] / avg, 2) if avg else None


def btc_dominance():
    j = http_json("https://api.coingecko.com/api/v3/global")
    return round(float(j["data"]["market_cap_percentage"]["btc"]), 2)


# ---------------------------------------------------------------- 放量突破、背離

def check_breakout(alerter, closed, tf_key, tf_label, cfg):
    bc = cfg.get("breakout", {})
    n = int(bc.get("lookback", {}).get(tf_key, 20))
    need_vol = float(bc.get("volume_ratio", 1.5))
    need_body = float(bc.get("body_ratio", 0.5))
    if len(closed) < max(n, 20) + 1:
        return None
    last, prev = closed[-1], closed[-n - 1:-1]
    hi, lo = max(b["high"] for b in prev), min(b["low"] for b in prev)
    ratio = volume_ratio(closed) or 0
    rng = last["high"] - last["low"]
    body = abs(last["close"] - last["open"]) / rng if rng else 0
    status = None
    if last["close"] > hi and last["close"] > last["open"] and ratio >= need_vol and body >= need_body:
        status = "up"
    elif last["close"] < lo and last["close"] < last["open"] and ratio >= need_vol and body >= need_body:
        status = "down"
    if status:
        key = f"brk:{tf_key}:{last['time']}:{status}"
        if not alerter.seen(key):
            what = f"放量長陽突破 {n} 根區間高點 {usd(hi)}" if status == "up" else \
                   f"放量長陰跌破 {n} 根區間低點 {usd(lo)}"
            alerter.push(key, (f"BTC {tf_label}{what}\n"
                               f"收盤 {usd(last['close'])}，成交量 {ratio:.1f}× 均量，實體佔 {body * 100:.0f}%"))
    return {"lookback": n, "range_high": round(hi, 2), "range_low": round(lo, 2),
            "volume_ratio": round(ratio, 2), "body_ratio": round(body, 2), "status": status}


def check_divergence(alerter, w1_closed, mnav_hist, cfg):
    dc = cfg.get("divergence", {})
    n = int(dc.get("lookback_weeks", 26))
    gap = float(dc.get("mnav_gap_pct", 10))
    k = int(dc.get("crash_weeks", 4))
    crash_m, crash_b = float(dc.get("crash_mnav_pct", 20)), float(dc.get("crash_btc_pct", 5))
    wk = {}
    for p in mnav_hist:
        wk[bucket_week(p["time"])] = p["value"]  # 同一週取最後一天
    s = [(b["time"], b["close"], wk[b["time"]]) for b in w1_closed if b["time"] in wk]
    out = {"weeks": len(s), "need": n + 1, "status": None}
    if len(s) < n + 1:
        return out
    t, btc, m = s[-1]
    prev = s[-n - 1:-1]
    m_max = max(x[2] for x in prev)
    out["btc_new_high"] = btc >= max(x[1] for x in prev)
    out["mnav_vs_max_pct"] = round((m / m_max - 1) * 100, 1)
    if out["btc_new_high"] and m <= m_max * (1 - gap / 100):
        out["status"] = "top"
        key = f"div:top:{t}"
        if not alerter.seen(key):
            alerter.push(key, (f"mNAV 與 BTC 週線頂背離\nBTC 週收盤 {usd(btc)} 創 {n} 週新高，"
                               f"mNAV {m:.2f} 比 {n} 週最高 {m_max:.2f} 低 {abs(out['mnav_vs_max_pct']):.0f}%\n"
                               "簡報解讀：BTC 頂部訊號，MSTR 出場、BTC 準備出場"))
    if len(s) >= k + 1:
        _, btc0, m0 = s[-k - 1]
        m_chg, b_chg = (m / m0 - 1) * 100, (btc / btc0 - 1) * 100
        out["mnav_chg_pct"], out["btc_chg_pct"] = round(m_chg, 1), round(b_chg, 1)
        if m_chg <= -crash_m and b_chg > -crash_b:
            out["status"] = out["status"] or "crash"
            key = f"div:crash:{t}"
            if not alerter.seen(key):
                alerter.push(key, (f"mNAV 雪崩\n{k} 週內 mNAV {m_chg:+.0f}%，同期 BTC 只有 {b_chg:+.1f}%\n"
                                   "簡報解讀：mNAV 先於 BTC 轉弱"))
    return out


# ---------------------------------------------------------------- Strategy 申報警報

def strategy_alerts(alerter, new_filings, weeks, cfg, metrics, today):
    iss = cfg.get("issuance", {})
    massive_mstr = float(iss.get("massive_mstr_weekly_usd_m", 1000))
    massive_conv = float(iss.get("massive_convert_usd_m", 1000))
    for f in sorted(new_filings, key=lambda x: x["filed"]):
        if (today - dt.date.fromisoformat(f["filed"])).days > 3:
            continue  # 補抓的舊申報不發通知
        key_base = f.get("url", f["filed"])
        if f.get("is_update") and f.get("period_end"):
            atm, rep, btc = f.get("atm", {}), f.get("repurchase", {}), f.get("btc", {})
            lines = [f"Strategy 週報（{f.get('period_start', '')[5:]}～{f['period_end'][5:]}）"]
            if btc:
                act = f"買入 {btc.get('purchased', 0):,.0f}" if btc.get("purchased") else "無買入"
                if btc.get("sold"):
                    act += f"、賣出 {btc['sold']:,.0f}"
                lines.append(f"BTC：{act}，持有 {btc.get('holdings', 0):,.0f}")
            ms = atm.get("MSTR", {})
            if ms.get("shares"):
                lines.append(f"MSTR 增發 {ms['shares']:,.0f} 股（${ms.get('proceeds_m', 0):,.1f}M）")
            for s in sec.SERIES:
                a = atm.get(s, {})
                if a.get("proceeds_m"):
                    lines.append(f"{s} 發行 ${a['proceeds_m']:,.1f}M")
                r = rep.get(s, {})
                if r.get("shares"):
                    lines.append(f"{s} 回購 {r['shares']:,.0f} 股（${r.get('price_m', 0):,.1f}M）")
            if rep.get("MSTR", {}).get("shares"):
                lines.append(f"MSTR 回購 {rep['MSTR']['shares']:,.0f} 股（${rep['MSTR'].get('price_m', 0):,.1f}M）")
            if "usd_reserve" in f:
                lines.append(f"USD Reserve ${f['usd_reserve'] / 1e9:.2f}B、USD Cash ${f.get('usd_cash', 0) / 1e6:,.0f}M")
            if metrics.get("mnav_deck") is not None:
                lines.append(f"mNAV {metrics['mnav_deck']:.2f}（已用新數據重算）")
            alerter.push(f"8k:{key_base}", "\n".join(lines))
            if ms.get("proceeds_m", 0) >= massive_mstr:
                alerter.push(f"massive:{key_base}", (
                    f"⚠️ 巨量發普通股：本週 MSTR 增發 ${ms['proceeds_m']:,.0f}M（門檻 ${massive_mstr:,.0f}M）\n"
                    "簡報解讀：mNAV 頂部訊號，MSTR 分批出場"))
        for c in f.get("converts", []):
            if c.get("kind") not in ("pricing", "closing"):
                continue
            big = c["principal"] / 1e6 >= massive_conv
            note = ("簡報解讀：巨量發可轉債＝mNAV 最頂，MSTR 出場一半" if big
                    else "簡報解讀：發可轉債代表債市看好 MSTR，mNAV 將上升")
            alerter.push(f"conv:{key_base}", (
                f"{'⚠️ 巨量' if big else ''}Strategy 可轉債{'定價' if c['kind'] == 'pricing' else '完成發行'}："
                f"${c['principal'] / 1e9:.2f}B {c.get('notes', '')}\n{note}\n"
                "負債數字要等下一份季報才會自動更新，急的話在 config.json 填 debt_override"))
        if f.get("looks_like_update") and f.get("warnings"):
            alerter.push(f"warn:{key_base}", (
                "Strategy 8-K 自動解析有問題：" + "；".join(f["warnings"]) +
                f"\n這週的數字可能沒算進去，請打開原文確認：{f.get('url', '')}"))


# ---------------------------------------------------------------- 排行訊號

def _money_m(v, signed=True):
    sign = ("+" if v > 0 else ("−" if v < 0 else "")) if signed else ""
    v = abs(v)
    return f"{sign}${v / 1000:,.2f}B" if v >= 1000 else f"{sign}${v:,.0f}M"


def build_signals(m, lv, vol_d, vol_w, windows, levels, fin, div, brk, etf, btcd):
    sig = []

    if fin.get("weeks"):
        w = fin["weeks"][-1]
        ms = w["mstr_proceeds_m"]
        value = f"MSTR 增發 {_money_m(ms, False)}" if ms else "本週無增發"
        parts = [f"{(w.get('period_start') or '')[5:].replace('-', '/')}～{w['period_end'][5:].replace('-', '/')}"]
        if fin.get("pct_rank") is not None and ms:
            parts.append(f"過去 {len(fin['weeks'])} 週第 {fin['pct_rank']} 百分位")
        if w["pref_proceeds_m"]:
            parts.append(f"優先股發行 {_money_m(w['pref_proceeds_m'], False)}")
        if w["pref_repurchase_m"]:
            parts.append(f"優先股回購 {_money_m(w['pref_repurchase_m'], False)}")
        rc = fin.get("recent_convert")
        parts.append(f"可轉債：{rc['filed'][5:].replace('-', '/')} ${rc['principal'] / 1e9:.2f}B" if rc
                     else "近 90 天無可轉債")
        tone = "warn" if (ms >= fin["massive_m"] or rc) else "neutral"
        sig.append({"rank": 1, "name": "Strategy 融資方式", "live": True, "value": value,
                    "detail": "；".join(parts), "tone": tone})
    else:
        sig.append({"rank": 1, "name": "Strategy 融資方式", "live": False, "value": "等待 SEC 資料",
                    "detail": "第一次執行要補抓過去一年的 8-K，可能要跑幾次才完整"})

    if m.get("mnav_deck") is not None:
        md = m["mnav_deck"]
        state = "折價，市值低於淨 BTC" if md < 1 else f"溢價 {(md - 1) * 100:.0f}%"
        if div.get("status") == "top":
            dtext, tone = "週線頂背離！", "warn"
        elif div.get("status") == "crash":
            dtext, tone = "mNAV 雪崩！", "warn"
        elif div.get("weeks", 0) < div.get("need", 27):
            dtext, tone = f"背離偵測累積中（{div.get('weeks', 0)}/{div.get('need', 27)} 週）", \
                "warn" if md < 1 else "neutral"
        else:
            dtext = f"無背離（mNAV 距 {div.get('need', 27) - 1} 週高 {div.get('mnav_vs_max_pct', 0):+.0f}%）"
            tone = "warn" if md < 1 else "neutral"
        sig.append({"rank": 2, "name": "mNAV", "live": True, "value": f"{md:.2f}",
                    "detail": f"{state}；{dtext}", "tone": tone})
    else:
        sig.append({"rank": 2, "name": "mNAV", "live": True, "value": "無法計算",
                    "detail": "淨 BTC 市值為負或缺 MSTR 股價", "tone": "warn"})

    lows = sorted(levels, key=lambda x: x["price"])
    below, above = lv["below"], lv["above"]
    if below and above:
        value = f"{usd(below['price'])}–{usd(above['price'])} 之間"
    elif above:
        value = f"低於 {usd(above['price'])}"
    else:
        value = f"高於 {usd(below['price'])}"
    detail = "、".join(f"{d['name']} {d['pct']:+.1f}%" for d in reversed(lv["distances"]))
    tone = "neutral"
    if lows and not below:
        tone = "down"
    elif lows and below and below["price"] == lows[-1]["price"]:
        tone = "up"
    sig.append({"rank": 3, "name": "BTC 關鍵價位", "live": True, "value": value,
                "detail": detail, "tone": tone})

    bd, bw = brk.get("1d") or {}, brk.get("1w") or {}
    if bd.get("status") == "up" or bw.get("status") == "up":
        value, tone = ("週線放量突破" if bw.get("status") == "up" else "日線放量突破"), "up"
    elif bd.get("status") == "down" or bw.get("status") == "down":
        value, tone = ("週線放量跌破" if bw.get("status") == "down" else "日線放量跌破"), "down"
    else:
        value, tone = (f"日線 {vol_d:.1f}×" if vol_d else "資料不足"), "neutral"
    parts = []
    if bd:
        parts.append(f"日線區間 {usd(bd['range_low'])}–{usd(bd['range_high'])}，量 {bd['volume_ratio']:.1f}×")
    if bw:
        parts.append(f"週線區間 {usd(bw['range_low'])}–{usd(bw['range_high'])}，量 {bw['volume_ratio']:.1f}×")
    sig.append({"rank": 4, "name": "量價", "live": True, "value": value,
                "detail": "；".join(parts) or "上一根成交量 ÷ 20 根均量", "tone": tone})

    up, cur = windows["upcoming"], windows["current"]
    if cur:
        value = f"窗口中（{cur['phase_from']}之後）"
    elif up:
        value = f"{up[0]['label']}還有 {up[0]['days']} 天"
    else:
        value = "—"
    detail = "、".join(f"{e['label']} {e['date'][5:].replace('-', '/')}" for e in up[:3])
    sig.append({"rank": 5, "name": "時間窗口", "live": True, "value": value,
                "detail": detail, "tone": "warn" if cur else "neutral"})

    if m.get("net_bps_usd") is not None:
        sig.append({"rank": 6, "name": "Net BPS", "live": True,
                    "value": f"${m['net_bps_usd']:,.2f}",
                    "detail": f"每股淨 BTC {m['net_bps_sats']:,} 聰（未扣負債 {m['bps_sats']:,} 聰）"})

    ibit = f"IBIT 成交量 {etf['ibit_volume_ratio']:.1f}×" if etf.get("ibit_volume_ratio") else ""
    if etf.get("flows"):
        last = etf["flows"][-1]
        five = sum(r["total"] for r in etf["flows"][-5:])
        parts = [f"{last['date'][5:].replace('-', '/')}（{etf.get('source', 'Farside')}）", f"5 日合計 {_money_m(five)}"]
        if ibit:
            parts.append(ibit)
        sig.append({"rank": 7, "name": "ETF 資金流", "live": True, "value": _money_m(last["total"]),
                    "detail": "；".join(parts), "tone": "up" if last["total"] > 0 else ("down" if last["total"] < 0 else "neutral")})
    else:
        sig.append({"rank": 7, "name": "ETF 資金流", "live": bool(ibit),
                    "value": "資金流抓不到",
                    "detail": "；".join(x for x in (ibit, etf.get("error", ""),
                                                   "加上 SOSOVALUE_API_KEY 就能補上") if x)})

    if btcd.get("value") is not None:
        ch = btcd.get("change_7d")
        sig.append({"rank": 8, "name": "BTC.D", "live": True, "value": f"{btcd['value']:.1f}%",
                    "detail": f"7 日 {ch:+.1f} 個百分點" if ch is not None else "7 日變化累積中"})
    else:
        sig.append({"rank": 8, "name": "BTC.D", "live": False, "value": "抓不到",
                    "detail": btcd.get("error", "CoinGecko 暫時沒回應")})
    return sig


# ---------------------------------------------------------------- 模擬數據

def demo_series(now):
    rnd = random.Random(42)
    days = 900
    start_day = date_ts(utc_date(now)) - (days - 1) * D1
    closes, p = [], 30000.0
    for i in range(days):
        drift = 0.0016 if i < 560 else (-0.0019 if i < 790 else 0.0012)
        p *= math.exp(drift + rnd.gauss(0, 0.024))
        closes.append(p)
    scale = 82000 / closes[-1]
    daily, prev = [], closes[0] * scale
    for i, c in enumerate(closes):
        c *= scale
        hi = max(prev, c) * (1 + abs(rnd.gauss(0, 0.012)))
        lo = min(prev, c) * (1 - abs(rnd.gauss(0, 0.012)))
        daily.append({"time": start_day + i * D1, "open": prev, "high": hi, "low": lo,
                      "close": c, "volume": abs(rnd.gauss(9000, 3500)) + 1500})
        prev = c
    hourly, hp = [], 80000.0
    start_h = (now - now % H1) - 1199 * H1
    for i in range(1200):
        o = hp
        hp *= math.exp(rnd.gauss(0, 0.004))
        hourly.append({"time": start_h + i * H1, "open": o, "high": max(o, hp) * 1.001,
                       "low": min(o, hp) * 0.999, "close": hp, "volume": abs(rnd.gauss(400, 150)) + 50})
    k = 82000 / hourly[-1]["close"]
    for b in hourly:
        for f in ("open", "high", "low", "close"):
            b[f] *= k
    return daily, hourly


def demo_store(today):
    rnd = random.Random(7)
    filings, hold = {}, 830000.0
    end = today - dt.timedelta(days=(today.weekday() + 1) % 7 or 7)  # 上一個週日
    for i in range(52, 0, -1):
        pe = end - dt.timedelta(weeks=i - 1)
        ps = pe - dt.timedelta(days=6)
        proceeds = max(0.0, rnd.gauss(180, 160)) if i > 3 else 22.0
        if i == 30:
            proceeds = 1450.0
        price = 150 + rnd.gauss(0, 15)
        bought = round(proceeds * 1e6 / 82000)
        hold += bought
        filings[f"demo{i}"] = {
            "filed": (pe + dt.timedelta(days=1)).isoformat(), "url": "https://www.sec.gov/",
            "period_start": ps.isoformat(), "period_end": pe.isoformat(), "is_update": True,
            "atm": {"MSTR": {"shares": round(proceeds * 1e6 / price), "notional_m": 0.0, "proceeds_m": round(proceeds, 1)}},
            "repurchase": {"STRC": {"shares": 1900000, "price_m": 189.0}} if i <= 8 else {},
            "btc": {"holdings": hold, "purchased": bought, "sold": 0.0},
            "usd_reserve": 4.83e9, "usd_cash": 4.473e8, "converts": [], "warnings": [],
        }
    filings["demoC"] = {"filed": (today - dt.timedelta(days=40)).isoformat(), "url": "https://www.sec.gov/",
                        "is_update": False, "converts": [{"principal": 1.5e9, "kind": "pricing",
                                                          "notes": "0% convertible senior notes due 2031"}],
                        "atm": {}, "repurchase": {}, "btc": {}, "warnings": []}
    return {"filings": filings, "reports": {}, "checked_at": now_ts()}


# ---------------------------------------------------------------- 主流程

def run(args):
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("找不到 config.json")
    state = load_json(STATE_PATH, {})
    if args.demo:
        state = {"log": [
            {"time": now_ts() - 5 * H1, "text": "BTC 接近激進止損 74,900\n現價 75,480，在價位上方 0.77%"},
            {"time": now_ts() - 3 * D1, "text": "Strategy 週報（10/12～10/18）\nBTC：買入 267，持有 848,517\nMSTR 增發 140,872 股（$22.0M）"},
        ]}
    alerter = Alerter(state, dry_run=args.dry_run or args.demo)
    errors = []
    now = now_ts()
    today = dt.datetime.now(UTC).date()
    levels = cfg.get("levels", [])
    manual_snaps = cfg.get("strategy_snapshots", [])
    auto_cfg = cfg.get("strategy_auto", {})
    auto_on = bool(auto_cfg.get("enabled", True))

    try:
        # ---- BTC
        if args.demo:
            daily_raw, hourly_raw = demo_series(now)
            price = daily_raw[-1]["close"]
        else:
            try:
                daily_raw = cb_candles(D1, 1100)
                hourly_raw = cb_candles(H1, 1200)
            except Exception as e:
                sys.exit(f"抓不到 BTC K 線，這次不更新網頁：{e}")
            try:
                price = cb_ticker()
            except Exception as e:
                errors.append(f"Coinbase 即時價：{e}")
                price = hourly_raw[-1]["close"]

        h4 = aggregate(hourly_raw, bucket_h4)
        w1 = aggregate(daily_raw, bucket_week)
        d1 = daily_raw
        d1_closed, w1_closed = closed_only(d1, D1, now), closed_only(w1, W1, now)
        ref = next((b for b in reversed(hourly_raw) if b["time"] <= now - D1), None)
        change_24h = round(pct(price, ref["close"]), 2) if ref else None

        # ---- MSTR
        mstr, mstr_stale = None, False
        if args.demo:
            mstr = None  # 下面用持倉推算後再補
        else:
            mstr = mstr_quote(cfg, errors)
            if mstr:
                state["mstr_cache"] = {k: mstr[k] for k in ("price", "time", "source", "kind")}
            elif state.get("mstr_cache"):
                mstr = dict(state["mstr_cache"], history=[])
                mstr_stale = True

        # ---- Strategy：SEC 自動解析
        eur = float(cfg.get("eur_usd", 1.16)) if args.demo else eur_usd_rate(cfg.get("eur_usd", 1.16))
        new_filings = []
        if args.demo:
            store = demo_store(today)
        else:
            store = load_json(STRATEGY_STORE, {})
            if auto_on:
                ua = (os.environ.get("SEC_USER_AGENT") or auto_cfg.get("sec_user_agent") or "").strip()
                if "@" not in ua:
                    errors.append("SEC：還沒設定 SEC_USER_AGENT（名字 + email）")
                else:
                    try:
                        new_filings = sec.update(store, ua, today, errors.append)
                    except Exception as e:
                        errors.append(f"SEC：{e}")
                save_json(STRATEGY_STORE, store)
        fallback = dict(auto_cfg.get("fallback_anchor", {}))
        if auto_cfg.get("debt_override"):
            fallback["debt_override"] = auto_cfg["debt_override"]
        ups, anc = sec._updates(store), sec.anchors(store, fallback)

        def snapshot_for(date_str):
            s = sec.snapshot_at(date_str, store, fallback, eur, ups, anc) if auto_on else None
            return s or pick_snapshot(manual_snaps, date_str, allow_backfill=False)

        snap = snapshot_for(today.isoformat()) or pick_snapshot(manual_snaps, today.isoformat(), True)
        snap_mode = "auto" if snap and snap.get("_auto") else "manual"

        if args.demo and snap:
            hist = []
            for b in d1[-260:]:
                if utc_date(b["time"]).weekday() >= 5:
                    continue
                s_ = snapshot_for(utc_date(b["time"]).isoformat()) or snap
                nb = strategy_metrics(s_, b["close"], 1)["net_bps_usd"]
                drift = 1.05 + 0.25 * math.sin(b["time"] / (D1 * 40))
                hist.append({"date": utc_date(b["time"]).isoformat(), "close": round(nb * drift, 2)})
            mstr = {"price": hist[-1]["close"], "time": now, "source": "模擬數據", "history": hist,
                    "kind": "perp", "official": {"price": hist[-1]["close"] * 0.998, "time": now - 9 * H1},
                    "perp": {"venue": "Binance MSTRUSDT 永續", "price": hist[-1]["close"],
                             "volume_24h_usd": 4.2e7, "time": now},
                    "perps": [], "premium_pct": 0.2}

        metrics, hist_m, snap_age = {}, [], None
        if snap and mstr:
            metrics = strategy_metrics(snap, price, mstr["price"])
            hist_m = mnav_history(mstr.get("history", []), d1, snapshot_for)
        if snap_mode == "auto" and ups:
            snap_age = (today - dt.date.fromisoformat(ups[-1]["filed"])).days
        elif snap:
            snap_age = (today - dt.date.fromisoformat(snap["as_of"])).days

        # ---- 每週融資
        iss = cfg.get("issuance", {})
        massive_m = float(iss.get("massive_mstr_weekly_usd_m", 1000))
        weeks = sec.weekly_financing(store, int(iss.get("lookback_weeks", 52)))
        convs = [c for c in sec.convert_events(store) if c.get("kind") in ("pricing", "closing")]
        recent_conv = next((c for c in reversed(convs)
                            if (today - dt.date.fromisoformat(c["filed"])).days <= 90), None)
        fin = {"weeks": weeks, "massive_m": massive_m,
               "pct_rank": sec.percentile_rank([w["mstr_proceeds_m"] for w in weeks],
                                               weeks[-1]["mstr_proceeds_m"]) if weeks else None,
               "converts": convs[-10:], "recent_convert": recent_conv,
               "backlog": store.get("backlog_8k", 0), "checked_at": store.get("checked_at")}

        # ---- ETF、BTC.D
        etf, btcd = {}, {}
        if args.demo:
            rnd = random.Random(3)
            hist_e, aum = [], 95.0
            for i in range(420, 0, -1):
                d = today - dt.timedelta(days=i)
                if d.weekday() >= 5:
                    continue
                v = round(rnd.gauss(40, 260), 1)
                aum = max(60.0, aum + v / 1000 + rnd.gauss(0, 0.8))
                hist_e.append({"date": d.isoformat(), "total": v, "ibit": None, "aum_b": round(aum, 2)})
            etf = {"flows": hist_e[-30:], "history": hist_e, "source": "SoSoValue", "ibit_volume_ratio": 1.2}
            btcd = {"value": 58.4, "change_7d": 0.6}
        else:
            etf_cache = load_json(ETF_STORE, {})
            hist_e = {r["date"]: r for r in etf_cache.get("history", etf_cache.get("flows", []))}
            if etf_cache.get("v") != 2 or now - etf_cache.get("fetched_at", 0) >= 3 * H1 or not hist_e:
                try:
                    flows, src = etf_flows()
                    for r in flows:
                        old_r = hist_e.get(r["date"], {})
                        hist_e[r["date"]] = {**old_r, **{k: v for k, v in r.items() if v is not None}}
                    etf_cache = {"v": 2, "fetched_at": now, "source": src,
                                 "history": [hist_e[d] for d in sorted(hist_e)]}
                    save_json(ETF_STORE, etf_cache)
                except Exception as e:
                    etf["error"] = str(e)[:120]
            if hist_e:
                hist_list = [hist_e[d] for d in sorted(hist_e)]
                etf["flows"] = hist_list[-30:]
                etf["history"] = hist_list
                etf["source"] = etf_cache.get("source", "SoSoValue")
            try:
                etf["ibit_volume_ratio"] = ibit_volume_ratio()
            except Exception:
                pass
            try:
                v = btc_dominance()
                hist_d = load_json(BTCD_STORE, {"history": []})
                if not hist_d["history"] or hist_d["history"][-1]["date"] != today.isoformat():
                    hist_d["history"].append({"date": today.isoformat(), "value": v})
                    hist_d["history"] = hist_d["history"][-400:]
                    save_json(BTCD_STORE, hist_d)
                week_ago = (today - dt.timedelta(days=7)).isoformat()
                old = next((h for h in reversed(hist_d["history"]) if h["date"] <= week_ago), None)
                btcd = {"value": v, "change_7d": round(v - old["value"], 2) if old else None}
            except Exception as e:
                btcd = {"error": f"CoinGecko：{str(e)[:80]}"}

        # ---- 量能、時間窗口、價位
        vol_d, vol_w = volume_ratio(d1_closed), volume_ratio(w1_closed)
        windows = quarter_windows(cfg, today)
        lv = level_view(levels, price)

        # ---- 警報
        check_level_crosses(alerter, levels, d1_closed, "日線", "1d")
        check_level_crosses(alerter, levels, w1_closed, "週線", "1w")
        check_approach(alerter, levels, price, float(cfg.get("approach_pct", 1.0)),
                       float(cfg.get("approach_cooldown_hours", 12)))
        brk = {"1d": check_breakout(alerter, d1_closed, "1d", "日線", cfg),
               "1w": check_breakout(alerter, w1_closed, "1w", "週線", cfg)}
        div = check_divergence(alerter, w1_closed, hist_m, cfg)
        if metrics and not mstr_stale:
            check_mnav(alerter, state, metrics.get("mnav_deck"),
                       cfg.get("mnav_alert_thresholds", []), price, mstr["price"],
                       "（永續合約參考價）" if mstr.get("kind") == "perp" else "")
        strategy_alerts(alerter, new_filings, weeks, cfg, metrics, today)

        etf_th = float(cfg.get("etf", {}).get("alert_daily_usd_m", 500))
        if etf.get("flows"):
            last = etf["flows"][-1]
            if abs(last["total"]) >= etf_th:
                verb = "淨流入" if last["total"] > 0 else "淨流出"
                alerter.push(f"etf:{last['date']}", f"BTC 現貨 ETF {last['date']} 單日{verb} {_money_m(last['total'])}")

        reports = sorted(store.get("reports", {}).items(), key=lambda kv: kv[1].get("report_date", ""))
        for acc, r in reports[-1:]:
            if r.get("warnings") and not alerter.seen(f"rep:{acc}"):
                alerter.push(f"rep:{acc}", (f"{r.get('form')}（{r.get('report_date')}）有部分欄位沒解析到："
                                            + "；".join(r["warnings"]) + "\n這些欄位會繼續用 config.json 的備用基準"))

        stale_days = int(cfg.get("snapshot_stale_days", 8))
        if snap_mode == "auto":
            if snap_age is not None and snap_age >= 14 and alerter.cooldown_ok("stale", 72):
                alerter.push("stale", f"已經 {snap_age} 天沒有新的 Strategy 每週 8-K，持倉可能沒更新到")
        elif snap_age is not None and snap_age >= stale_days and alerter.cooldown_ok("stale", 72):
            alerter.push("stale", (f"Strategy 持倉快照已經 {snap_age} 天沒更新（自動解析目前沒有作用）\n"
                                   "到 strategy.com 或最新 8-K 抄數字，更新 config.json"))

        hour = cfg.get("daily_summary_hour_taipei")
        tnow = dt.datetime.now(TPE)
        if hour is not None and tnow.hour >= int(hour) and \
                state.get("last_summary") != tnow.date().isoformat() and d1_closed:
            last_close = d1_closed[-1]["close"]
            lines = [f"每日摘要 {tnow.strftime('%m/%d')}",
                     f"BTC 日線收盤 {usd(last_close)}，現價 {usd(price)}"]
            if metrics.get("mnav_deck") is not None:
                lines.append(f"mNAV {metrics['mnav_deck']:.2f}（簡報）／{metrics['mnav_ev']:.2f}（企業價值）")
                lines.append(f"Net BPS ${metrics['net_bps_usd']:,.2f}")
            lines.append("價位距離：" + "、".join(
                f"{d['name']} {d['pct']:+.1f}%" for d in reversed(lv["distances"])))
            if etf.get("flows"):
                lines.append(f"ETF 昨日 {_money_m(etf['flows'][-1]['total'])}")
            if btcd.get("value") is not None:
                lines.append(f"BTC.D {btcd['value']:.1f}%")
            if windows["upcoming"]:
                e = windows["upcoming"][0]
                lines.append(f"下一個時間點：{e['label']} {e['date']}（{e['days']} 天後）")
            alerter.push(f"summary:{tnow.date().isoformat()}", "\n".join(lines))
            state["last_summary"] = tnow.date().isoformat()

        alerter.prune()

        # ---- 輸出
        data = {
            "generated_at": now,
            "demo": bool(args.demo),
            "btc": {"price": round(price, 2), "change_24h_pct": change_24h, "source": "Coinbase"},
            "candles": {"4h": round_bars(h4[-500:]), "1d": round_bars(d1[-800:]), "1w": round_bars(w1)},
            "levels": levels,
            "level_view": lv,
            "volume": {"1d": vol_d, "1w": vol_w},
            "breakout": brk,
            "divergence": div,
            "mstr": None if not mstr else {
                "price": round(mstr["price"], 2), "time": mstr["time"],
                "source": mstr["source"], "kind": mstr.get("kind", "official"), "stale": mstr_stale,
                "official": mstr.get("official"), "perp": mstr.get("perp"),
                "perps": mstr.get("perps", []), "premium_pct": mstr.get("premium_pct")},
            "strategy": None if not snap else {
                "mode": snap_mode, "as_of": snap["as_of"], "age_days": snap_age,
                "stale": snap_age is not None and snap_age >= (14 if snap_mode == "auto" else stale_days),
                "btc_holdings": snap["btc_holdings"], "shares_outstanding": snap["shares_outstanding"],
                "debt_usd": snap["debt_usd"], "cash_usd": snap["cash_usd"],
                "preferred_usd": snap.get("preferred_usd", {}),
                "sources": snap.get("_資料來源", {}), "metrics": metrics},
            "financing": {k: v for k, v in fin.items() if k != "recent_convert"},
            "etf": etf,
            "btcd": btcd,
            "mnav_history": hist_m,
            "mnav_thresholds": cfg.get("mnav_alert_thresholds", []),
            "windows": windows,
            "signals": build_signals(metrics, lv, vol_d, vol_w, windows, levels, fin, div, brk, etf, btcd),
            "alerts": state.get("log", [])[:15],
            "errors": errors,
        }
        save_json(DATA_PATH, data, compact=True)
        print(f"完成：BTC {usd(price)}，mNAV {metrics.get('mnav_deck')}（{snap_mode}），"
              f"新 8-K {len(new_filings)} 份，錯誤 {len(errors)} 筆")
    finally:
        if not args.demo:
            save_json(STATE_PATH, state)


def main():
    ap = argparse.ArgumentParser(description="BTC／MSTR 指標監控")
    ap.add_argument("--dry-run", action="store_true", help="警報只印出來，不發 Telegram")
    ap.add_argument("--demo", action="store_true", help="不連網，用模擬數據產生 docs/data.json")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
