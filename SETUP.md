# Setup

The code is done. These are the steps that need your credentials and
accounts, so they cannot be scripted from here.

Order matters: secret first, then a manual run to prove it works, then
Pages, then the scheduler.

---

## 1. Repository secret

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `WRL_API_KEY` | your key from WRL → Integrations → Developer API |

The workflows fail with a clear message if this is missing, rather than
running and writing nothing.

---

## 2. Prove the workflow runs

Before wiring a scheduler to something untested, run it by hand.

**Actions → Collect full log → Run workflow**, choosing this branch.

It should check out, install, pull ~86 pages, and either commit updated
JSON or report "No change in data/latest". If it fails, fix that now —
a scheduler pointed at a broken workflow just fails on a timer.

---

## 3. GitHub Pages

**Settings → Pages → Source: Deploy from a branch**, then pick the branch
this lives on and folder `/ (root)`.

No workflow needed; Pages serves the branch directly. The boards will be at:

```
https://jmills06.github.io/wrl-boards/career.html
https://jmills06.github.io/wrl-boards/grids.html
https://jmills06.github.io/wrl-boards/recent.html
```

The first deploy takes a couple of minutes. Check one in a browser before
pointing DakBoard at it.

---

## 4. Fine-grained PAT

The scheduler needs a token to trigger workflows. Scope it to this one
repository — a token that can reach every repo you own is a bad thing to
paste into a third-party scheduler.

**Settings (your account) → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**

| Field | Value |
|---|---|
| Repository access | Only select repositories → `jmills06/wrl-boards` |
| Repository permissions → Actions | Read and write |
| Repository permissions → Contents | Read and write |
| Expiration | Your call; set a calendar reminder to rotate it |

Copy it once. It is not shown again.

---

## 5. cron-job.org

Two jobs. GitHub's own `schedule:` cron is deliberately unused: it is
unreliable and silently skips runs under load.

Both jobs are **POST** requests. In cron-job.org, headers go in separate
Key/Value fields, not as one pasted blob.

### Common to both

**Headers**

| Key | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer YOUR_PAT` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |
| `User-Agent` | `wrl-boards-cron` |

`User-Agent` is not optional. GitHub rejects API requests without one.

**Body** — set `ref` to the branch Pages is serving:

```json
{"ref":"main"}
```

### Job A — recent, every 30 minutes

- **URL**
  `https://api.github.com/repos/jmills06/wrl-boards/actions/workflows/collect-recent.yml/dispatches`
- **Schedule**: every 30 minutes
- **Expected response**: `204 No Content`

### Job B — full, nightly

- **URL**
  `https://api.github.com/repos/jmills06/wrl-boards/actions/workflows/collect-full.yml/dispatches`
- **Schedule**: once daily, a quiet hour — 08:10 UTC (04:10 Eastern) keeps
  it clear of the :00 and :30 recent runs
- **Expected response**: `204 No Content`

A successful dispatch returns **204 with an empty body**. cron-job.org may
show that as a failure if it is configured to expect body content; treat
204 as success.

### Testing a job before trusting the schedule

```powershell
curl.exe -i -X POST `
  -H "Accept: application/vnd.github+json" `
  -H "Authorization: Bearer YOUR_PAT" `
  -H "X-GitHub-Api-Version: 2022-11-28" `
  -H "User-Agent: wrl-boards-cron" `
  -H "Content-Type: application/json" `
  -d '{\"ref\":\"main\"}' `
  https://api.github.com/repos/jmills06/wrl-boards/actions/workflows/collect-recent.yml/dispatches
```

`204` means dispatched — check the Actions tab. `404` usually means the PAT
lacks Actions write, or the workflow file is not on the branch named in `ref`.

---

## 6. DakBoard

Add each board as a **Web Page** source, portrait 1080x1920.

`AUTO_FIT` is already `false` in all three, which is what you want on a real
1080x1920 panel. Set it to `true` only to view a board in a desktop browser
window.

Suggested rotation matching how often the data actually changes:

| Board | Dwell |
|---|---|
| `recent.html` | longest — it is the one that changes |
| `career.html` | shorter |
| `grids.html` | shorter |

---

## Running it locally

```powershell
[Environment]::SetEnvironmentVariable("WRL_API_KEY", "your key", "User")   # once
# open a new PowerShell window for that to take effect

python collect.py --mode=full --dry-run   # writes nothing
python collect.py --mode=full
python -m http.server 8000                # then browse localhost:8000/career.html
```

`file://` will not work — the boards fetch a relative path, which needs a
server.

---

## When something looks wrong

| Symptom | Where to look |
|---|---|
| Board shows a **Stale** badge | The collector has not published in a while. Check the Actions tab. |
| Board shows "Waiting for data" | It has never loaded the JSON. Check the Pages URL and that `data/latest/*.json` is committed. |
| Numbers frozen but no badge | The collector is running and writing identical data — check WRL is receiving your log. |
| Workflow fails on push | Another run pushed mid-collect. The retry loop handles this; five consecutive failures means something else. |
| Run says "log shrank" | Deliberate guard against a partial pull wiping good data. If the deletion was real, run the full workflow with `force` ticked. |
