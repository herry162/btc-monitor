"""
Strategy（MSTR）SEC 申報自動解析

做三件事：
1. 從 SEC EDGAR 抓 Strategy 的 8-K（每週更新）和 10-Q／10-K（每季）
2. 解析出持幣、USD Reserve／Cash、MSTR 與優先股的增發和回購、可轉債事件
3. 用「最近一次季報的數字 + 之後每週 8-K 的增減」推算任何一天的持倉快照

解析結果存在 data/strategy.json（會 commit 回 repo），只有新申報才會重新下載。
"""

import datetime as dt
import gzip
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser

CIK = "0001050446"
SERIES = ("STRF", "STRC", "STRE", "STRK", "STRD")
ROW_RE = re.compile(r"^(STRF|STRC|STRK|STRD|STRE|MSTR)\b")
DATE_PAT = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


# ---------------------------------------------------------------- HTML → 表格與文字

def _clean(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.text = [], []
        self._tables, self._row, self._cell, self._skip = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._tables.append([])
        elif tag == "tr" and self._tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._tables:
            if any(self._row):
                self._tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._tables:
            t = self._tables.pop()
            if t:
                self.tables.append(t)
        if tag in ("p", "div", "tr", "br", "li"):
            self.text.append(" ")

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def parse_html(html):
    p = _Parser()
    p.feed(html)
    return p.tables, _clean(" ".join(p.text))


def _nums(cells):
    """把一列儲存格裡的數字依序取出。'-' 當 0，(1) 這種註腳拿掉。"""
    s = " ".join(cells)
    s = re.sub(r"\(\d{1,2}\)", " ", s)
    s = s.replace("$", " ").replace("€", " ")
    out = []
    for tok in s.split():
        tok = tok.strip(",;")
        if tok in ("-", "—", "–"):
            out.append(0.0)
        elif re.fullmatch(r"\d[\d,]*(\.\d+)?", tok):
            out.append(float(tok.replace(",", "")))
    return out


def _labels(row):
    out = []
    for c in row:
        c = re.sub(r"\(\d{1,2}\)", "", c).strip()
        if c and c.lower() != "security":
            out.append(c)
    return out


def _find(labels, *keys):
    for i, lab in enumerate(labels):
        low = lab.lower()
        if all(k in low for k in keys):
            return i
    return None


def _date(s):
    m = re.match(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", s or "")
    if not m or m.group(1) not in MONTHS:
        return None
    return dt.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).isoformat()


def _money(num, unit):
    v = float(num.replace(",", ""))
    return v * (1e9 if unit.lower().startswith("b") else 1e6)


# ---------------------------------------------------------------- 8-K

def _table_rows(table, header_key):
    """找到含 header_key 的標題列，回傳（欄位名稱, 之後的資料列）"""
    for i, row in enumerate(table):
        if any(header_key.lower() in c.lower() for c in row):
            return _labels(row), table[i + 1:]
    return None, []


def _series_rows(labels, rows):
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        m = ROW_RE.match(cells[0])
        if not m:
            continue
        yield m.group(1), _nums(cells[1:])


def parse_8k(html):
    tables, text = parse_html(html)
    out = {"atm": {}, "repurchase": {}, "btc": {}, "converts": [], "warnings": []}

    m = re.search(r"During (?:the )?Period " + DATE_PAT + r" to " + DATE_PAT, text)
    if m:
        out["period_start"], out["period_end"] = _date(m.group(1)), _date(m.group(2))

    for t in tables:
        flat = " ".join(" ".join(r) for r in t)
        if "Shares Sold" in flat and "Net Proceeds" in flat:
            labels, rows = _table_rows(t, "Shares Sold")
            i_sh, i_no, i_pr = _find(labels, "shares sold"), _find(labels, "notional"), _find(labels, "net proceeds")
            for ser, vals in _series_rows(labels, rows):
                if len(vals) >= len(labels):
                    rec = {"shares": vals[i_sh] if i_sh is not None else 0.0,
                           "notional_m": vals[i_no] if i_no is not None else 0.0,
                           "proceeds_m": vals[i_pr] if i_pr is not None else 0.0}
                elif len(vals) >= 2:
                    rec = {"shares": vals[0], "notional_m": 0.0, "proceeds_m": vals[1]}
                    out["warnings"].append(f"ATM {ser} 欄位數不符，用推測順序")
                else:
                    continue
                out["atm"][ser] = rec
        elif "Shares Repurchased" in flat:
            labels, rows = _table_rows(t, "Shares Repurchased")
            i_sh, i_pr = _find(labels, "shares repurchased"), _find(labels, "purchase price")
            for ser, vals in _series_rows(labels, rows):
                if len(vals) >= 2:
                    out["repurchase"][ser] = {
                        "shares": vals[i_sh if i_sh is not None else 0],
                        "price_m": vals[i_pr if i_pr is not None else 1]}
        elif "Aggregate BTC Holdings" in flat:
            labels, rows = _table_rows(t, "Aggregate BTC Holdings")
            i_h = _find(labels, "aggregate btc holdings")
            i_p = _find(labels, "btc purchased")
            if i_p is None:
                i_p = _find(labels, "btc acquired")
            i_s = _find(labels, "btc sold")
            for row in rows:
                vals = _nums(row)
                if len(vals) >= len(labels) and i_h is not None:
                    out["btc"] = {"holdings": vals[i_h],
                                  "purchased": vals[i_p] if i_p is not None else 0.0,
                                  "sold": vals[i_s] if i_s is not None else 0.0}
                    break

    m = re.search(r"balances of the USD Reserve and USD Cash were \$\s?([\d.,]+)\s*(billion|million)"
                  r" and \$\s?([\d.,]+)\s*(billion|million)", text)
    if m:
        out["usd_reserve"] = _money(m.group(1), m.group(2))
        out["usd_cash"] = _money(m.group(3), m.group(4))
    else:
        m = re.search(r"balance of the USD Reserve was \$\s?([\d.,]+)\s*(billion|million)", text)
        if m:
            out["usd_reserve"] = _money(m.group(1), m.group(2))
            out["usd_cash"] = 0.0

    for m in re.finditer(r"\$\s?([\d.,]+)\s*(billion|million)\s+aggregate principal amount of"
                         r"\s+(?:its\s+|our\s+)?([^.;]{0,80}?)convertible senior notes", text, re.I):
        out["converts"].append({"principal": _money(m.group(1), m.group(2)),
                                "notes": _clean(m.group(3) + "convertible senior notes")})
    if out["converts"]:
        if re.search(r"\bpric(?:ed|ing)\b", text, re.I):
            kind = "pricing"
        elif re.search(r"\bproposed\b", text, re.I):
            kind = "proposed"
        elif re.search(r"\b(?:closing|closed|completed)\b", text, re.I):
            kind = "closing"
        elif re.search(r"repurchas|redeem|redemption|exchang|settle", text, re.I):
            kind = "retire"
        else:
            kind = "other"
        for c in out["converts"]:
            c["kind"] = kind

    out["is_update"] = bool(out["atm"] or out["btc"] or "usd_reserve" in out)
    out["looks_like_update"] = bool(re.search(r"\b(ATM Update|BTC Update)\b", text))
    if out["looks_like_update"] and not out["btc"]:
        out["warnings"].append("看起來是每週更新，但沒解析到持幣表")
    if out["looks_like_update"] and "period_end" not in out:
        out["warnings"].append("沒解析到期間日期")
    return out


# ---------------------------------------------------------------- 10-Q／10-K

def parse_report(html, report_date):
    tables, text = parse_html(html)
    out = {"report_date": report_date, "warnings": []}

    m = re.search(r"As of " + DATE_PAT + r",? the registrant had ([\d,]+) and ([\d,]+) shares of class A"
                  r" common stock and class B common stock outstanding", text, re.I)
    if not m:
        m = re.search(r"As of " + DATE_PAT + r",? the registrant had ([\d,]+) shares of class A common"
                      r" stock and ([\d,]+) shares of class B common stock outstanding", text, re.I)
    if m:
        a, b = int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))
        out["shares"] = {"as_of": _date(m.group(1)), "value": a + b, "class_a": a, "class_b": b}
    else:
        out["warnings"].append("沒找到封面流通股數")

    m = re.search(r"principal due upon maturity of our long-term debt instruments in the aggregate of"
                  r" \$\s?([\d.,]+)\s*(billion|million)", text, re.I)
    if m:
        out["debt"] = {"as_of": report_date, "value": _money(m.group(1), m.group(2))}
    else:
        out["warnings"].append("沒找到負債本金")

    pref = {}
    for t in tables:
        flat = " ".join(" ".join(r) for r in t)
        if "Notional Value" not in flat or "Shares Outstanding" not in flat:
            continue
        labels, rows = _table_rows(t, "Notional Value")
        if labels and not re.search(r"shares|notional|dividend|rate", labels[0], re.I):
            labels = labels[1:]  # 第一欄是列名（例如「Preferred Stock」），不是數字欄
        i_no = _find(labels, "notional")
        if labels is None or i_no is None:
            continue
        thousands = "thousand" in labels[i_no].lower()
        for row in rows:
            cells = [c for c in row if c]
            if not cells:
                continue
            m2 = re.match(r"^(STRF|STRC|STRK|STRD|STRE)\b", cells[0])
            if not m2:
                continue
            vals = _nums(cells[1:])
            if len(vals) <= i_no:
                continue
            v = vals[i_no] * (1000 if thousands else 1e6)
            pref[m2.group(1)] = {"value": v, "eur": "€" in " ".join(cells)}
        if pref:
            break
    if pref:
        out["preferred"] = {"as_of": report_date, "series": pref}
    else:
        out["warnings"].append("沒找到優先股面額表")
    return out


