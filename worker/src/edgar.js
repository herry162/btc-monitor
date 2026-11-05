/** SEC EDGAR 輕量偵測：只比對 accession，不做 HTML 解析（免費方案 CPU 只有 10ms）。 */

const CIK = "0001050446";
const SUBMISSIONS = `https://data.sec.gov/submissions/CIK${CIK}.json`;
const ATOM =
  "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001050446&type=&dateb=&owner=exclude&count=40&output=atom";

const MONITORED = new Set(["8-K", "8-K/A", "10-Q", "10-K", "DEFA14A"]);

function isMonitored(form) {
  if (!form) return false;
  if (MONITORED.has(form)) return true;
  return form.startsWith("424B");
}

export async function fetchRecentAccessions(userAgent) {
  const headers = {
    "User-Agent": userAgent,
    Accept: "application/json, application/atom+xml;q=0.9, */*;q=0.8",
  };
  const found = new Map(); // accession -> {form, filed, acceptance, source}

  // 1) submissions JSON（較完整）
  try {
    const r = await fetch(SUBMISSIONS, { headers });
    if (r.ok) {
      const j = await r.json();
      const block = j.filings.recent;
      const n = block.accessionNumber.length;
      const adt = block.acceptanceDateTime || [];
      for (let i = 0; i < Math.min(n, 40); i++) {
        const form = block.form[i];
        if (!isMonitored(form)) continue;
        found.set(block.accessionNumber[i], {
          form,
          filed: block.filingDate[i],
          acceptance: adt[i] || null,
          source: "submissions",
        });
      }
    }
  } catch (e) {
    console.log("submissions error", String(e));
  }

  // 2) Atom feed（有時比較快出現）
  try {
    const r = await fetch(ATOM, { headers });
    if (r.ok) {
      const xml = await r.text();
      const re =
        /accession-number=([0-9-]+)[\s\S]*?term="([^"]+)"[\s\S]*?<updated>([^<]+)<\/updated>/gi;
      // Atom 結構不一定固定，改用兩段掃描
      const accs = [...xml.matchAll(/accession-number=([0-9-]+)/g)].map((m) => m[1]);
      const forms = [...xml.matchAll(/term="([^"]+)"/g)].map((m) => m[1]);
      for (let i = 0; i < accs.length; i++) {
        const form = forms[i] || null;
        if (form && !isMonitored(form)) continue;
        if (!found.has(accs[i])) {
          found.set(accs[i], {
            form: form || "unknown",
            filed: null,
            acceptance: null,
            source: "atom",
          });
        } else if (found.get(accs[i]).source === "submissions") {
          // 兩個來源都有：標記
          found.get(accs[i]).source = "both";
        }
      }
    }
  } catch (e) {
    console.log("atom error", String(e));
  }

  return found;
}
