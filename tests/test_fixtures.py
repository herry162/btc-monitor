#!/usr/bin/env python3
"""三份已知 8-K 的單元測試：數字必須完全吻合。"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sec_strategy as sec

FIX = os.path.join(ROOT, "tests", "fixtures")


def _load(name_prefix):
    files = [f for f in os.listdir(FIX) if f.startswith(name_prefix) and f.endswith(".html")]
    assert files, f"找不到 fixture：{name_prefix}"
    path = os.path.join(FIX, files[0])
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    meta_path = path.replace(".html", ".meta.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    parsed = sec.parse_8k(html)
    parsed["filed"] = meta.get("filed")
    parsed["form"] = meta.get("form", "8-K")
    parsed["url"] = meta.get("url")
    parsed["acceptance"] = meta.get("acceptanceDateTime")
    return parsed, meta


class FixtureTests(unittest.TestCase):
    def test_2024_11_18_mstr_atm(self):
        p, meta = _load("2024-11-18")
        m = p["atm"]["MSTR"]
        self.assertEqual(m["shares"], 13_593_865)
        self.assertAlmostEqual(m["proceeds_m"], 4600.0, places=1)  # 約 $4.6B
        self.assertEqual(p["period_start"], "2024-11-11")
        self.assertEqual(p["period_end"], "2024-11-17")
        evs = sec.filing_to_events(meta["accession"], p)
        atm = [e for e in evs if e["type"] == "mstr_atm"][0]
        self.assertEqual(atm["shares"], 13_593_865)
        self.assertAlmostEqual(atm["net_proceeds_usd"], 4.6e9, delta=1)

    def test_2026_04_20_strc_and_mstr(self):
        p, meta = _load("2026-04-20")
        self.assertEqual(p["atm"]["STRC"]["shares"], 21_795_389)
        self.assertAlmostEqual(p["atm"]["STRC"]["proceeds_m"], 2176.3, places=1)
        self.assertEqual(p["atm"]["MSTR"]["shares"], 2_165_000)
        self.assertAlmostEqual(p["atm"]["MSTR"]["proceeds_m"], 366.0, places=1)
        evs = sec.filing_to_events(meta["accession"], p)
        types = {(e["type"], e["security"]): e for e in evs}
        self.assertIn(("pref_atm", "STRC"), types)
        self.assertIn(("mstr_atm", "MSTR"), types)

    def test_2026_08_03_atm_sell_repurchase(self):
        p, meta = _load("2026-08-03")
        self.assertEqual(p["atm"]["MSTR"]["shares"], 3_011_361)
        self.assertAlmostEqual(p["atm"]["MSTR"]["proceeds_m"], 290.6, places=1)
        self.assertEqual(p["btc"]["sold"], 1638.0)
        self.assertEqual(p["repurchase"]["STRC"]["shares"], 912_143)
        self.assertAlmostEqual(p["repurchase"]["STRC"]["price_m"], 81.2, places=1)
        evs = sec.filing_to_events(meta["accession"], p)
        kinds = {e["type"] for e in evs}
        self.assertIn("mstr_atm", kinds)
        self.assertIn("btc_sell", kinds)
        self.assertIn("pref_repurchase", kinds)

    def test_2026_05_15_convert_repurchase(self):
        p, meta = _load("2026-05-15")
        creps = p.get("convert_repurchases") or []
        self.assertTrue(creps, "應解析到可轉債回購")
        c = creps[0]
        self.assertAlmostEqual(c["principal"], 1_500_000_000, delta=20_000_000)
        self.assertAlmostEqual(c["cash"], 1_380_000_000, delta=20_000_000)
        self.assertEqual(c["notes_year"], "2029")


if __name__ == "__main__":
    unittest.main(verbosity=2)
