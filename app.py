"""
Coinmania App Metrics — TV Dashboard Server
============================================
Runs a web server that:
  · Fetches iOS metrics from App Store Connect on startup and every REFRESH_HOURS
  · Serves a TV-optimised dashboard at  /
  · Exposes raw JSON at  /data
  · Accepts a manual refresh at  POST /refresh?secret=<REFRESH_SECRET>

Environment variables (set in Railway dashboard or .env):
  ASC_KEY_ID          – 10-char key ID, e.g. DQPB76VDR5
  ASC_ISSUER_ID       – UUID from App Store Connect
  ASC_PRIVATE_KEY     – full content of the .p8 file  (cloud / Railway)
  ASC_PRIVATE_KEY_PATH– path to the .p8 file          (local fallback)
  ASC_VENDOR_NUMBER   – numeric vendor number
  ASC_APP_ID          – numeric app ID
  REFRESH_HOURS       – how often to refresh data, default 6
  REFRESH_SECRET      – optional password for POST /refresh
  PORT                – port to listen on, default 8000
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import time
import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ASC_KEY_ID        = os.environ.get("ASC_KEY_ID", "")
ASC_ISSUER_ID     = os.environ.get("ASC_ISSUER_ID", "")
ASC_VENDOR_NUMBER = os.environ.get("ASC_VENDOR_NUMBER", "")
ASC_APP_ID        = os.environ.get("ASC_APP_ID", "")
ASC_BASE          = "https://api.appstoreconnect.apple.com"
REFRESH_HOURS     = int(os.environ.get("REFRESH_HOURS", "6"))
REFRESH_SECRET    = os.environ.get("REFRESH_SECRET", "")

# Key content: prefer ASC_PRIVATE_KEY env var (cloud), fall back to file path (local)
_raw_key = os.environ.get("ASC_PRIVATE_KEY", "").strip()
if not _raw_key:
    _key_path = os.environ.get("ASC_PRIVATE_KEY_PATH", "")
    if _key_path and Path(_key_path).exists():
        _raw_key = Path(_key_path).read_text().strip()
ASC_PRIVATE_KEY = _raw_key

app = Flask(__name__)

# ── In-memory cache ───────────────────────────────────────────────────────────

_cache: dict[str, Any] = {
    "data":      None,
    "updatedAt": None,
    "error":     None,
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def _jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": ASC_ISSUER_ID,
            "iat": now,
            "exp": now + 1140,          # 19 minutes
            "aud": "appstoreconnect-v1",
        },
        ASC_PRIVATE_KEY,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": ASC_KEY_ID, "typ": "JWT"},
    )


def _get(path: str, params=None, accept="application/json") -> httpx.Response:
    url = path if path.startswith("http") else f"{ASC_BASE}{path}"
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {_jwt()}", "Accept": accept},
        params=params,
        timeout=60.0,
    )
    r.raise_for_status()
    return r

# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_reviews() -> dict:
    data = _get(
        f"/v1/apps/{ASC_APP_ID}/customerReviews",
        params={"limit": 50, "sort": "-createdDate"},
    ).json().get("data", [])

    ratings = [
        d["attributes"]["rating"]
        for d in data
        if d.get("attributes", {}).get("rating") is not None
    ]
    avg = round(sum(ratings) / len(ratings), 1) if ratings else None
    dist = {str(s): ratings.count(s) for s in [5, 4, 3, 2, 1]}

    return {
        "count":        len(data),
        "average":      avg,
        "distribution": dist,
        "recent": [
            {
                "rating":    d["attributes"].get("rating"),
                "title":     d["attributes"].get("title") or "",
                "body":      d["attributes"].get("body") or "",
                "date":      (d["attributes"].get("createdDate") or "")[:10],
                "territory": d["attributes"].get("territory") or "",
            }
            for d in data[:20]
        ],
    }


def _fetch_sales() -> dict:
    yesterday = (dt.datetime.utcnow().date() - dt.timedelta(days=1)).isoformat()
    params = {
        "filter[frequency]":     "DAILY",
        "filter[reportType]":    "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]":  ASC_VENDOR_NUMBER,
        "filter[reportDate]":    yesterday,
        "filter[version]":       "1_1",
    }
    try:
        r = _get("/v1/salesReports", params=params, accept="application/a-gzip")
        rows = list(
            csv.DictReader(
                io.StringIO(gzip.decompress(r.content).decode("utf-8")),
                delimiter="\t",
            )
        )
        ours = [row for row in rows if row.get("Apple Identifier") == ASC_APP_ID] or rows
        units = sum(
            int(row["Units"]) for row in ours if str(row.get("Units", "")).isdigit()
        )
        return {"date": yesterday, "units": units}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Apple publishes reports ~24h late; this is normal
            return {"date": yesterday, "units": None, "note": "Report not yet available for this date"}
        raise


def refresh() -> None:
    ts = dt.datetime.utcnow().isoformat()
    try:
        _cache["data"] = {
            "reviews": _fetch_reviews(),
            "sales":   _fetch_sales(),
        }
        _cache["updatedAt"] = dt.datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")
        _cache["error"] = None
        print(f"[{ts}] Data refreshed OK")
    except Exception as exc:
        _cache["error"] = str(exc)
        print(f"[{ts}] Refresh error: {exc}")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/data")
def data_route():
    return jsonify(_cache)


@app.route("/refresh", methods=["POST"])
def manual_refresh():
    if REFRESH_SECRET and request.args.get("secret") != REFRESH_SECRET:
        return jsonify({"error": "forbidden"}), 403
    refresh()
    return jsonify({"ok": True, "updatedAt": _cache["updatedAt"], "error": _cache["error"]})


@app.route("/")
def dashboard():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coinmania — App Metrics</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0d0d0f;
    --card:    #18181b;
    --border:  #27272a;
    --text:    #f4f4f5;
    --muted:   #71717a;
    --gold:    #fbbf24;
    --blue:    #3b82f6;
    --green:   #22c55e;
    --red:     #ef4444;
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 18px;
    -webkit-font-smoothing: antialiased;
  }

  body {
    display: flex;
    flex-direction: column;
    padding: 36px 48px 28px;
    gap: 28px;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
  }
  .logo {
    font-size: 1.9rem;
    font-weight: 800;
    letter-spacing: -0.03em;
  }
  .logo em { color: var(--gold); font-style: normal; }
  .logo-sub {
    font-size: 0.85rem;
    color: var(--muted);
    font-weight: 400;
    margin-left: 12px;
    letter-spacing: 0;
  }
  .header-right {
    text-align: right;
    font-size: 0.82rem;
    color: var(--muted);
    line-height: 1.6;
  }
  #countdown { color: var(--blue); font-weight: 600; }

  /* ── KPI row ── */
  .kpis {
    display: grid;
    grid-template-columns: 1fr 1.6fr 1fr;
    gap: 20px;
    flex-shrink: 0;
  }
  .kpi {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 28px 32px 24px;
  }
  .kpi-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin-bottom: 12px;
  }
  .kpi-value {
    font-size: 3.6rem;
    font-weight: 800;
    line-height: 1;
    letter-spacing: -0.04em;
  }
  .kpi-sub {
    font-size: 0.8rem;
    color: var(--muted);
    margin-top: 8px;
  }
  .c-gold  { color: var(--gold); }
  .c-blue  { color: var(--blue); }
  .c-green { color: var(--green); }

  /* star distribution inside rating card */
  .dist {
    margin-top: 18px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .dist-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.78rem;
    color: var(--muted);
  }
  .dist-label { width: 22px; text-align: right; flex-shrink: 0; }
  .dist-track {
    flex: 1;
    height: 7px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .dist-fill {
    height: 100%;
    background: var(--gold);
    border-radius: 4px;
    transition: width 0.7s cubic-bezier(.4,0,.2,1);
  }
  .dist-count { width: 22px; flex-shrink: 0; }

  /* ── Error banner ── */
  .error-bar {
    background: #1f0a0a;
    border: 1px solid #7f1d1d;
    border-radius: 12px;
    padding: 12px 20px;
    color: #fca5a5;
    font-size: 0.88rem;
    flex-shrink: 0;
  }

  /* ── Reviews ── */
  .section-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    flex-shrink: 0;
  }
  .reviews-grid {
    flex: 1;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
    align-content: start;
    overflow: hidden;
  }
  .rv {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 22px;
  }
  .rv-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 10px;
  }
  .rv-stars { color: var(--gold); font-size: 1.05rem; letter-spacing: 2px; }
  .rv-meta  { font-size: 0.72rem; color: var(--muted); text-align: right; line-height: 1.5; }
  .rv-title { font-size: 0.9rem; font-weight: 600; color: #d4d4d8; margin-bottom: 5px; }
  .rv-body  {
    font-size: 0.82rem;
    color: var(--muted);
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  /* ── Placeholder ── */
  .placeholder { color: var(--muted); font-size: 0.9rem; }
</style>
</head>
<body>

<header>
  <div>
    <span class="logo">Coin<em>mania</em><span class="logo-sub">iOS App Metrics</span></span>
  </div>
  <div class="header-right">
    <div id="updated-at">Loading…</div>
    <div>Next refresh in <span id="countdown">—</span></div>
  </div>
</header>

<div id="error-bar" class="error-bar" style="display:none"></div>

<div class="kpis">

  <div class="kpi">
    <div class="kpi-label">Downloads &mdash; yesterday</div>
    <div class="kpi-value c-blue" id="kpi-dl">—</div>
    <div class="kpi-sub" id="kpi-dl-sub">&nbsp;</div>
  </div>

  <div class="kpi">
    <div class="kpi-label">Average Rating &nbsp;·&nbsp; App Store</div>
    <div class="kpi-value c-gold" id="kpi-rating">—</div>
    <div class="kpi-sub" id="kpi-rating-sub">&nbsp;</div>
    <div class="dist" id="dist"></div>
  </div>

  <div class="kpi">
    <div class="kpi-label">Reviews fetched</div>
    <div class="kpi-value c-green" id="kpi-count">—</div>
    <div class="kpi-sub">most recent 50</div>
  </div>

</div>

<div class="section-label">Recent Reviews</div>
<div class="reviews-grid" id="reviews-grid">
  <div class="placeholder">Loading reviews…</div>
</div>

<script>
// ── helpers ──────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function starStr(n) {
  const filled = '★'.repeat(Math.max(0, n));
  const empty  = '☆'.repeat(Math.max(0, 5 - n));
  return filled + empty;
}

// ── render ───────────────────────────────────────────────────────────────────

function render(cache) {
  // header
  document.getElementById('updated-at').textContent =
    cache.updatedAt ? 'Updated ' + cache.updatedAt : 'Not yet fetched';

  // error
  const errEl = document.getElementById('error-bar');
  if (cache.error) {
    errEl.textContent = '⚠ ' + cache.error;
    errEl.style.display = 'block';
  } else {
    errEl.style.display = 'none';
  }

  if (!cache.data) return;

  const { reviews, sales } = cache.data;

  // Downloads KPI
  const dlEl  = document.getElementById('kpi-dl');
  const dlSub = document.getElementById('kpi-dl-sub');
  if (sales.units !== null && sales.units !== undefined) {
    dlEl.textContent  = sales.units.toLocaleString();
    dlSub.textContent = sales.date;
  } else {
    dlEl.textContent  = '—';
    dlSub.textContent = sales.note || '';
  }

  // Rating KPI
  document.getElementById('kpi-rating').textContent =
    reviews.average !== null ? reviews.average + ' ★' : '—';
  document.getElementById('kpi-rating-sub').textContent =
    reviews.count + ' ratings';

  // Distribution bars
  const dist  = reviews.distribution || {};
  const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
  document.getElementById('dist').innerHTML = [5, 4, 3, 2, 1].map(s => {
    const pct = Math.round((dist[s] || 0) / total * 100);
    return `<div class="dist-row">
      <span class="dist-label">${s}★</span>
      <div class="dist-track">
        <div class="dist-fill" style="width:${pct}%"></div>
      </div>
      <span class="dist-count">${dist[s] || 0}</span>
    </div>`;
  }).join('');

  // Review count KPI
  document.getElementById('kpi-count').textContent = reviews.count;

  // Reviews grid
  const grid = document.getElementById('reviews-grid');
  if (!reviews.recent || reviews.recent.length === 0) {
    grid.innerHTML = '<div class="placeholder">No reviews found.</div>';
    return;
  }
  grid.innerHTML = reviews.recent.map(rv => `
    <div class="rv">
      <div class="rv-top">
        <span class="rv-stars">${starStr(rv.rating || 0)}</span>
        <span class="rv-meta">${esc(rv.territory)}<br>${esc(rv.date)}</span>
      </div>
      ${rv.title ? `<div class="rv-title">${esc(rv.title)}</div>` : ''}
      <div class="rv-body">${esc(rv.body)}</div>
    </div>
  `).join('');
}

// ── countdown timer ──────────────────────────────────────────────────────────

const POLL_MS      = 5 * 60 * 1000;   // poll server every 5 min
const REFRESH_SECS = 6 * 60 * 60;     // matches server's REFRESH_HOURS default
let nextRefreshAt  = Date.now() + REFRESH_SECS * 1000;

function updateCountdown() {
  const secs = Math.max(0, Math.round((nextRefreshAt - Date.now()) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  document.getElementById('countdown').textContent =
    h > 0
      ? `${h}h ${String(m).padStart(2,'0')}m`
      : `${m}m ${String(s).padStart(2,'0')}s`;
}

// ── polling ───────────────────────────────────────────────────────────────────

let lastUpdatedAt = null;

async function poll() {
  try {
    const res  = await fetch('/data');
    const json = await res.json();
    render(json);
    if (json.updatedAt && json.updatedAt !== lastUpdatedAt) {
      lastUpdatedAt = json.updatedAt;
      nextRefreshAt = Date.now() + REFRESH_SECS * 1000;
    }
  } catch (e) {
    console.error('poll error', e);
  }
}

poll();
setInterval(poll, POLL_MS);
setInterval(updateCountdown, 1000);
updateCountdown();
</script>
</body>
</html>
"""

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ASC_PRIVATE_KEY:
        print("WARNING: ASC_PRIVATE_KEY / ASC_PRIVATE_KEY_PATH not set — metrics will fail")

    refresh()  # fetch immediately on start

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(refresh, "interval", hours=REFRESH_HOURS)
    scheduler.start()

    port = int(os.environ.get("PORT", 8000))
    print(f"Dashboard running at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
