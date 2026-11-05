#!/usr/bin/env python3
"""
回填 2020-08 至今的 Strategy 申報，寫入 data/strategy.json 與 data/events.json。

用法：
  SEC_USER_AGENT="btc-monitor you@example.com" python3 scripts/backfill_events.py
  SEC_USER_AGENT="..." python3 scripts/backfill_events.py --validate-only
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sec_strategy as sec

STRATEGY = os.path.join(ROOT, "data", "strategy.json")
EVENTS = os.path.join(ROOT, "data", "events.json")
EVENTS_DOCS = os.path.join(ROOT, "docs", "events.json")
UTC = dt.timezone.utc


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def push_kv(events_payload):
    """可選：把 events.json 推到 Cloudflare KV（需 CF_* secrets）。"""
    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    ns = os.environ.get("CF_KV_NAMESPACE_ID", "").strip()
    if not (account and token and ns):
        return False
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           f"/storage/kv/namespaces/{ns}/values/events")
    body = json.dumps(events_payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status in (200, 201)


def validate_known(store, events):
    """對已知可轉債交割／回購做校驗；不符就回傳錯誤字串列表。"""
    errors = []
    # 交割：在 filed 前後 14 天內找金額接近的 convert_issue
    converts = [e for e in events if e["type"] == "convert_issue"
                and e.get("stage") in ("settled", "priced", "announced", "mention")]
    for day, principal in sec.KNOWN_CONVERT_SETTLEMENTS:
        d0 = dt.date.fromisoformat(day)
        window = []
        for e in converts:
            fd = e.get("filed_date") or (e.get("filed_at") or "")[:10]
            if not fd:
                continue
            try:
                d = dt.date.fromisoformat(fd)
            except ValueError:
                continue
            if abs((d - d0).days) <= 14 and e.get("notional_usd"):
                if abs(e["notional_usd"] - principal) <= max(1_000_000, principal * 0.02):
                    window.append(e)
        if not window:
            # 也掃 raw converts in store
            found = False
            for rec in store.get("filings", {}).values():
                fd = rec.get("filed")
                if not fd:
                    continue
                try:
                    d = dt.date.fromisoformat(fd)
                except ValueError:
                    continue
                if abs((d - d0).days) > 14:
                    continue
                for c in rec.get("converts") or []:
                    if abs(float(c.get("principal") or 0) - principal) <= max(1e6, principal * 0.02):
                        found = True
                        break
                if found:
                    break
            if not found:
                errors.append(f"找不到可轉債交割 {day} ${principal/1e6:.2f}M（filed ±14 天內）")
    # 回購
    kr = sec.KNOWN_CONVERT_REPURCHASE
    hit = False
    quote = None
    for acc, rec in store.get("filings", {}).items():
        if rec.get("filed") != kr["filed"]:
            continue
        for c in rec.get("convert_repurchases") or []:
            if (abs(c["principal"] - kr["principal"]) <= kr["tol"]
                    and abs(c["cash"] - kr["cash"]) <= kr["tol"]):
                hit = True
                quote = (f"accession={acc} principal={c['principal']} cash={c['cash']} "
                         f"year={c.get('notes_year')}")
                break
    if not hit:
        errors.append(
            f"2026-05-15 可轉債回購不符：期望現金≈${kr['cash']/1e6:.0f}M、"
            f"面額≈${kr['principal']/1e6:.0f}M")
    else:
        print("回購校驗通過：", quote)
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2020-08-01")
    ap.add_argument("--batch", type=int, default=30, help="每輪最多新抓幾份")
    ap.add_argument("--max-rounds", type=int, default=40)
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--forms", default="8-K,8-K/A,DEFA14A",
                    help="回填時優先抓的表單（逗號分隔）；424B/10-Q 可另加")
    args = ap.parse_args()

    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    store = load(STRATEGY, {"filings": {}, "reports": {}})

    if not args.validate_only:
        if "@" not in ua:
            sys.exit("請設定 SEC_USER_AGENT，例如：btc-monitor you@example.com")
        today = dt.datetime.now(UTC).date()
        want = set(x.strip() for x in args.forms.split(",") if x.strip())
        ed = sec.Edgar(ua)
        print(f"列出 {args.since} 至今的申報…")
        listing = ed.filings(since=args.since)
        targets = [f for f in listing if f["form"] in want
                   or any(f["form"].startswith(p) for p in sec.FORM_PREFIXES
                          if "424B" in want or f["form"].startswith("424B"))]
        # 若使用者沒寫 424B，就不要抓
        if not any(x.startswith("424B") for x in want):
            targets = [f for f in targets if not f["form"].startswith("424B")]
        targets = [f for f in targets if f["accession"] not in store.get("filings", {})]
        print(f"待抓 {len(targets)} 份（已有 {len(store.get('filings', {}))}）")
        rounds = 0
        while targets and rounds < args.max_rounds:
            rounds += 1
            batch = targets[:args.batch]
            targets = targets[args.batch:]
            print(f"第 {rounds} 輪：抓 {len(batch)} 份，剩餘 {len(targets)}")
            for f in batch:
                try:
                    html = ed.get(sec.Edgar.url(f))
                    p = sec.parse_8k(html)
                    rec = {"filed": f["filed"], "form": f["form"], "url": sec.Edgar.url(f),
                           "acceptance": f.get("acceptance"), "v": sec.PARSER_VERSION, **p}
                    store.setdefault("filings", {})[f["accession"]] = rec
                    tag = "update" if p.get("is_update") else p.get("form", "")
                    print(f"  OK {f['filed']} {f['form']} {tag}")
                except Exception as e:
                    print(f"  FAIL {f['filed']} {f['form']}: {e}", file=sys.stderr)
            save(STRATEGY, store)
            time.sleep(0.2)
        # 季報補一點
        try:
            sec.update(store, ua, today, print, max_8k=0, max_reports=3, lookback_days=2000)
        except Exception as e:
            print("季報更新略過：", e)
        save(STRATEGY, store)

    events = sec.store_to_events(store)
    payload = {
        "generated_at": int(time.time()),
        "count": len(events),
        "events": events,
    }
    save(EVENTS, payload)
    save(EVENTS_DOCS, payload)
    print(f"寫入 events.json：{len(events)} 筆")
    try:
        if push_kv(payload):
            print("已推送到 Cloudflare KV")
    except Exception as e:
        print("KV 推送略過：", e)

    errs = validate_known(store, events)
    if errs:
        print("校驗失敗：")
        for e in errs:
            print(" -", e)
        sys.exit(1)
    print("已知可轉債交割／回購校驗通過")


if __name__ == "__main__":
    main()
