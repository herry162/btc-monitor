/** 用 fine-grained token 觸發 GitHub Actions workflow_dispatch。 */

export async function dispatchWorkflow({ token, repo, workflow, accession, form, filed }) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`;
  const body = {
    ref: "main",
    inputs: {
      accession: accession || "",
      form: form || "",
      filed: filed || "",
    },
  };
  const r = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "btc-monitor-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (r.status === 204 || r.status === 200) return { ok: true };
  const text = await r.text();
  return { ok: false, status: r.status, text: text.slice(0, 300) };
}
