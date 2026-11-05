#!/usr/bin/env python3
"""可選：規則解析失敗時，用 Grok API 依 JSON schema 抽取（預設關閉）。

開啟條件：環境變數 GROK_API_KEY 有值，且呼叫 extract_with_grok(...)。
抽出的每個數字都必須能在原文中找到，否則 confidence=low。
"""
import json
import os
import re
import urllib.request

SCHEMA_HINT = {
  "events": [{
    "type": "mstr_atm|pref_atm|convert_issue|convert_repurchase|pref_repurchase|mstr_repurchase|btc_buy|btc_sell|usd_reserve|other_debt",
    "security": "MSTR|STRC|...",
    "shares": 0,
    "net_proceeds_usd": 0,
    "notional_usd": 0,
    "btc": 0,
    "avg_price_usd": 0,
    "stage": "announced|priced|settled",
  }]
}


def _numbers_in_text(text):
  vals = set()
  for m in re.finditer(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(billion|million|B|M)?", text, re.I):
    raw = m.group(1).replace(",", "")
    try:
      v = float(raw)
    except ValueError:
      continue
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "b"):
      v *= 1e9
    elif unit in ("million", "m"):
      v *= 1e6
    vals.add(round(v, 2))
    vals.add(round(float(raw), 2))
  return vals


def verify_event_numbers(ev, text):
  """每個出現的數字都要能在原文找到（允許千分位／百萬／十億寫法）。"""
  pool = _numbers_in_text(text)
  for key in ("shares", "net_proceeds_usd", "notional_usd", "btc", "avg_price_usd"):
    v = ev.get(key)
    if v is None:
      continue
    ok = False
    for cand in (v, v / 1e6, v / 1e9, round(v), round(v / 1e6, 1), round(v / 1e9, 2)):
      if any(abs(cand - p) <= max(0.05, abs(cand) * 0.001) for p in pool):
        ok = True
        break
      # 原文常寫 13,593,865
      if re.search(re.escape(f"{int(v):,}"), text) or re.search(rf"\b{int(v)}\b", text):
        ok = True
        break
    if not ok:
      return False
  return True


def extract_with_grok(html_text, meta=None):
  key = (os.environ.get("GROK_API_KEY") or "").strip()
  if not key:
    return None, "GROK_API_KEY 未設定"
  # 截斷避免超長
  excerpt = re.sub(r"<[^>]+>", " ", html_text)
  excerpt = re.sub(r"\s+", " ", excerpt)[:20000]
  prompt = (
    "Extract Strategy Inc financing / bitcoin / convertible events from this SEC filing. "
    "Return ONLY JSON matching this schema: " + json.dumps(SCHEMA_HINT) +
    "\nFiling text:\n" + excerpt
  )
  body = json.dumps({
    "model": "grok-3-mini",
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0,
  }).encode()
  req = urllib.request.Request(
    "https://api.x.ai/v1/chat/completions",
    data=body,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=60) as r:
      data = json.loads(r.read().decode())
  except Exception as e:
    return None, f"Grok 呼叫失敗：{e}"
  content = data["choices"][0]["message"]["content"]
  m = re.search(r"\{.*\}", content, re.S)
  if not m:
    return None, "Grok 沒有回 JSON"
  try:
    parsed = json.loads(m.group(0))
  except json.JSONDecodeError as e:
    return None, f"Grok JSON 無效：{e}"
  events = parsed.get("events") or []
  for ev in events:
    ev["parse_confidence"] = "high" if verify_event_numbers(ev, excerpt) else "low"
    if meta:
      ev.setdefault("accession", meta.get("accession"))
      ev.setdefault("source_url", meta.get("url"))
  return events, None
