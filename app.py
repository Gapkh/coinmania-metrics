"""
Coinmania App Metrics — TV Dashboard Server v2
"""
from __future__ import annotations
import csv, gzip, io, os, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

ASC_KEY_ID        = os.environ.get("ASC_KEY_ID", "")
ASC_ISSUER_ID     = os.environ.get("ASC_ISSUER_ID", "")
ASC_VENDOR_NUMBER = os.environ.get("ASC_VENDOR_NUMBER", "")
ASC_APP_ID        = os.environ.get("ASC_APP_ID", "")
ASC_BASE          = "https://api.appstoreconnect.apple.com"
REFRESH_HOURS     = int(os.environ.get("REFRESH_HOURS", "6"))
REFRESH_SECRET    = os.environ.get("REFRESH_SECRET", "")

_raw_key = os.environ.get("ASC_PRIVATE_KEY", "").strip()
if not _raw_key:
    _kp = os.environ.get("ASC_PRIVATE_KEY_PATH", "")
    if _kp and Path(_kp).exists():
        _raw_key = Path(_kp).read_text().strip()
ASC_PRIVATE_KEY = _raw_key

app   = Flask(__name__)
_cache: dict[str, Any] = {"data": None, "updatedAt": None, "error": None}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"iss": ASC_ISSUER_ID, "iat": now, "exp": now + 1140, "aud": "appstoreconnect-v1"},
        ASC_PRIVATE_KEY, algorithm="ES256",
        headers={"alg": "ES256", "kid": ASC_KEY_ID, "typ": "JWT"},
    )

def _get(path: str, params=None, accept="application/json") -> httpx.Response:
    url = path if path.startswith("http") else f"{ASC_BASE}{path}"
    r   = httpx.get(url,
            headers={"Authorization": f"Bearer {_jwt()}", "Accept": accept},
            params=params, timeout=60.0)
    r.raise_for_status()
    return r


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_reviews() -> dict:
    data = _get(f"/v1/apps/{ASC_APP_ID}/customerReviews",
                params={"limit": 50, "sort": "-createdDate"}).json().get("data", [])
    ratings = [d["attributes"]["rating"] for d in data
               if d.get("attributes", {}).get("rating") is not None]
    avg  = round(sum(ratings) / len(ratings), 1) if ratings else None
    dist = {str(s): ratings.count(s) for s in [5, 4, 3, 2, 1]}
    return {
        "count": len(data), "average": avg, "distribution": dist,
        "recent": [
            {"rating":    d["attributes"].get("rating"),
             "title":     d["attributes"].get("title") or "",
             "body":      d["attributes"].get("body") or "",
             "date":      (d["attributes"].get("createdDate") or "")[:10],
             "territory": d["attributes"].get("territory") or ""}
            for d in data[:18]
        ],
    }

def _fetch_one_day(date: str) -> tuple[str, dict | None]:
    params = {
        "filter[frequency]": "DAILY", "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY", "filter[vendorNumber]": ASC_VENDOR_NUMBER,
        "filter[reportDate]": date, "filter[version]": "1_1",
    }
    try:
        r    = _get("/v1/salesReports", params=params, accept="application/a-gzip")
        rows = list(csv.DictReader(io.StringIO(gzip.decompress(r.content).decode()), delimiter="\t"))
        # Product Type Identifier:
        #   '1'  = iPhone/iPod download   '3'  = Universal (iPhone+iPad) download
        #   '1T' = iPhone re-download     '3T' = Universal re-download
        #   '7'  = in-app purchase (excluded — not a download)
        DOWNLOAD_TYPES = {"1", "1T", "3", "3T"}
        all_app = [row for row in rows if row.get("Apple Identifier") == ASC_APP_ID] or rows
        ours = [row for row in all_app if row.get("Product Type Identifier", "").strip() in DOWNLOAD_TYPES]
        if not ours:
            ours = all_app  # fallback if column missing
        units = sum(int(row["Units"]) for row in ours if str(row.get("Units","")).isdigit())
        by_cc: dict[str, int] = {}
        for row in ours:
            cc = row.get("Country Code","").strip()
            u  = str(row.get("Units","0")).strip()
            if cc and u.isdigit():
                by_cc[cc] = by_cc.get(cc, 0) + int(u)
        return date, {"units": units, "by_cc": by_cc}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return date, None
        raise

