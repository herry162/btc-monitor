/**
 * btc-monitor SEC 偵測 Worker（方案 A：免費混合）
 * - Cron 每分鐘：比對 EDGAR 新 accession →（僅有新件時）記 KV → 觸發 GitHub Actions
 * - GET /events.json：GitHub raw + Workers Cache API（不寫 KV）
 * - GET /health：記憶體 last_check，跨 isolate 回退到最多每小時一次的 KV 心跳
 *
 * KV put 策略（免費額度 1000/天，目標典型 <100/天）：
 * - last_seen：只在 seed 或出現新 accession 時寫
 * - last_check：最多每小時寫一次（供 /health）
 * - events：不再寫入 KV（改 Cache API）
 */
import { fetchRecentAccessions } from "./edgar.js";
import { dispatchWorkflow } from "./github_dispatch.js";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "public, max-age=30",
};

const EVENTS_CACHE_URL = "https://btc-monitor-sec.internal/events.json";
const EVENTS_TTL_SEC = 60;
/** last_check 最多每小時寫一次 KV */
const HEARTBEAT_INTERVAL_MS = 60 * 60 * 1000;

/** isolate 內記憶體；冷啟動後可能為 null，/health 會回退讀 KV */
let memLastCheck = null;
/** 本 isolate 上次寫入 last_check 的時間，用來避免每分鐘 get+put */
let memHeartbeatWrittenAt = 0;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });
}

/** 純函式：是否該寫 last_seen（給單元測試） */
export function shouldWriteLastSeen({ seeded, freshCount }) {
  return Boolean(seeded) || Number(freshCount) > 0;
}

/** 純函式：距離上次心跳是否已超過間隔 */
export function shouldWriteHeartbeat(now, lastWrittenAt, intervalMs = HEARTBEAT_INTERVAL_MS) {
  return !lastWrittenAt || now - lastWrittenAt >= intervalMs;
}

async function loadEvents(env) {
  const cache = caches.default;
  const cacheKey = new Request(EVENTS_CACHE_URL);
  try {
    const hit = await cache.match(cacheKey);
    if (hit) {
      return await hit.json();
    }
  } catch (e) {
    console.log("cache match fail", String(e));
  }

  const repo = env.GITHUB_REPO || "herry162/btc-monitor";
  const url = `https://raw.githubusercontent.com/${repo}/main/data/events.json`;
  try {
    const r = await fetch(url, { headers: { "User-Agent": "btc-monitor-worker" } });
    if (r.ok) {
      const data = await r.json();
      const body = JSON.stringify(data);
      const resp = new Response(body, {
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": `public, max-age=${EVENTS_TTL_SEC}`,
        },
      });
      try {
        await cache.put(cacheKey, resp.clone());
      } catch (e) {
        console.log("cache put fail", String(e));
      }
      return data;
    }
    console.log("raw events status", r.status);
  } catch (e) {
    console.log("raw fallback failed", String(e));
  }
  return { generated_at: 0, count: 0, events: [], note: "waiting for main/data/events.json" };
}

async function writeHeartbeatIfDue(env, payload) {
  memLastCheck = payload;
  const now = Number(payload.at) || Date.now();
  if (!shouldWriteHeartbeat(now, memHeartbeatWrittenAt, HEARTBEAT_INTERVAL_MS)) {
    return { wrote: false, reason: "mem_throttle" };
  }
  // 跨 isolate：讀一次 KV，避免多 isolate 同時每小時各寫多次仍可控
  let prevAt = 0;
  try {
    const prev = await env.EVENTS_KV.get("last_check", "json");
    prevAt = Number((prev && prev.at) || 0);
  } catch (e) {
    console.log("last_check get fail", String(e));
  }
  if (!shouldWriteHeartbeat(now, prevAt, HEARTBEAT_INTERVAL_MS)) {
    memHeartbeatWrittenAt = prevAt; // 對齊，之後本 isolate 也節流
    return { wrote: false, reason: "kv_throttle" };
  }
  await env.EVENTS_KV.put("last_check", JSON.stringify(payload));
  memHeartbeatWrittenAt = now;
  return { wrote: true };
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
    const heartbeat = await writeHeartbeatIfDue(env, {
      at: Date.now(),
      checked: recent.size,
      fresh: 0,
      seeded: true,
    });
    return { seeded: true, fresh: [], checked: recent.size, kv: { last_seen: true, heartbeat } };
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

  let wroteLastSeen = false;
  if (shouldWriteLastSeen({ seeded: false, freshCount: results.length })) {
    // 只保留最近 200 個 accession
    const merged = [...seen].slice(-200);
    await env.EVENTS_KV.put(
      "last_seen",
      JSON.stringify({ accessions: merged, updated_at: Date.now() }),
    );
    wroteLastSeen = true;
  }

  const heartbeat = await writeHeartbeatIfDue(env, {
    at: Date.now(),
    checked: recent.size,
    fresh: results.length,
  });
  return {
    seeded: false,
    fresh: results,
    checked: recent.size,
    kv: { last_seen: wroteLastSeen, heartbeat },
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (url.pathname === "/" || url.pathname === "/health") {
      let check = memLastCheck;
      if (!check) {
        try {
          check = await env.EVENTS_KV.get("last_check", "json");
        } catch (e) {
          console.log("health kv fail", String(e));
        }
      }
      return json({
        ok: true,
        service: "btc-monitor-sec",
        last_check: check,
        kv_put_policy: "last_seen_on_change; last_check_hourly; events_via_cache_api",
      });
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
