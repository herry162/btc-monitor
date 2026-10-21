#!/usr/bin/env python3
"""
BTC／MSTR 指標監控（第一階段）

每次執行會：
1. 從 Coinbase 抓 BTC-USD K 線，合成 4H／日／週
2. 抓 MSTR 股價，用 config.json 的 Strategy 持倉快照算 mNAV 與 Net BPS
3. 檢查價位、mNAV 門檻、持倉快照是否過期，必要時發 Telegram
4. 輸出 docs/data.json 給網頁讀

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
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
DATA_PATH = os.path.join(ROOT, "docs", "data.json")

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


def mnav_history(mstr_hist, btc_daily, snaps, allow_backfill=False):
    btc_by_date = {utc_date(b["time"]).isoformat(): b["close"] for b in btc_daily}
    out = []
    for row in mstr_hist:
        s = pick_snapshot(snaps, row["date"], allow_backfill)
        btc = btc_by_date.get(row["date"])
        if not s or not btc:
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


def check_mnav(alerter, state, m, thresholds, btc, mstr):
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
                    f"目前 {m:.3f}，MSTR {mstr:.2f}，BTC {usd(btc)}"))
    if m is not None:
        state["last_mnav"] = m


# ---------------------------------------------------------------- 排行訊號

def build_signals(m, lv, vol_d, vol_w, windows, levels):
    sig = []

    sig.append({"rank": 1, "name": "Strategy 融資方式", "live": False,
                "value": "尚未接上", "detail": "第二階段：自動解析 SEC 8-K，判斷可轉債或普通股的巨量發行"})

    if m.get("mnav_deck") is not None:
        md = m["mnav_deck"]
        state = "折價，市值低於淨 BTC" if md < 1 else f"溢價 {(md - 1) * 100:.0f}%"
        sig.append({"rank": 2, "name": "mNAV", "live": True, "value": f"{md:.2f}",
                    "detail": f"{state}；背離偵測在第二階段", "tone": "warn" if md < 1 else "neutral"})
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

    vd = f"日線 {vol_d:.1f}×" if vol_d else "資料不足"
    vw = f"週線 {vol_w:.1f}×" if vol_w else "週線資料不足"
    sig.append({"rank": 4, "name": "量價", "live": True, "value": vd,
                "detail": f"上一根成交量 ÷ 20 根均量；{vw}；放量突破偵測在第二階段",
                "tone": "up" if (vol_d or 0) >= 1.5 else "neutral"})

    up = windows["upcoming"]
    cur = windows["current"]
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

    sig.append({"rank": 7, "name": "ETF 資金流", "live": False, "value": "尚未接上",
                "detail": "第二階段：每日美股收盤後更新"})
    sig.append({"rank": 8, "name": "BTC.D", "live": False, "value": "尚未接上",
                "detail": "第二階段：CoinGecko 每小時更新"})
    return sig


# ---------------------------------------------------------------- 模擬數據

def demo_series(now):
    rnd = random.Random(42)
    # 日線：900 天，最後收在 82,000 附近
    days = 900
    start_day = date_ts(utc_date(now)) - (days - 1) * D1
    closes, p = [], 30000.0
    for i in range(days):
        drift = 0.0016 if i < 560 else (-0.0019 if i < 790 else 0.0012)
        p *= math.exp(drift + rnd.gauss(0, 0.024))
        closes.append(p)
    scale = 82000 / closes[-1]
    daily = []
    prev = closes[0] * scale
    for i, c in enumerate(closes):
        c *= scale
        hi = max(prev, c) * (1 + abs(rnd.gauss(0, 0.012)))
        lo = min(prev, c) * (1 - abs(rnd.gauss(0, 0.012)))
        daily.append({"time": start_day + i * D1, "open": prev, "high": hi, "low": lo,
                      "close": c, "volume": abs(rnd.gauss(9000, 3500)) + 1500})
        prev = c
    # 1 小時：1200 根
    hourly = []
    hp = 80000.0
    start_h = (now - now % H1) - 1199 * H1
    for i in range(1200):
        o = hp
        hp *= math.exp(rnd.gauss(0, 0.004))
        hourly.append({"time": start_h + i * H1, "open": o, "high": max(o, hp) * 1.001,
                       "low": min(o, hp) * 0.999, "close": hp,
                       "volume": abs(rnd.gauss(400, 150)) + 50})
    k = 82000 / hourly[-1]["close"]
    for b in hourly:
        for f in ("open", "high", "low", "close"):
            b[f] *= k
    return daily, hourly


# ---------------------------------------------------------------- 主流程

def run(args):
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("找不到 config.json")
    state = load_json(STATE_PATH, {})
    if args.demo:
        state = {"log": [
            {"time": now_ts() - 5 * H1, "text": "BTC 接近激進止損 74,900\n現價 75,480，在價位上方 0.77%"},
            {"time": now_ts() - 3 * D1, "text": "mNAV 跌破 1.0（簡報算法）\n目前 0.985，MSTR 148.70，BTC 81,200"},
        ]}
    alerter = Alerter(state, dry_run=args.dry_run or args.demo)
    errors = []
    now = now_ts()
    today = dt.datetime.now(UTC).date()
    levels = cfg.get("levels", [])
    snaps = cfg.get("strategy_snapshots", [])

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
        d1_closed = closed_only(d1, D1, now)
        w1_closed = closed_only(w1, W1, now)
        h4_closed = closed_only(h4, H4, now)

        # 24 小時漲跌：用 1 小時 K 棒往回找
        ref = next((b for b in reversed(hourly_raw) if b["time"] <= now - D1), None)
        change_24h = round(pct(price, ref["close"]), 2) if ref else None

        # ---- MSTR
        mstr, mstr_stale = None, False
        if args.demo:
            snap0 = pick_snapshot(snaps, today.isoformat(), True)
            hist = []
            for b in d1[-260:]:
                if utc_date(b["time"]).weekday() >= 5:
                    continue
                nb = strategy_metrics(snap0, b["close"], 1)["net_bps_usd"]
                drift = 1.05 + 0.25 * math.sin(b["time"] / (D1 * 40))
                hist.append({"date": utc_date(b["time"]).isoformat(), "close": round(nb * drift, 2)})
            mstr = {"price": hist[-1]["close"], "time": now, "source": "模擬數據", "history": hist}
        else:
            for fn in (mstr_yahoo, mstr_stooq):
                try:
                    mstr = fn()
                    break
                except Exception as e:
                    errors.append(f"MSTR（{fn.__name__}）：{e}")
            if mstr:
                state["mstr_cache"] = {k: mstr[k] for k in ("price", "time", "source")}
            elif state.get("mstr_cache"):
                mstr = dict(state["mstr_cache"], history=[])
                mstr_stale = True

        # ---- Strategy 指標
        snap = pick_snapshot(snaps, today.isoformat(), allow_backfill=True)
        metrics, hist_m, snap_age = {}, [], None
        if snap and mstr:
            metrics = strategy_metrics(snap, price, mstr["price"])
            hist_m = mnav_history(mstr.get("history", []), d1, snaps, allow_backfill=args.demo)
        if snap:
            snap_age = (today - dt.date.fromisoformat(snap["as_of"])).days

        # ---- 量能、時間窗口、價位
        vol_d = volume_ratio(d1_closed)
        vol_w = volume_ratio(w1_closed)
        windows = quarter_windows(cfg, today)
        lv = level_view(levels, price)

        # ---- 警報
        check_level_crosses(alerter, levels, d1_closed, "日線", "1d")
        check_level_crosses(alerter, levels, w1_closed, "週線", "1w")
        check_approach(alerter, levels, price, float(cfg.get("approach_pct", 1.0)),
                       float(cfg.get("approach_cooldown_hours", 12)))
        if metrics and not mstr_stale:
            check_mnav(alerter, state, metrics.get("mnav_deck"),
                       cfg.get("mnav_alert_thresholds", []), price, mstr["price"])

        stale_days = int(cfg.get("snapshot_stale_days", 8))
        if snap_age is not None and snap_age >= stale_days and alerter.cooldown_ok("stale", 72):
            alerter.push("stale", (
                f"Strategy 持倉快照已經 {snap_age} 天沒更新（最後是 {snap['as_of']}）\n"
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
            "candles": {"4h": round_bars(h4[-500:]), "1d": round_bars(d1[-800:]),
                        "1w": round_bars(w1)},
            "levels": levels,
            "level_view": lv,
            "volume": {"1d": vol_d, "1w": vol_w},
            "mstr": None if not mstr else {
                "price": round(mstr["price"], 2), "time": mstr["time"],
                "source": mstr["source"], "stale": mstr_stale},
            "strategy": None if not snap else {
                "as_of": snap["as_of"], "age_days": snap_age,
                "stale": snap_age is not None and snap_age >= stale_days,
                "btc_holdings": snap["btc_holdings"],
                "shares_outstanding": snap["shares_outstanding"],
                "debt_usd": snap["debt_usd"], "cash_usd": snap["cash_usd"],
                "preferred_usd": snap.get("preferred_usd", {}),
                "sources": snap.get("_資料來源", {}),
                "metrics": metrics},
            "mnav_history": hist_m,
            "mnav_thresholds": cfg.get("mnav_alert_thresholds", []),
            "windows": windows,
            "signals": build_signals(metrics, lv, vol_d, vol_w, windows, levels),
            "alerts": state.get("log", [])[:15],
            "errors": errors,
        }
        save_json(DATA_PATH, data, compact=True)
        print(f"完成：BTC {usd(price)}，mNAV {metrics.get('mnav_deck')}，"
              f"錯誤 {len(errors)} 筆，輸出 {os.path.relpath(DATA_PATH, ROOT)}")
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