def _fetch_sales_history(days: int = 30) -> dict:
    today = dt.datetime.utcnow().date()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, days + 6)]
    raw: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_day, d): d for d in dates}
        for fut in as_completed(futs):
            try:
                d, res = fut.result()
                if res is not None:
                    raw[d] = res
            except Exception:
                pass

    sorted_dates = sorted(raw)[-days:]
    daily = [{"date": d, "units": raw[d]["units"]} for d in sorted_dates]

    by_cc: dict[str, int] = {}
    for d in sorted_dates:
        for cc, u in raw[d].get("by_cc", {}).items():
            by_cc[cc] = by_cc.get(cc, 0) + u

    top_countries = sorted([{"code": cc, "units": u} for cc, u in by_cc.items()],
                           key=lambda x: x["units"], reverse=True)[:10]

    total_30d = sum(r["units"] for r in daily)
    total_7d  = sum(r["units"] for r in daily[-7:])
    prev_7d   = sum(r["units"] for r in daily[-14:-7]) if len(daily) >= 14 else 0
    chg_7d    = round((total_7d - prev_7d) / prev_7d * 100, 1) if prev_7d else None
    yesterday = daily[-1] if daily else {"date": None, "units": 0}

    return {
        "yesterday":  yesterday,
        "last7d":     {"units": total_7d, "change_pct": chg_7d},
        "last30d":    {"units": total_30d},
        "daily":      daily,
        "by_country": top_countries,
        "sparkline":  [r["units"] for r in daily[-7:]],
    }


def refresh() -> None:
    ts = dt.datetime.utcnow().isoformat()
    try:
        print(f"[{ts}] Refreshing...")
        reviews = _fetch_reviews()
        sales   = _fetch_sales_history(30)
        _cache["data"]      = {"reviews": reviews, "sales": sales}
        _cache["updatedAt"] = dt.datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")
        _cache["error"]     = None
        print(f"[{ts}] Done - yesterday: {sales['yesterday']['units']} dls")
    except Exception as exc:
        _cache["error"] = str(exc)
        print(f"[{ts}] Error: {exc}")


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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coinmania — Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b0b0f;--card:#13131a;--card2:#16161f;--border:#1f1f2e;
  --text:#f1f1f5;--muted:#6b6b80;
  --cyan:#06b6d4;--purple:#a78bfa;--gold:#f59e0b;--green:#22c55e;--red:#ef4444;
}
html,body{height:100%;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:15px;-webkit-font-smoothing:antialiased;overflow:hidden}
body{display:grid;grid-template-rows:auto auto 1fr 1fr;gap:14px;
     padding:18px 26px;height:100vh}

header{display:flex;align-items:center;justify-content:space-between}
.logo{font-size:1.3rem;font-weight:800;letter-spacing:-.03em}
.logo em{color:var(--gold);font-style:normal}
.logo-sub{color:var(--muted);font-size:.75rem;font-weight:400;margin-left:10px}
.hdr-right{display:flex;align-items:center;gap:16px;font-size:.75rem;color:var(--muted)}
.pill{background:var(--card);border:1px solid var(--border);border-radius:8px;
      padding:5px 12px;color:var(--text);font-size:.75rem}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);
     box-shadow:0 0 6px var(--green);display:inline-block;margin-right:5px}

.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:14px;
     padding:16px 18px 12px;position:relative;overflow:hidden}
.kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.kpi.c1::after{background:linear-gradient(90deg,var(--cyan),#0ea5e9)}
.kpi.c2::after{background:linear-gradient(90deg,var(--purple),#7c3aed)}
.kpi.c3::after{background:linear-gradient(90deg,var(--gold),#d97706)}
.kpi.c4::after{background:linear-gradient(90deg,var(--green),#16a34a)}
.kpi-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.kpi-lbl{font-size:.68rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}
.kpi-ico{width:28px;height:28px;border-radius:7px;display:flex;align-items:center;
         justify-content:center;font-size:.85rem}
.c1 .kpi-ico{background:rgba(6,182,212,.15);color:var(--cyan)}
.c2 .kpi-ico{background:rgba(167,139,250,.15);color:var(--purple)}
.c3 .kpi-ico{background:rgba(245,158,11,.15);color:var(--gold)}
.c4 .kpi-ico{background:rgba(34,197,94,.15);color:var(--green)}
.kpi-val{font-size:2.2rem;font-weight:800;letter-spacing:-.04em;line-height:1;margin-bottom:5px}
.kpi-foot{display:flex;align-items:center;justify-content:space-between}
.badge{font-size:.68rem;font-weight:600;padding:2px 8px;border-radius:20px}
.badge.pos{background:rgba(34,197,94,.15);color:var(--green)}
.badge.neg{background:rgba(239,68,68,.15);color:var(--red)}
.badge.neu{background:rgba(107,107,128,.15);color:var(--muted)}
.kpi-sub{font-size:.68rem;color:var(--muted)}
.sp-wrap{height:32px;margin-top:7px;overflow:hidden}
.sp-wrap svg{width:100%;height:32px;overflow:visible}
.minibars{display:flex;align-items:flex-end;gap:2px;height:32px;margin-top:7px}
.minibar{flex:1;border-radius:3px 3px 0 0;transition:height .6s ease;min-height:3px}

.charts-row{display:grid;grid-template-columns:2fr 1fr;gap:12px;min-height:0}
.bot-row{display:grid;grid-template-columns:1fr 2.4fr;gap:12px;min-height:0}

.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
      padding:16px 18px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.card-hdr{display:flex;justify-content:space-between;align-items:flex-start;
          margin-bottom:12px;flex-shrink:0}
.card-title{font-size:.82rem;font-weight:600}
.card-sub{font-size:.68rem;color:var(--muted);margin-top:2px}
.card-badge{font-size:.68rem;color:var(--muted);background:var(--card2);
            border:1px solid var(--border);border-radius:6px;padding:3px 9px;flex-shrink:0}
.chart-wrap{flex:1;min-height:0;position:relative}
.chart-wrap canvas{width:100%!important;height:100%!important}

.clist{flex:1;display:flex;flex-direction:column;gap:7px;overflow:hidden}
.crow{display:flex;align-items:center;gap:8px;font-size:.75rem}
.cflag{font-size:.95rem;width:18px;flex-shrink:0;text-align:center}
.cname{width:88px;color:var(--text);flex-shrink:0;white-space:nowrap;
       overflow:hidden;text-overflow:ellipsis;font-size:.72rem}
.cbar-wrap{flex:1;height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.cbar{height:100%;border-radius:3px;transition:width .8s cubic-bezier(.4,0,.2,1)}
.cval{width:34px;text-align:right;color:var(--muted);font-size:.68rem;flex-shrink:0}

.donut-area{display:flex;align-items:center;justify-content:center;
            position:relative;flex-shrink:0;height:130px}
.donut-area canvas{max-width:130px!important;max-height:130px!important}
.donut-mid{position:absolute;text-align:center;pointer-events:none}
.donut-num{font-size:1.7rem;font-weight:800;color:var(--gold);line-height:1}
.donut-lbl{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.dist-list{display:flex;flex-direction:column;gap:5px;margin-top:10px;flex-shrink:0}
.drow{display:flex;align-items:center;gap:7px;font-size:.68rem;color:var(--muted)}
.dlbl{width:16px;text-align:right}
.dtrack{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.dfill{height:100%;border-radius:2px;background:var(--gold);transition:width .7s ease}
.dcnt{width:16px}

.rv-grid{flex:1;display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
         overflow:hidden;align-content:start}
.rv{background:var(--card2);border:1px solid var(--border);border-radius:11px;padding:12px 14px}
.rv-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:7px}
.rv-stars{color:var(--gold);font-size:.8rem;letter-spacing:1px}
.rv-meta{font-size:.63rem;color:var(--muted);text-align:right;line-height:1.5}
.rv-title{font-size:.77rem;font-weight:600;color:#d4d4d8;margin-bottom:3px}
.rv-body{font-size:.72rem;color:var(--muted);line-height:1.4;
         display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}

#err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);
     border-radius:10px;padding:8px 14px;color:#fca5a5;font-size:.75rem;
     position:fixed;top:16px;right:26px;max-width:400px;z-index:99}
.placeholder{color:var(--muted);font-size:.8rem}
</style>
</head>
<body>

<header>
  <div>
    <span class="logo">Coin<em>mania</em><span class="logo-sub">iOS Analytics</span></span>
  </div>
  <div class="hdr-right">
    <span class="pill">&#128197; Last 30 Days</span>
    <span><span class="dot"></span><span id="updated">Loading&#8230;</span></span>
  </div>
</header>

<div class="kpis">
  <div class="kpi c1">
    <div class="kpi-top"><span class="kpi-lbl">Downloads &middot; Yesterday</span><span class="kpi-ico">&#8595;</span></div>
    <div class="kpi-val" id="kv0">&#8212;</div>
    <div class="kpi-foot"><span class="badge neu" id="kb0">&#8212;</span><span class="kpi-sub" id="ks0"></span></div>
    <div class="sp-wrap"><svg id="sp0" viewBox="0 0 100 32" preserveAspectRatio="none"></svg></div>
  </div>
  <div class="kpi c2">
    <div class="kpi-top"><span class="kpi-lbl">Downloads &middot; 30 Days</span><span class="kpi-ico">&#128200;</span></div>
    <div class="kpi-val" id="kv1">&#8212;</div>
    <div class="kpi-foot"><span class="badge neu" id="kb1">&#8212;</span><span class="kpi-sub" id="ks1"></span></div>
    <div class="sp-wrap"><svg id="sp1" viewBox="0 0 100 32" preserveAspectRatio="none"></svg></div>
  </div>
  <div class="kpi c3">
    <div class="kpi-top"><span class="kpi-lbl">Avg Rating</span><span class="kpi-ico">&#9733;</span></div>
    <div class="kpi-val" id="kv2">&#8212;</div>
    <div class="kpi-foot"><span class="badge pos" id="kb2">App Store</span><span class="kpi-sub" id="ks2"></span></div>
    <div class="minibars" id="mb2"></div>
  </div>
  <div class="kpi c4">
    <div class="kpi-top"><span class="kpi-lbl">Total Reviews</span><span class="kpi-ico">&#128172;</span></div>
    <div class="kpi-val" id="kv3">&#8212;</div>
    <div class="kpi-foot"><span class="badge neu" id="kb3">&#8212;</span><span class="kpi-sub">most recent 50</span></div>
    <div class="minibars" id="mb3"></div>
  </div>
</div>

<div class="charts-row">
  <div class="card">
    <div class="card-hdr">
      <div><div class="card-title">Downloads Over Time</div><div class="card-sub">Daily &middot; last 30 days</div></div>
      <span class="card-badge" id="badge-total">&#8212;</span>
    </div>
    <div class="chart-wrap"><canvas id="ch-line"></canvas></div>
  </div>
  <div class="card">
    <div class="card-hdr">
      <div><div class="card-title">Top Countries</div><div class="card-sub">Downloads by territory</div></div>
    </div>
    <div class="clist" id="clist"><div class="placeholder">Loading&#8230;</div></div>
  </div>
</div>

<div class="bot-row">
  <div class="card">
    <div class="card-hdr"><div><div class="card-title">Rating Distribution</div><div class="card-sub">App Store</div></div></div>
    <div class="donut-area">
      <canvas id="ch-donut"></canvas>
      <div class="donut-mid"><div class="donut-num" id="donut-avg">&#8212;</div><div class="donut-lbl">avg rating</div></div>
    </div>
    <div class="dist-list" id="dist-list"></div>
  </div>
  <div class="card">
    <div class="card-hdr">
      <div><div class="card-title">Recent Reviews</div><div class="card-sub">Latest feedback &middot; App Store</div></div>
      <span class="card-badge" id="badge-rv">&#8212;</span>
    </div>
    <div class="rv-grid" id="rv-grid"><div class="placeholder">Loading&#8230;</div></div>
  </div>
</div>

<div id="err" style="display:none"></div>

<script>
Chart.defaults.color='#6b6b80';
Chart.defaults.font.family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
Chart.defaults.font.size=11;

const CC={US:"United States",GB:"United Kingdom",GE:"Georgia",DE:"Germany",FR:"France",
  RU:"Russia",TR:"Turkey",UA:"Ukraine",KZ:"Kazakhstan",AZ:"Azerbaijan",AM:"Armenia",
  SA:"Saudi Arabia",AE:"UAE",IN:"India",JP:"Japan",CN:"China",KR:"S. Korea",
  CA:"Canada",AU:"Australia",BR:"Brazil",MX:"Mexico",ES:"Spain",IT:"Italy",
  PL:"Poland",NL:"Netherlands",SE:"Sweden",CH:"Switzerland",AT:"Austria",
  IL:"Israel",EG:"Egypt",NG:"Nigeria",ZA:"S. Africa",MA:"Morocco",
  PK:"Pakistan",ID:"Indonesia",TH:"Thailand",VN:"Vietnam",PH:"Philippines"};
const FL={US:"&#127482;&#127480;",GB:"&#127468;&#127463;",GE:"&#127468;&#127466;",DE:"&#127465;&#127466;",
  FR:"&#127467;&#127479;",RU:"&#127479;&#127482;",TR:"&#127481;&#127479;",UA:"&#127482;&#127462;",
  KZ:"&#127472;&#127487;",AZ:"&#127462;&#127487;",AM:"&#127462;&#127474;",SA:"&#127480;&#127462;",
  AE:"&#127462;&#127466;",IN:"&#127470;&#127475;",JP:"&#127471;&#127477;",CN:"&#127464;&#127475;",
  KR:"&#127472;&#127479;",CA:"&#127464;&#127462;",AU:"&#127462;&#127482;",BR:"&#127463;&#127479;",
  MX:"&#127474;&#127485;",ES:"&#127466;&#127480;",IT:"&#127470;&#127481;",PL:"&#127477;&#127473;",
  NL:"&#127475;&#127473;",SE:"&#127480;&#127466;",CH:"&#127464;&#127469;",AT:"&#127462;&#127481;",
  IL:"&#127470;&#127473;",EG:"&#127466;&#127468;"};
const CLRS=['#06b6d4','#a78bfa','#22c55e','#f59e0b','#ec4899',
            '#3b82f6','#10b981','#f97316','#6366f1','#84cc16'];
const RCOLS=['#22c55e','#84cc16','#f59e0b','#f97316','#ef4444'];

let lineChart=null,donutChart=null;

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function fmt(n){return n==null?'—':Number(n).toLocaleString()}
function mo(d){const[,m,day]=d.split('-');return['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+m]+' '+parseInt(day)}
function stars(n){return'★'.repeat(n||0)+'☆'.repeat(5-(n||0))}

function sparkSVG(vals,color){
  if(!vals||vals.length<2)return'';
  const W=100,H=32,max=Math.max(...vals,1),min=Math.min(...vals,0),range=max-min||1;
  const pts=vals.map((v,i)=>((i/(vals.length-1))*W).toFixed(1)+','+(H-((v-min)/range)*(H-4)-2).toFixed(1));
  const gid='g'+color.slice(1);
  return`<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="${color}" stop-opacity="0.35"/>
    <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
  </linearGradient></defs>
  <polygon points="0,${H} ${pts.join(' ')} ${W},${H}" fill="url(#${gid})"/>
  <polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="2"
    stroke-linejoin="round" stroke-linecap="round"/>`;
}

function buildLine(labels,values){
  const cv=document.getElementById('ch-line');
  const ctx=cv.getContext('2d');
  const gr=ctx.createLinearGradient(0,0,0,cv.offsetHeight||220);
  gr.addColorStop(0,'rgba(6,182,212,0.28)');gr.addColorStop(0.7,'rgba(6,182,212,0.04)');gr.addColorStop(1,'rgba(6,182,212,0)');
  const cfg={type:'line',data:{labels,datasets:[{data:values,borderColor:'#06b6d4',backgroundColor:gr,
    fill:true,tension:0.4,pointRadius:values.length>20?0:3,pointHoverRadius:5,
    pointBackgroundColor:'#06b6d4',borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#1e1e2a',borderColor:'#2a2a3a',
        borderWidth:1,titleColor:'#f1f1f5',bodyColor:'#a1a1aa',padding:10,
        callbacks:{label:c=>' '+c.parsed.y.toLocaleString()+' downloads'}}},
      scales:{x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{maxTicksLimit:8,color:'#6b6b80'}},
              y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'#6b6b80'},beginAtZero:true}}}};
  if(lineChart){lineChart.data.labels=labels;lineChart.data.datasets[0].data=values;lineChart.update('none');}
  else lineChart=new Chart(ctx,cfg);
}

function buildDonut(dist){
  const ctx=document.getElementById('ch-donut').getContext('2d');
  const vals=[5,4,3,2,1].map(s=>dist[s]||0),total=vals.reduce((a,b)=>a+b,0);
  const clrs=['#22c55e','#84cc16','#f59e0b','#f97316','#ef4444'];
  const cfg={type:'doughnut',data:{labels:['5★','4★','3★','2★','1★'],
    datasets:[{data:total>0?vals:[1],backgroundColor:total>0?clrs:['#1f1f2e'],borderWidth:0,hoverOffset:3}]},
    options:{responsive:true,maintainAspectRatio:true,cutout:'72%',
      plugins:{legend:{display:false},tooltip:{enabled:total>0,backgroundColor:'#1e1e2a',
        borderColor:'#2a2a3a',borderWidth:1,callbacks:{label:c=>` ${c.label}: ${c.parsed}`}}}}};
  if(donutChart){donutChart.data.datasets[0].data=total>0?vals:[1];donutChart.update('none');}
  else donutChart=new Chart(ctx,cfg);
}

function render(cache){
  document.getElementById('updated').textContent=cache.updatedAt?'Updated '+cache.updatedAt:'—';
  const errEl=document.getElementById('err');
  if(cache.error){errEl.textContent='⚠ '+cache.error;errEl.style.display='block';}
  else errEl.style.display='none';
  if(!cache.data)return;
  const{reviews:rv,sales:sl}=cache.data;

  document.getElementById('kv0').textContent=fmt(sl.yesterday?.units);
  document.getElementById('ks0').textContent=sl.yesterday?.date||'';
  document.getElementById('sp0').innerHTML=sparkSVG(sl.sparkline,'#06b6d4');

  document.getElementById('kv1').textContent=fmt(sl.last30d?.units);
  const chg=sl.last7d?.change_pct,kb1=document.getElementById('kb1');
  if(chg!=null){kb1.className='badge '+(chg>0?'pos':chg<0?'neg':'neu');
    kb1.textContent=(chg>0?'▲':chg<0?'▼':'')+Math.abs(chg)+'% 7d';}
  document.getElementById('ks1').textContent=fmt(sl.last7d?.units)+' this week';
  document.getElementById('sp1').innerHTML=sparkSVG(sl.sparkline,'#a78bfa');

  document.getElementById('kv2').textContent=rv.average!=null?rv.average+'★':'—';
  document.getElementById('ks2').textContent=rv.count+' ratings';
  document.getElementById('mb2').innerHTML=[5,4,3,2,1].map((s,i)=>{
    const h=Math.max(3,Math.round((rv.distribution?.[s]||0)/(rv.count||1)*28));
    return`<div class="minibar" style="height:${h}px;background:${RCOLS[i]}"></div>`;}).join('');

  document.getElementById('kv3').textContent=fmt(rv.count);
  const kb3=document.getElementById('kb3');kb3.className='badge pos';kb3.textContent=(rv.average||'')+'★ avg';
  document.getElementById('mb3').innerHTML=[5,4,3,2,1].map((s,i)=>{
    const h=Math.max(3,Math.round((rv.distribution?.[s]||0)/(rv.count||1)*28));
    return`<div class="minibar" style="height:${h}px;background:${RCOLS[i]}"></div>`;}).join('');

  if(sl.daily?.length){
    buildLine(sl.daily.map(d=>mo(d.date)),sl.daily.map(d=>d.units));
    document.getElementById('badge-total').textContent=fmt(sl.last30d.units)+' total';}

  const cl=document.getElementById('clist');
  if(sl.by_country?.length){
    const mx=sl.by_country[0].units;
    cl.innerHTML=sl.by_country.map((c,i)=>`<div class="crow">
      <span class="cflag">${FL[c.code]||'🌐'}</span>
      <span class="cname">${esc(CC[c.code]||c.code)}</span>
      <div class="cbar-wrap"><div class="cbar" style="width:${Math.round(c.units/mx*100)}%;background:${CLRS[i%CLRS.length]}"></div></div>
      <span class="cval">${c.units}</span>
    </div>`).join('');}
  else cl.innerHTML='<div class="placeholder">No country data yet</div>';

  buildDonut(rv.distribution||{});
  document.getElementById('donut-avg').textContent=rv.average!=null?rv.average:'—';
  const dist=rv.distribution||{},tot=Object.values(dist).reduce((a,b)=>a+b,0)||1;
  document.getElementById('dist-list').innerHTML=[5,4,3,2,1].map(s=>`<div class="drow">
    <span class="dlbl">${s}★</span>
    <div class="dtrack"><div class="dfill" style="width:${Math.round((dist[s]||0)/tot*100)}%"></div></div>
    <span class="dcnt">${dist[s]||0}</span></div>`).join('');

  document.getElementById('badge-rv').textContent=rv.count+' reviews';
  const rg=document.getElementById('rv-grid');
  if(rv.recent?.length)rg.innerHTML=rv.recent.map(r=>`<div class="rv">
    <div class="rv-top">
      <span class="rv-stars">${stars(r.rating)}</span>
      <span class="rv-meta">${esc(r.territory)}<br>${esc(r.date)}</span>
    </div>
    ${r.title?`<div class="rv-title">${esc(r.title)}</div>`:''}
    <div class="rv-body">${esc(r.body)}</div>
  </div>`).join('');
  else rg.innerHTML='<div class="placeholder">No reviews yet</div>';
}

async function poll(){
  try{render(await(await fetch('/data')).json());}catch(e){console.error(e);}
}
poll();setInterval(poll,5*60*1000);
</script>
</body></html>
"""

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ASC_PRIVATE_KEY:
        print("WARNING: ASC_PRIVATE_KEY not set")
    refresh()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(refresh, "interval", hours=REFRESH_HOURS)
    scheduler.start()
    port = int(os.environ.get("PORT", 8000))
    print(f"Running at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