# ---------------------------------------------------------------- EDGAR

class Edgar:
    def __init__(self, user_agent):
        self.ua = user_agent
        self._last = 0.0

    @staticmethod
    def _body(raw, encoding):
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="replace")

    def get(self, url, timeout=30):
        wait = 0.15 - (time.time() - self._last)  # SEC 規定每秒最多 10 次
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        # 照 SEC 文件的建議帶標頭：名字 + email 的 User-Agent，並接受 gzip
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return self._body(r.read(), r.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            try:
                body = self._body(e.read(), e.headers.get("Content-Encoding"))
            except Exception:
                body = ""
            if "Undeclared Automated Tool" in body or "declare your traffic" in body.lower():
                raise RuntimeError("SEC 拒絕：User-Agent 要有名字和真實 email（設定 SEC_USER_AGENT）") from None
            if "Request Rate Threshold" in body:
                raise RuntimeError("SEC 拒絕：請求太頻繁，稍後會自動重試") from None
            hint = re.sub(r"<[^>]+>", " ", body)
            hint = re.sub(r"\s+", " ", hint).strip()[:120]
            raise RuntimeError(f"SEC HTTP {e.code}{('：' + hint) if hint else ''}") from None

    def filings(self):
        j = json.loads(self.get(f"https://data.sec.gov/submissions/CIK{CIK}.json"))
        r = j["filings"]["recent"]
        out = []
        for i in range(len(r["accessionNumber"])):
            out.append({"form": r["form"][i], "filed": r["filingDate"][i],
                        "report_date": r["reportDate"][i], "accession": r["accessionNumber"][i],
                        "doc": r["primaryDocument"][i]})
        return out

    @staticmethod
    def url(f):
        return (f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/"
                f"{f['accession'].replace('-', '')}/{f['doc']}")


def update(store, user_agent, today, log, max_8k=40, max_reports=1, lookback_days=400):
    """抓新申報、解析後存進 store。回傳這次新增的 8-K 清單。"""
    store.setdefault("filings", {})
    store.setdefault("reports", {})
    ed = Edgar(user_agent)
    listing = ed.filings()
    cutoff = (today - dt.timedelta(days=lookback_days)).isoformat()
    new = []

    eightk = [f for f in listing if f["form"] in ("8-K", "8-K/A") and f["filed"] >= cutoff
              and f["accession"] not in store["filings"]]
    eightk.sort(key=lambda f: f["filed"], reverse=True)  # 先抓最新的
    for f in eightk[:max_8k]:
        try:
            p = parse_8k(ed.get(Edgar.url(f)))
        except Exception as e:  # 單一份失敗不影響其他
            log(f"8-K {f['filed']} 解析失敗：{e}")
            continue
        rec = {"filed": f["filed"], "url": Edgar.url(f), **p}
        store["filings"][f["accession"]] = rec
        new.append(rec)

    reports = [f for f in listing if f["form"] in ("10-Q", "10-K")]
    reports.sort(key=lambda f: f["report_date"], reverse=True)
    todo = [f for f in reports[:3] if f["accession"] not in store["reports"]]
    for f in todo[:max_reports]:
        try:
            p = parse_report(ed.get(Edgar.url(f), timeout=90), f["report_date"])
            store["reports"][f["accession"]] = {"form": f["form"], "filed": f["filed"],
                                                "url": Edgar.url(f), **p}
        except Exception as e:
            log(f"{f['form']} {f['report_date']} 解析失敗：{e}")

    store["checked_at"] = int(time.time())
    store["backlog_8k"] = max(0, len(eightk) - max_8k)
    return new


# ---------------------------------------------------------------- 推算持倉快照

def _updates(store):
    ups = [f for f in store.get("filings", {}).values() if f.get("is_update") and f.get("period_end")]
    ups.sort(key=lambda f: (f["period_end"], f["filed"]))
    # 同一期間若有更正版（8-K/A），只留最後申報的，避免重複加減
    dedup = {}
    for f in ups:
        dedup[(f.get("period_start"), f["period_end"])] = f
    return sorted(dedup.values(), key=lambda f: (f["period_end"], f["filed"]))


def _latest(items, date):
    best = None
    for it in items:
        if it and it.get("as_of") and it["as_of"] <= date:
            if best is None or it["as_of"] > best["as_of"]:
                best = it
    return best


def anchors(store, fallback):
    reps = list(store.get("reports", {}).values())
    sh = [fallback.get("shares")] + [r.get("shares") for r in reps]
    debt = [fallback.get("debt")] + [r.get("debt") for r in reps]
    pref = []
    fb = fallback.get("preferred")
    if fb:
        pref.append({"as_of": fb["as_of"],
                     "series": {k: {"value": float(v), "eur": False} for k, v in fb.items() if k in SERIES}})
    for r in reps:
        if r.get("preferred"):
            pref.append(r["preferred"])
    override = fallback.get("debt_override")
    if override and override.get("as_of"):
        debt.append(override)
    return [a for a in sh if a], [a for a in pref if a], [a for a in debt if a]


def snapshot_at(date, store, fallback, eur_usd=1.16, ups=None, anc=None):
    """date（YYYY-MM-DD）當天的推算持倉。資料不足時回傳 None。"""
    ups = ups if ups is not None else _updates(store)
    sh_a, pf_a, db_a = anc if anc is not None else anchors(store, fallback)
    a_sh, a_pf, a_db = _latest(sh_a, date), _latest(pf_a, date), _latest(db_a, date)
    done = [u for u in ups if u["period_end"] <= date]
    btc = next((u for u in reversed(done) if u.get("btc", {}).get("holdings")), None)
    cash = next((u for u in reversed(done) if "usd_reserve" in u), None)
    if not (a_sh and a_pf and a_db and btc and cash):
        return None

    shares, sh_delta = float(a_sh["value"]), 0.0
    for u in done:
        if u.get("period_start") and u["period_start"] > a_sh["as_of"]:
            sh_delta += u.get("atm", {}).get("MSTR", {}).get("shares", 0.0)
            sh_delta -= u.get("repurchase", {}).get("MSTR", {}).get("shares", 0.0)
    shares += sh_delta

    pref = {}
    for s in SERIES:
        base = a_pf["series"].get(s)
        if not base:
            continue
        fx = eur_usd if base.get("eur") else 1.0
        v = base["value"] * fx
        for u in done:
            if u.get("period_start") and u["period_start"] > a_pf["as_of"]:
                v += u.get("atm", {}).get(s, {}).get("notional_m", 0.0) * 1e6
                rep = u.get("repurchase", {}).get(s, {}).get("shares", 0.0)
                v -= rep * 100 * (eur_usd if s == "STRE" else 1.0)
        pref[s] = round(max(v, 0.0))

    return {
        "as_of": date,
        "btc_holdings": btc["btc"]["holdings"],
        "shares_outstanding": round(shares),
        "debt_usd": a_db["value"],
        "preferred_usd": pref,
        "cash_usd": cash["usd_reserve"] + cash.get("usd_cash", 0.0),
        "_資料來源": {
            "btc_holdings": f"8-K {btc['filed']}（截至 {btc['period_end']}）",
            "cash_usd": f"8-K {cash['filed']}：USD Reserve {cash['usd_reserve'] / 1e9:.2f}B"
                        f" + USD Cash {cash.get('usd_cash', 0) / 1e6:,.1f}M",
            "shares_outstanding": f"{a_sh['as_of']} 基準 {a_sh['value']:,.0f} 股，之後 8-K 淨增 {sh_delta:,.0f} 股",
            "preferred_usd": f"{a_pf['as_of']} 基準面額，加上之後 8-K 的發行、扣掉回購",
            "debt_usd": f"{a_db['as_of']} 基準",
        },
        "_auto": True,
    }


def weekly_financing(store, lookback=52):
    """每週融資：MSTR 增發、優先股發行、回購、可轉債"""
    rows = []
    for u in _updates(store):
        atm, rep = u.get("atm", {}), u.get("repurchase", {})
        rows.append({
            "period_start": u.get("period_start"), "period_end": u["period_end"], "filed": u["filed"],
            "url": u.get("url"),
            "mstr_shares": atm.get("MSTR", {}).get("shares", 0.0),
            "mstr_proceeds_m": atm.get("MSTR", {}).get("proceeds_m", 0.0),
            "pref_proceeds_m": sum(atm.get(s, {}).get("proceeds_m", 0.0) for s in SERIES),
            "pref_repurchase_m": sum(rep.get(s, {}).get("price_m", 0.0) for s in SERIES),
            "mstr_repurchase_m": rep.get("MSTR", {}).get("price_m", 0.0),
            "btc_purchased": u.get("btc", {}).get("purchased", 0.0),
            "btc_sold": u.get("btc", {}).get("sold", 0.0),
            "btc_holdings": u.get("btc", {}).get("holdings"),
            "repurchase": {s: v for s, v in rep.items() if v.get("shares")},
            "atm": {s: v for s, v in atm.items() if v.get("shares") or v.get("proceeds_m")},
        })
    # 同一期間若有多份（例如更正），保留最後申報的
    dedup = {}
    for r in rows:
        dedup[r["period_end"]] = r
    return sorted(dedup.values(), key=lambda r: r["period_end"])[-lookback:]


def convert_events(store):
    ev = []
    for f in store.get("filings", {}).values():
        for c in f.get("converts", []):
            ev.append({"filed": f["filed"], "url": f.get("url"), **c})
    return sorted(ev, key=lambda e: e["filed"])


def percentile_rank(values, x):
    if not values:
        return None
    return round(100 * sum(1 for v in values if v <= x) / len(values))
