/**
 * btc-monitor SEC 偵測 Worker（方案 A：免費混合）
 * - Cron 每分鐘：比對 EDGAR 新 accession → 記 KV → 觸發 GitHub Actions
 * - GET /events.json：從 KV 回傳事件（開 CORS）；沒有就回退到 GitHub raw
 */
import { fetchRecentAccessions } from "./edgar.js";
import { dispatchWorkflow } from "./github_dispatch.js";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "public, max-age=30",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });
}

async function loadEvents(env) {
  const cached = await env.EVENTS_KV.get("events", "json");
  if (cached && Array.isArray(cached.events)) return cached;
  // 回退：GitHub raw（Pages 未部署 data/ 時也能讀）
  const repo = env.GITHUB_REPO || "herry162/btc-monitor";
  const url = `https://raw.githubusercontent.com/${repo}/main/data/events.json`;
  try {
    const r = await fetch(url, { headers: { "User-Agent": "btc-monitor-worker" } });
    if (r.ok) {
      const data = await r.json();
      await env.EVENTS_KV.put("events", JSON.stringify(data));
      return data;
    }
  } catch (e) {
    console.log("raw fallback failed", String(e));
  }
  return { generated_at: 0, count: 0, events: [] };
}

async function checkNewFilings(env) {
  const ua = (env.SEC_USER_AGENT || `${env.SEC_UA_FALLBACK || "btc-monitor"} worker@example.com`).trim();
  const recent = await fetchRecentAccessions(ua);
  const last = (await env.EVENTS_KV.get("last_seen", "json")) || { accessions: [] };
  const seen = new Set(last.accessions || []);
  const fresh = [];
  for (const [acc, meta] of recent.entries()) {
    if (!seen.has(acc)) fresh.push({ accession: acc, ...meta });
  }
  // 第一次啟動：只記錄、不狂觸發
  if (!last.accessions || last.accessions.length === 0) {
    const seed = [...recent.keys()].slice(0, 40);
    await env.EVENTS_KV.put(
      "last_seen",
      JSON.stringify({ accessions: seed, updated_at: Date.now(), seeded: true }),
    );
    return { seeded: true, fresh: [], checked: recent.size };
  }

  const results = [];
  for (const f of fresh) {
    let dispatched = null;
    if (env.GITHUB_TOKEN) {
      dispatched = await dispatchWorkflow({
        token: env.GITHUB_TOKEN,
        repo: env.GITHUB_REPO || "herry162/btc-monitor",
        workflow: env.GITHUB_WORKFLOW || "sec-fast.yml",
        accession: f.accession,
        form: f.form,
        filed: f.filed,
      });
    }
    // 輕量 Telegram：只說「偵測到新申報」，詳細內容由 Actions 再推
    if (env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID) {
      const text =
        `Strategy 新申報偵測｜${f.form || "?"}｜${f.filed || "日期未知"}｜accession ${f.accession}\n` +
        `來源：${f.source}。正在觸發解析…`;
      try {
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            chat_id: env.TELEGRAM_CHAT_ID,
            text,
            disable_web_page_preview: "true",
          }),
        });
      } catch (e) {
        console.log("tg fail", String(e));
      }
    }
    results.push({ ...f, dispatched });
    seen.add(f.accession);
  }

  // 只保留最近 200 個 accession
  const merged = [...seen].slice(-200);
  await env.EVENTS_KV.put(
    "last_seen",
    JSON.stringify({ accessions: merged, updated_at: Date.now() }),
  );
  await env.EVENTS_KV.put(
    "last_check",
    JSON.stringify({ at: Date.now(), checked: recent.size, fresh: results.length }),
  );
  return { seeded: false, fresh: results, checked: recent.size };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (url.pathname === "/" || url.pathname === "/health") {
      const check = await env.EVENTS_KV.get("last_check", "json");
      return json({ ok: true, service: "btc-monitor-sec", last_check: check });
    }
    if (url.pathname === "/events.json") {
      const data = await loadEvents(env);
      return json(data);
    }
    // 手動觸發檢查（本機測試用；正式靠 Cron）
    if (url.pathname === "/check") {
      const out = await checkNewFilings(env);
      return json(out);
    }
    return json({ error: "not found" }, 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      checkNewFilings(env).then((r) => console.log("cron", JSON.stringify(r))),
    );
  },
};
