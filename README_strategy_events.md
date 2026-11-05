# Strategy 融資事件監控（補充說明）

這份文件教你：**怎麼把「Strategy／MSTR 每一筆融資、買幣、賣幣、回購」加到現有的 btc-monitor**，以及怎麼部署、怎麼在電腦上自己測。

你不需要會寫程式。照著步驟做即可。

---

## 它做什麼？

1. **盯著 SEC EDGAR**（美國官方公告系統）看 Strategy（CIK 0001050446）有沒有新的：
   - 8-K、8-K/A、10-Q、10-K、424B 系列、DEFA14A
2. **公告一出現**（約 1 分鐘內偵測到）：
   - Cloudflare Worker 先喊一聲 Telegram「偵測到新申報」
   - 立刻觸發 GitHub Actions，用現有的 Python 解析表格與正文
   - 解析完再推一則**詳細** Telegram，並更新網站圖表
3. 網站上有一張**多窗格圖**（取代舊的「每週融資」圖）：
   - 上：BTC 日線  
   - 中：MSTR 日線  
   - 下：融資／回購／賣幣金額柱狀圖（回購與賣幣是負的）  
   - 底：mNAV  
4. 原本的價位警報、mNAV、ETF、Telegram 每日摘要**都還在**，沒有拿掉。

### 為什麼不用「全部塞進 Cloudflare Worker」？

免費方案的 Worker **每次 Cron 只能用約 10 毫秒 CPU**，解析一整份 8-K 會超時。  
所以採用你確認過的 **方案 A（免費混合）**：

- Worker：每分鐘只做「有沒有新 accession？」＋提供 `/events.json`
- GitHub Actions + Python：真正解析、推詳細 Telegram、把 `data/events.json` 存回 repo，並可選推到 Cloudflare KV

---

## 部署步驟（從頭到尾）

> 假設你已經有這個 GitHub repo：`herry162/btc-monitor`，而且原本的 Actions、Telegram 已經能跑。

### 第 0 步：把程式合併進你的 repo

本機分支名稱建議：`feature/strategy-events`。  
用 PR 合併到 `main`，或直接在 GitHub 網頁上傳／合併皆可。  
**合併後**再繼續下面的 Cloudflare／Secrets 設定。

### 第 1 步：註冊 Cloudflare（免費）

1. 打開 https://dash.cloudflare.com/ 註冊／登入  
2. 左側進 **Workers & Pages**  
3. 若跳出「Claim your free Workers plan」，點同意即可（不用付費）

### 第 2 步：在電腦安裝 Node.js 與 wrangler

1. 安裝 Node.js LTS：https://nodejs.org/（裝好後重開終端機）  
2. 打開終端機，執行：

```bash
cd 你的/btc-monitor/worker
npm install
npx wrangler login
```

瀏覽器會跳出 Cloudflare 授權，允許即可。

### 第 3 步：建立 KV 命名空間

```bash
cd 你的/btc-monitor/worker
npx wrangler kv namespace create btc-monitor-events
```

終端機會印出一串 **id**（類似 `a1b2c3d4e5f6...`）。  
打開 `worker/wrangler.toml`，把兩處：

```toml
id = "REPLACE_WITH_YOUR_KV_NAMESPACE_ID"
preview_id = "REPLACE_WITH_YOUR_KV_NAMESPACE_ID"
```

都改成你的 id。

### 第 4 步：設定 Worker 密鑰（secrets）

每一條都會問你貼上內容（貼上後不會顯示在螢幕上）：

```bash
cd 你的/btc-monitor/worker
npx wrangler secret put SEC_USER_AGENT
# 貼上：btc-monitor 你的真實email@example.com

npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID

npx wrangler secret put GITHUB_TOKEN
# 見下一節「GitHub fine-grained token」
```

（可選）若要用 Grok 備援解析：

```bash
npx wrangler secret put GROK_API_KEY
```

不設也完全能跑。

### 第 5 步：建立 GitHub fine-grained token

