# App Metrics MCP — README

A local MCP server that exposes Google Play and App Store Connect metrics
(downloads, uninstalls, active devices, crashes, ratings) so Claude — and
Claude live artifacts — can pull live numbers.

```
SETUP.md          ← do this first: get your API credentials
.env.example      ← copy to .env and fill in
server.py         ← the MCP server
requirements.txt  ← Python deps
```

---

## 1. Get your credentials

Follow `SETUP.md` end to end. It walks through both consoles, what to click,
and what to copy. ~15–20 minutes if you've never done it before.

When done you should have:
- `service-account.json` (Google)
- `AuthKey_XXXXXX.p8` (Apple)
- A handful of IDs

Drop everything into a `.env` file in this folder (copy from `.env.example`).

---

## 2. Install Python deps

```bash
cd app-metrics-mcp
python -m venv .venv
source .venv/bin/activate          # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
```

Python 3.10+ required.

---

## 3. Smoke-test

With your `.venv` active and `.env` filled in, run:

```bash
python -c "from server import list_apps; import json; print(json.dumps(list_apps(), indent=2))"
```

You should see your app(s) listed under both `play` and `appStore`. If either
side errors, fix that before continuing — the other tools depend on the same
credentials. Common fixes:

- *Google Play "permission denied"* → service account hasn't been granted in
  Play Console yet. Re-check Setup §A.4.
- *App Store "401 Unauthorized"* → key revoked, or `.p8` path/Issuer/Key ID
  don't match. Re-check Setup §B.1.

---

## 4. Add the server to Cowork

In Cowork's MCP settings, add this server. The exact UI varies; the
configuration values are:

```json
{
  "name": "app-metrics",
  "command": "/absolute/path/to/app-metrics-mcp/.venv/bin/python",
  "args": ["/absolute/path/to/app-metrics-mcp/server.py"],
  "env": {}
}
```

Use the **absolute path** to the venv's `python` so dependencies resolve. The
`.env` file is loaded automatically by `server.py`, so you don't need to
duplicate the credentials in `env`.

After saving, restart Cowork. You should see tools like `play_vitals`,
`play_reviews`, `play_installs_uninstalls`, `appstore_sales`,
`appstore_reviews`, etc. become available.

---

## 5. Verify in Claude

Ask: *"Use list_apps to confirm my Play and App Store credentials work."*

You should get back the apps visible to your credentials. If yes, you're
ready for phase 2 (the live artifact dashboard).

---

## What each tool does

| Tool | Source | What you get |
|------|--------|--------------|
| `play_vitals` | Play Reporting API | Crash rate, ANR rate, slow start, slow rendering, errors — daily series |
| `play_reviews` | Play androidpublisher API | Recent reviews, average, distribution |
| `play_installs_uninstalls` | Cloud Storage CSV export | Daily installs, uninstalls, active devices |
| `appstore_sales` | ASC Sales Report | Daily/weekly downloads (units) by territory |
| `appstore_reviews` | ASC Customer Reviews | Recent reviews, average, distribution |
| `appstore_analytics_request` | ASC Analytics Reports | Kicks off analytics access for the app (one-time) |
| `appstore_analytics_list_reports` | ASC Analytics Reports | Lists available report families (installs, deletions, sessions, crashes) |
| `appstore_analytics_list_instances` | ASC Analytics Reports | Lists daily/weekly snapshots of a report |
| `appstore_analytics_download` | ASC Analytics Reports | Downloads + parses a snapshot as TSV |
| `list_apps` | Both | Sanity-check connectivity |

The App Store Connect Analytics flow has three steps because Apple processes
analytics asynchronously: `request → list_reports → list_instances →
download`. The first time you call `appstore_analytics_request` for an app,
Apple takes up to 48 hours to start producing reports. After that they
update on a daily/weekly schedule.

---

## Phase 2 — the live artifact

Once `list_apps` works, ask Claude something like:

> "Build the live artifact dashboard. Use `play_installs_uninstalls`,
>  `play_vitals`, `play_reviews`, `appstore_sales`, `appstore_reviews`, and
>  the `appstore_analytics_*` tools to populate it."

Claude will build a Cowork artifact that calls these tools every time the
artifact opens, so the numbers stay current without you doing anything.

## Planned panels (v1 + v2)

v1 panels (no extra setup beyond credentials):
- Headline KPIs — downloads, uninstalls, active devices, crash rate, average rating
- Daily downloads chart — iOS vs Android lines
- Per-store breakdown table — DAU, crash rate, rating
- Recent reviews — last 10 reviews from each store with stars + text

v2 panels (require additional tools wired into `server.py`; flagged with their data source):
- **Geography panel** — top countries by installs and active devices, side by side iOS/Android. Android: from per-country CSV in the pubsite GCS bucket. iOS: from App Store Connect Analytics Reports (async).
- **iOS acquisition sources + search terms** — share of installs from Search vs Browse vs Web Referrer vs App Referrer, plus top search terms. Source: App Store Connect Analytics Reports.
- **Per-version health** — crash rate + average rating for top 3 active app versions, % of users on latest. Android: Reporting API with version dimension + per-version CSV. iOS: Analytics Reports.
- **iOS conversion funnel** — App Store impressions → product page views → installs, with conversion %. Source: App Store Connect Analytics Reports.

Most v2 iOS panels depend on Apple's async Analytics Reports flow, which has a one-time 24–48h lag from the first `analyticsReportRequest` call before data starts appearing. The plan is to fire that request as soon as the .p8 key is in hand so the lag clock starts running early.

---

## Security notes

- Credentials never leave your machine. The MCP server runs locally; only
  the *results* of metric queries flow through Claude.
- The `.gitignore` in this folder excludes `.env` and `*.p8` and `*.json` so
  you can safely git-init the project without leaking secrets.
- Both consoles let you revoke any key in one click if anything ever feels
  off.
- All scopes used are read-only.