用途：讓 Worker 能「按一下」觸發你的 Actions。

1. 打開 https://github.com/settings/personal-access-tokens  
2. **Generate new token** → Fine-grained  
3. Repository access：只選 **herry162/btc-monitor**  
4. Permissions：  
   - **Contents**：Read and write（之後若要用 API 寫檔才需要；目前主要靠 Actions 自己 commit）  
   - **Actions**：Read and write（**一定要**，才能 workflow_dispatch）  
5. 產生後複製 token（只會顯示一次）  
6. 回到上一節，把它貼進 `npx wrangler secret put GITHUB_TOKEN`

為什麼要這麼窄？避免 token 能動到你別的倉庫。

### 第 6 步：部署 Worker

```bash
cd 你的/btc-monitor/worker
npx wrangler deploy
```

成功後會得到網址，例如：

`https://btc-monitor-sec.<你的帳號>.workers.dev`

用瀏覽器打開：

- `…/health` 應看到 `{"ok":true,...}`  
- `…/events.json` 一開始可能是空的，回填後就有資料  

### 第 7 步：GitHub Secrets（repo 設定）

到 repo → **Settings → Secrets and variables → Actions**，確認／新增：

| 名稱 | 用途 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 原本就有 |
| `TELEGRAM_CHAT_ID` | 原本就有 |
| `SEC_USER_AGENT` | `btc-monitor 你的email@…`（SEC 規定要真 email） |
| `SOSOVALUE_API_KEY` | 可選，ETF 用 |
| `CF_ACCOUNT_ID` | Cloudflare 帳號 ID（Dashboard 網址或右側邊欄） |
| `CF_API_TOKEN` | Cloudflare API Token，權限包含 **Account → Workers KV Storage → Edit** |
| `CF_KV_NAMESPACE_ID` | 與 wrangler.toml 同一個 KV id |
| `GROK_API_KEY` | 可選 |

`CF_*` 三個是為了讓 Actions 把 `events.json` **推到 KV**，網站讀 Worker 才會是最新的。  
若暫時不設，Worker 會改從 GitHub raw 讀 `data/events.json`（稍微慢一點，但仍可用）。

### 第 8 步：告訴網站 Worker 網址

編輯 repo 根目錄的 `config.json`：

```json
"events_worker_url": "https://btc-monitor-sec.你的帳號.workers.dev"
```

存檔、推進 `main`。下一次 Actions 跑完，前端就會每 60 秒輪詢這個網址。

### 第 9 步：第一次歷史回填（只要做一次）

在**你自己的電腦**（或 GitHub Actions 手動）跑：

```bash
cd 你的/btc-monitor
export SEC_USER_AGENT="btc-monitor 你的email@example.com"
python3 scripts/backfill_events.py
```

這會：

- 抓 2020-08 至今的 8-K／DEFA14A  
- 寫入 `data/strategy.json`、`data/events.json`、`docs/events.json`  
- 檢查已知可轉債交割與 2026-05-15 回購數字  

通過時會看到：

```text
已知可轉債交割／回購校驗通過
```

然後把更新後的 `data/`、`docs/events.json` commit 推上 GitHub。

### 第 10 步：手機上確認

1. 打開 https://herry162.github.io/btc-monitor  
2. 找到 **Strategy 融資事件** 區塊（多窗格圖）  
3. 試著切「日／週」、勾選類型、點一根柱子看明細  
4. 若 Worker 正常，約一分鐘內偵測到的新公告會閃「新公告」標籤，並推 Telegram  

---

## 本機測試方法

### A. 三份單元測試（必須全過）

```bash
cd 你的/btc-monitor
python3 tests/test_fixtures.py
```

預期輸出最後幾行：

```text
Ran 4 tests in 0.0xxs

OK
```

這四個測試對應：

| 日期 | 必須對上的數字 |
|---|---|
| 2024-11-18 | MSTR ATM 13,593,865 股，淨額約 $4.6B |
| 2026-04-20 | STRC 21,795,389 股／$2,176.3M；MSTR 2,165,000 股／$366.0M |
| 2026-08-03 | MSTR 3,011,361 股／$290.6M；賣出 1,638 BTC；回購 STRC 912,143 股／$81.2M |
| 2026-05-15 | 以約 $1.38B 現金回購 $1.50B 面額 2029 可轉債 |

### B. 可轉債交割校驗

回填後（或已有 `data/strategy.json`）：

```bash
export SEC_USER_AGENT="btc-monitor 你的email@example.com"
python3 scripts/backfill_events.py --validate-only
```

預期：

```text
回購校驗通過： ...
已知可轉債交割／回購校驗通過
```

### C. 本機預覽網站

```bash
cd 你的/btc-monitor
python3 monitor.py --demo          # 產生 docs/data.json（示範用）
# 若 demo 動到 events，再從 data 拷回：
cp data/events.json docs/events.json
cd docs
python3 -m http.server 8765
```

瀏覽器打開 http://127.0.0.1:8765  

### D. Worker 本機

```bash
cd 你的/btc-monitor/worker
npx wrangler dev
```

另開終端機：

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/events.json | head
```

---

## Telegram 長什麼樣？

```text
Strategy 8-K｜美東 8/3 07:31（台灣 19:31）｜期間 7/27–8/2｜MSTR ATM 3,011,361 股，淨額 $290.6M｜用途：…｜賣出 1,638 BTC｜https://www.sec.gov/...
```

特別提醒（門檻在 `config.json` → `issuance`）：

- 宣布／定價／交割可轉債  
- 單週 MSTR ATM ≥ $1,000M（可改 `massive_mstr_weekly_usd_m`）  
- 任何賣幣（`alert_btc_sell`）

---

## 免費方案上限（心裡有數）

| 項目 | 免費大約上限 | 我們怎麼用 |
|---|---|---|
| Workers 請求 | 10 萬次／天 | 1 分鐘 Cron ≈ 1,440 次／天 |
| Cron CPU | 10ms／次 | 只做偵測，不解析 HTML |
| KV 寫入 | 1,000 次／天 | 幾乎只在有新公告時寫 |
| GitHub Actions | 依帳號方案 | 沿用原本排程＋新的 sec-fast |

---

## 常見問題

**Q：Telegram 只有「偵測到」沒有細節？**  
A：看 GitHub → Actions → `sec-fast` 有沒有失敗。通常是 `SEC_USER_AGENT` 沒設，或 SEC 暫時拒絕。

**Q：網站圖是空的？**  
A：先確認 `docs/events.json` 有內容；再確認 `config.json` 的 `events_worker_url` 填對，且 Worker `/events.json` 打得開。

**Q：SEC 說 Undeclared Automated Tool？**  
A：`SEC_USER_AGENT` 必須是「名字 + 真實 email」，例如 `btc-monitor you@gmail.com`。

**Q：想開 Grok 備援？**  
A：在 GitHub Secrets 與（可選）Worker secrets 加 `GROK_API_KEY`。沒有 key 時完全不影響主流程。

**Q：交割日跟 EDGAR 申報日差幾天？**  
A：正常。規格裡的「交割日」是成交日；公司常在隔 1～3 天才交 8-K。校驗允許 ±14 天，金額必須對上。

---

## 檔案對照（給想看結構的人）

| 路徑 | 作用 |
|---|---|
| `sec_strategy.py` | SEC 解析＋事件扁平化 |
| `scripts/backfill_events.py` | 歷史回填與校驗 |
| `tests/test_fixtures.py` | 三份＋回購單元測試 |
| `tests/fixtures/*.html` | 從 EDGAR 下載的原文快照 |
| `worker/` | Cloudflare Worker |
| `docs/index.html` | 前端（Lightweight Charts **5.2.1**） |
| `data/events.json` | 事件列表（repo 備份） |
| `docs/events.json` | 給 GitHub Pages 的回退副本 |
| `.github/workflows/sec-fast.yml` | Worker 觸發的快速解析 |

