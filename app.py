import os
import gzip
import json
import logging
import time
import io
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import base64
import jwt
import requests
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ASC_KEY_ID = os.environ.get("ASC_KEY_ID", "")
ASC_ISSUER_ID = os.environ.get("ASC_ISSUER_ID", "")
ASC_VENDOR_NUMBER = os.environ.get("ASC_VENDOR_NUMBER", "89476434")
ASC_APP_ID = os.environ.get("ASC_APP_ID", "6740985837")
REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "6"))
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "")

ANDROID_PACKAGE = os.environ.get("ANDROID_PACKAGE", "com.coinmania.app")
ANDROID_SA_JSON_BASE64 = os.environ.get("ANDROID_SA_JSON_BASE64", "")
ANDROID_SA_JSON_PATH = os.environ.get("ANDROID_SA_JSON_PATH", "")

def _load_private_key():
    raw = os.environ.get("ASC_PRIVATE_KEY", "").strip()
    if raw:
        return raw
    path = os.environ.get("ASC_PRIVATE_KEY_PATH", "")
    if path:
        return Path(path).read_text().strip()
    return ""

ASC_PRIVATE_KEY = _load_private_key()

DOWNLOAD_TYPES = {"1", "1T", "3", "3T"}

BASE_URL = "https://api.appstoreconnect.apple.com"

TARGET_REPORTS = {
    "App Store Installation and Deletion Standard",
    "App Sessions Standard",
    "App Crashes",
    "Retention Messaging",
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_cache = {"data": None, "updatedAt": None, "error": None}
_cache_lock = threading.Lock()

_analytics = {
    "status": "pending",
    "request_id": None,
    "report_ids": {},
    "data": {"daily": {}, "summary": {}, "retention": {}},
    "fetched_at": None,
}
_analytics_lock = threading.Lock()

_android_state = {
    "reviews": [],
    "avg_rating": None,
    "rating_count": 0,
    "dist": {},
    "error": None,
    "fetched_at": None,
}
_android_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _make_jwt():
    now = int(time.time())
    payload = {
        "iss": ASC_ISSUER_ID,
        "iat": now,
        "exp": now + 19 * 60,
        "aud": "appstoreconnect-v1",
    }
    headers = {"alg": "ES256", "kid": ASC_KEY_ID, "typ": "JWT"}
    token = jwt.encode(payload, ASC_PRIVATE_KEY, algorithm="ES256", headers=headers)
    return token if isinstance(token, str) else token.decode()


def _auth_headers(accept="application/json"):
    return {"Authorization": f"Bearer {_make_jwt()}", "Accept": accept}


def _get(url, params=None, timeout=30, accept="application/json"):
    resp = requests.get(url, headers=_auth_headers(accept), params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def _post(url, body, timeout=30):
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp

# ---------------------------------------------------------------------------
# Daily sales
# ---------------------------------------------------------------------------

def _fetch_one_day(date_str):
    """Fetch one day SALES SUMMARY report, return list of rows."""
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportDate]": date_str,
        "filter[reportSubType]": "SUMMARY",
        "filter[reportType]": "SALES",
        "filter[vendorNumber]": ASC_VENDOR_NUMBER,
        "filter[version]": "1_1",
    }
    url = f"{BASE_URL}/v1/salesReports"
    try:
        resp = _get(url, params=params, timeout=30, accept="application/a-gzip")
        content = resp.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        text = content.decode("utf-8")
        rows = []
        lines = text.strip().splitlines()
        if not lines:
            return []
        headers = lines[0].split("\t")
        for line in lines[1:]:
            parts = line.split("\t")
            row = dict(zip(headers, parts))
            rows.append(row)
        return rows
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (404, 410):
            return []
        log.warning("HTTP error fetching day %s: %s", date_str, e)
        return []
    except Exception as e:
        log.warning("Error fetching day %s: %s", date_str, e)
        return []


def _rows_to_units(rows):
    """Sum units for download product types, filtered to ASC_APP_ID."""
    total = 0
    by_country = {}
    matched = False
    for row in rows:
        apple_id = row.get("Apple Identifier", "") or row.get("Apple ID", "")
        prod_type = row.get("Product Type Identifier", "")
        if apple_id == ASC_APP_ID and prod_type in DOWNLOAD_TYPES:
            matched = True
            try:
                units = int(float(row.get("Units", 0)))
            except (ValueError, TypeError):
                units = 0
            total += units
            country = row.get("Country Code", row.get("Country", ""))
            if country:
                by_country[country] = by_country.get(country, 0) + units
    if not matched:
        # fallback: all rows with download types
        for row in rows:
            prod_type = row.get("Product Type Identifier", "")
            if prod_type in DOWNLOAD_TYPES:
                try:
                    units = int(float(row.get("Units", 0)))
                except (ValueError, TypeError):
                    units = 0
                total += units
                country = row.get("Country Code", row.get("Country", ""))
                if country:
                    by_country[country] = by_country.get(country, 0) + units
    return total, by_country


def _fetch_daily_sales(days=35):
    today = datetime.now(timezone.utc).date()
    date_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days + 1)]

    results = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_date = {executor.submit(_fetch_one_day, d): d for d in date_list}
        for future in as_completed(future_to_date):
            d = future_to_date[future]
            try:
                results[d] = future.result()
            except Exception as e:
                log.warning("Future error for %s: %s", d, e)
                results[d] = []

    daily_units = {}
    daily_countries = {}
    for d, rows in results.items():
        u, bc = _rows_to_units(rows)
        daily_units[d] = u
        daily_countries[d] = bc

    sorted_dates = sorted(daily_units.keys(), reverse=True)

    yesterday = sorted_dates[0] if sorted_dates else None
    yesterday_units = daily_units.get(yesterday, 0) if yesterday else 0

    last7_dates = sorted_dates[:7]
    prev7_dates = sorted_dates[7:14]
    last7_units = sum(daily_units.get(d, 0) for d in last7_dates)
    prev7_units = sum(daily_units.get(d, 0) for d in prev7_dates)
    change7_pct = round((last7_units - prev7_units) / prev7_units * 100, 1) if prev7_units else 0

    last30_dates = sorted_dates[:30]
    prev30_dates = sorted_dates[30:60] if len(sorted_dates) >= 60 else sorted_dates[30:]
    last30_units = sum(daily_units.get(d, 0) for d in last30_dates)
    prev30_units = sum(daily_units.get(d, 0) for d in prev30_dates)
    change30_pct = round((last30_units - prev30_units) / prev30_units * 100, 1) if prev30_units else 0

    # Daily list last 30 sorted ascending
    daily_list = [
        {"date": d, "units": daily_units.get(d, 0)}
        for d in sorted(last30_dates)
    ]

    # Aggregate countries over last 30 days
    agg_countries = {}
    for d in last30_dates:
        for code, cnt in daily_countries.get(d, {}).items():
            agg_countries[code] = agg_countries.get(code, 0) + cnt
    top_countries = sorted(agg_countries.items(), key=lambda x: x[1], reverse=True)[:12]
    by_country = [{"code": c, "units": u} for c, u in top_countries]

    sparkline = [daily_units.get(d, 0) for d in sorted(last7_dates)]

    return {
        "yesterday": {"date": yesterday or "", "units": yesterday_units},
        "last7d": {"units": last7_units, "change_pct": change7_pct},
        "last30d": {"units": last30_units},
        "change_30d_pct": change30_pct,
        "daily": daily_list,
        "by_country": by_country,
        "sparkline": sparkline,
    }

# ---------------------------------------------------------------------------
# Monthly sales
# ---------------------------------------------------------------------------

def _fetch_monthly_sales(months=14):
    today = datetime.now(timezone.utc).date()
    current_month_str = today.strftime("%Y-%m")

    month_list = []
    for i in range(months):
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        month_list.append(f"{year:04d}-{month:02d}")

    results = []
    for month_str in month_list:
        if month_str == current_month_str:
            continue  # skip partial current month
        params = {
            "filter[frequency]": "MONTHLY",
            "filter[reportDate]": month_str,
            "filter[reportSubType]": "SUMMARY",
            "filter[reportType]": "SALES",
            "filter[vendorNumber]": ASC_VENDOR_NUMBER,
            "filter[version]": "1_0",
        }
        url = f"{BASE_URL}/v1/salesReports"
        try:
            resp = _get(url, params=params, timeout=30, accept="application/a-gzip")
            content = resp.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            text = content.decode("utf-8")
            lines = text.strip().splitlines()
            if not lines:
                continue
            headers = lines[0].split("\t")
            total = 0
            for line in lines[1:]:
                parts = line.split("\t")
                row = dict(zip(headers, parts))
                apple_id = row.get("Apple Identifier", "") or row.get("Apple ID", "")
                prod_type = row.get("Product Type Identifier", "")
                if (apple_id == ASC_APP_ID or not apple_id) and prod_type in DOWNLOAD_TYPES:
                    try:
                        total += int(float(row.get("Units", 0)))
                    except (ValueError, TypeError):
                        pass
            results.append({"month": month_str, "units": total})
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                continue
            log.warning("HTTP error for monthly %s: %s", month_str, e)
        except Exception as e:
            log.warning("Error fetching monthly %s: %s", month_str, e)

    results.sort(key=lambda x: x["month"])
    return results

# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

def _fetch_reviews():
    url = f"{BASE_URL}/v1/apps/{ASC_APP_ID}/customerReviews"
    params = {
        "sort": "-createdDate",
        "limit": 50,
        "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate,territory",
    }
    try:
        resp = _get(url, params=params, timeout=30)
        data = resp.json()
        reviews_raw = data.get("data", [])
    except Exception as e:
        log.warning("Error fetching reviews: %s", e)
        return {"average": None, "count": 0, "distribution": {}, "recent": []}

    ratings = []
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    recent = []

    for item in reviews_raw:
        attrs = item.get("attributes", {})
        rating = attrs.get("rating")
        if rating is not None:
            try:
                r = int(rating)
                ratings.append(r)
                if 1 <= r <= 5:
                    dist[r] += 1
            except (ValueError, TypeError):
                pass
        recent.append({
            "rating": attrs.get("rating"),
            "title": attrs.get("title", ""),
            "body": attrs.get("body", ""),
            "author": attrs.get("reviewerNickname", ""),
            "date": (attrs.get("createdDate", "") or "")[:10],
            "territory": attrs.get("territory", ""),
        })

    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "average": avg,
        "count": len(ratings),
        "distribution": dist,
        "recent": recent[:12],
    }

# ---------------------------------------------------------------------------
# Analytics API
# ---------------------------------------------------------------------------

def _ensure_analytics_request():
    body = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {"accessType": "ONE_TIME_SNAPSHOT"},
            "relationships": {
                "app": {
                    "data": {"type": "apps", "id": ASC_APP_ID}
                }
            },
        }
    }
    try:
        resp = _post(f"{BASE_URL}/v1/analyticsReportRequests", body, timeout=30)
        data = resp.json()
        req_id = data["data"]["id"]
        with _analytics_lock:
            _analytics["request_id"] = req_id
        log.info("Analytics request created: %s", req_id)
    except Exception as e:
        log.warning("Error creating analytics request: %s", e)


def _parse_tsv_bytes(raw_bytes):
    """Decompress if gzip and parse TSV, return (headers, list_of_dicts)."""
    try:
        if raw_bytes[:2] == b"\x1f\x8b":
            raw_bytes = gzip.decompress(raw_bytes)
        text = raw_bytes.decode("utf-8")
        lines = text.strip().splitlines()
        if not lines:
            return [], []
        headers = lines[0].split("\t")
        rows = []
        for line in lines[1:]:
            parts = line.split("\t")
            rows.append(dict(zip(headers, parts)))
        return headers, rows
    except Exception as e:
        log.warning("TSV parse error: %s", e)
        return [], []


def _col(row, *candidates):
    """Return value for first matching column name (case-insensitive partial)."""
    for key in row:
        kl = key.lower()
        for c in candidates:
            if c.lower() in kl:
                return row[key]
    return None


def _safe_int(val):
    if val is None:
        return 0
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _poll_analytics():
    with _analytics_lock:
        req_id = _analytics["request_id"]
        report_ids = dict(_analytics["report_ids"])

    if not req_id:
        _ensure_analytics_request()
        return

    if not report_ids:
        try:
            url = f"{BASE_URL}/v1/analyticsReportRequests/{req_id}/reports"
            resp = _get(url, params={"limit": 200}, timeout=30)
            data = resp.json()
            new_ids = {}
            for item in data.get("data", []):
                name = item.get("attributes", {}).get("name", "")
                rid = item.get("id", "")
                if name in TARGET_REPORTS and rid:
                    new_ids[name] = rid
            if new_ids:
                with _analytics_lock:
                    _analytics["report_ids"] = new_ids
                log.info("Analytics report IDs found: %s", list(new_ids.keys()))
            else:
                log.info("Analytics reports not ready yet")
            return
        except Exception as e:
            log.warning("Error polling analytics reports list: %s", e)
            return

    # Fetch each report
    daily_data = {}  # date -> dict of metrics

    def _fetch_report_instances(name, rid):
        rows_out = []
        try:
            url = f"{BASE_URL}/v1/analyticsReports/{rid}/instances"
            resp = _get(url, params={"limit": 200}, timeout=30)
            data = resp.json()
            instances = data.get("data", [])
            if not instances:
                return rows_out
            for inst in instances:
                inst_id = inst.get("id", "")
                if not inst_id:
                    continue
                seg_url = f"{BASE_URL}/v1/analyticsReportInstances/{inst_id}/segments"
                try:
                    seg_resp = _get(seg_url, params={"limit": 200}, timeout=30)
                    seg_data = seg_resp.json()
                    for seg in seg_data.get("data", []):
                        seg_dl_url = seg.get("attributes", {}).get("url", "")
                        if not seg_dl_url:
                            continue
                        try:
                            dl_resp = requests.get(seg_dl_url, timeout=60)
                            dl_resp.raise_for_status()
                            _headers, rows = _parse_tsv_bytes(dl_resp.content)
                            rows_out.extend(rows)
                        except Exception as e:
                            log.warning("Error downloading segment for %s: %s", name, e)
                except Exception as e:
                    log.warning("Error fetching segments for instance %s: %s", inst_id, e)
        except Exception as e:
            log.warning("Error fetching instances for %s: %s", name, e)
        return rows_out

    report_rows = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_report_instances, name, rid): name
                   for name, rid in report_ids.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                report_rows[name] = future.result()
            except Exception as e:
                log.warning("Error in analytics future for %s: %s", name, e)
                report_rows[name] = []

    any_data = False

    # Parse Installation and Deletion
    install_rows = report_rows.get("App Store Installation and Deletion Standard", [])
    for row in install_rows:
        date = _col(row, "date")
        if not date:
            continue
        date = str(date)[:10]
        installs = _safe_int(_col(row, "installation", "installs"))
        deletions = _safe_int(_col(row, "deletion", "deletions", "uninstall"))
        if date not in daily_data:
            daily_data[date] = {}
        daily_data[date]["installs"] = daily_data[date].get("installs", 0) + installs
        daily_data[date]["deletions"] = daily_data[date].get("deletions", 0) + deletions
        if installs or deletions:
            any_data = True

    # Parse App Sessions
    session_rows = report_rows.get("App Sessions Standard", [])
    for row in session_rows:
        date = _col(row, "date")
        if not date:
            continue
        date = str(date)[:10]
        sessions = _safe_int(_col(row, "session"))
        active = _safe_int(_col(row, "active device", "active_device"))
        if date not in daily_data:
            daily_data[date] = {}
        daily_data[date]["sessions"] = daily_data[date].get("sessions", 0) + sessions
        daily_data[date]["active_d1"] = daily_data[date].get("active_d1", 0) + active
        if sessions:
            any_data = True

    # Parse Crashes
    crash_rows = report_rows.get("App Crashes", [])
    for row in crash_rows:
        date = _col(row, "date")
        if not date:
            continue
        date = str(date)[:10]
        crashes = _safe_int(_col(row, "crash"))
        if date not in daily_data:
            daily_data[date] = {}
        daily_data[date]["crashes"] = daily_data[date].get("crashes", 0) + crashes
        if crashes:
            any_data = True

    # Parse Retention
    retention_data = {}
    retention_rows = report_rows.get("Retention Messaging", [])
    for row in retention_rows:
        date = _col(row, "date")
        if not date:
            continue
        date = str(date)[:10]
        d1 = None
        d7 = None
        d30 = None
        for key in row:
            kl = key.lower()
            val = row[key]
            try:
                fval = float(str(val).replace("%", "")) if val else None
            except (ValueError, TypeError):
                fval = None
            if "day 1" in kl or kl == "d1" or kl.endswith("_d1") or "day1" in kl:
                d1 = fval
            elif "day 7" in kl or kl == "d7" or kl.endswith("_d7") or "day7" in kl:
                d7 = fval
            elif "day 30" in kl or kl == "d30" or kl.endswith("_d30") or "day30" in kl:
                d30 = fval
        if d1 is not None or d7 is not None or d30 is not None:
            retention_data[date] = {"d1": d1, "d7": d7, "d30": d30}
            any_data = True

    # Compute summary over last 30 days
    today = datetime.now(timezone.utc).date()
    last30 = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 31)]

    deletions_30d = sum(daily_data.get(d, {}).get("deletions", 0) for d in last30)
    crashes_30d = sum(daily_data.get(d, {}).get("crashes", 0) for d in last30)
    sessions_30d = sum(daily_data.get(d, {}).get("sessions", 0) for d in last30)

    # Most recent active_d1
    active_d1 = None
    for d in sorted(daily_data.keys(), reverse=True):
        v = daily_data[d].get("active_d1")
        if v is not None and v > 0:
            active_d1 = v
            break

    # Most recent active_d28
    active_d28 = None
    for d in sorted(daily_data.keys(), reverse=True):
        v = daily_data[d].get("active_d28")
        if v is not None and v > 0:
            active_d28 = v
            break

    summary = {
        "deletions_30d": deletions_30d,
        "crashes_30d": crashes_30d,
        "sessions_30d": sessions_30d,
        "active_d1": active_d1,
        "active_d28": active_d28,
    }

    if any_data:
        with _analytics_lock:
            _analytics["status"] = "ready"
            _analytics["data"]["daily"] = daily_data
            _analytics["data"]["summary"] = summary
            _analytics["data"]["retention"] = retention_data
            _analytics["fetched_at"] = datetime.now(timezone.utc).isoformat()
        log.info("Analytics data stored. summary=%s", summary)
    else:
        log.info("Analytics polled but no data instances ready yet")

# ---------------------------------------------------------------------------
# Android / Google Play
# ---------------------------------------------------------------------------

def _get_android_sa_info():
    if ANDROID_SA_JSON_BASE64:
        try:
            return json.loads(base64.b64decode(ANDROID_SA_JSON_BASE64).decode())
        except Exception as e:
            log.warning("Android SA base64 error: %s", e)
    if ANDROID_SA_JSON_PATH:
        try:
            with open(ANDROID_SA_JSON_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Android SA path error: %s", e)
    return None


def _fetch_android_data():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as _gapi_build

    sa_info = _get_android_sa_info()
    if not sa_info:
        log.info("Android SA not configured, skipping")
        return

    error = None
    reviews = []
    avg_rating = None
    rating_count = 0
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    try:
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        svc = _gapi_build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
        result = svc.reviews().list(
            packageName=ANDROID_PACKAGE, maxResults=50
        ).execute()

        ratings = []
        for item in result.get("reviews", []):
            for comment in item.get("comments", []):
                uc = comment.get("userComment")
                if not uc:
                    continue
                rating = uc.get("starRating", 0)
                if rating:
                    ratings.append(rating)
                    r = int(rating)
                    if 1 <= r <= 5:
                        dist[r] += 1
                last_mod = uc.get("lastModified", {})
                secs = last_mod.get("seconds")
                date_str = ""
                if secs:
                    try:
                        dt = datetime.fromtimestamp(int(secs), tz=timezone.utc)
                        date_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
                reviews.append({
                    "rating": rating,
                    "title": "",
                    "body": uc.get("text", ""),
                    "author": item.get("authorName", ""),
                    "date": date_str,
                    "territory": "Android",
                    "platform": "android",
                })

        avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
        rating_count = len(ratings)
        log.info("Android reviews: %d, avg=%.2f", rating_count, avg_rating or 0)
    except Exception as e:
        log.warning("Android reviews error: %s", e)
        error = str(e)

    with _android_lock:
        _android_state["reviews"] = reviews[:12]
        _android_state["avg_rating"] = avg_rating
        _android_state["rating_count"] = rating_count
        _android_state["dist"] = dist
        _android_state["error"] = error
        _android_state["fetched_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh():
    t0 = time.time()
    log.info("Refresh started")
    error = None
    result = {}
    try:
        reviews = _fetch_reviews()
        result["reviews"] = reviews
    except Exception as e:
        log.error("Reviews fetch error: %s", e)
        result["reviews"] = {"average": None, "count": 0, "distribution": {}, "recent": []}
        error = str(e)

    try:
        sales = _fetch_daily_sales(days=65)
        result["sales"] = sales
    except Exception as e:
        log.error("Daily sales fetch error: %s", e)
        result["sales"] = {}
        error = str(e)

    try:
        monthly = _fetch_monthly_sales(months=14)
        result["monthly"] = monthly
    except Exception as e:
        log.error("Monthly sales fetch error: %s", e)
        result["monthly"] = []
        error = str(e)

    with _cache_lock:
        _cache["data"] = result
        _cache["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _cache["error"] = error

    try:
        _fetch_android_data()
    except Exception as e:
        log.warning("Android fetch error: %s", e)

    log.info("Refresh done in %.1fs", time.time() - t0)

    try:
        _poll_analytics()
    except Exception as e:
        log.warning("Analytics poll error: %s", e)


def analytics_check():
    try:
        _poll_analytics()
    except Exception as e:
        log.warning("Analytics check error: %s", e)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/data")
def data_route():
    with _cache_lock:
        cache_snap = dict(_cache)
    with _analytics_lock:
        analytics_snap = {
            "status": _analytics["status"],
            "request_id": _analytics["request_id"],
            "data": _analytics["data"],
            "fetched_at": _analytics["fetched_at"],
        }
    with _android_lock:
        android_snap = dict(_android_state)
    return jsonify({**cache_snap, "analytics": analytics_snap, "android": android_snap})


@app.route("/refresh", methods=["POST"])
def refresh_route():
    secret = request.args.get("secret", "")
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        return jsonify({"error": "forbidden"}), 403
    threading.Thread(target=refresh, daemon=True).start()
    return jsonify({"ok": True, "message": "Refresh started"})


@app.route("/")
def index():
    return DASHBOARD_HTML

# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coinmania &middot; Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#09090e; --card:#10101a; --card2:#14141f; --border:#1e1e2d;
    --text:#f0f0f5; --muted:#6b6b7e;
    --cyan:#06b6d4; --purple:#a78bfa; --gold:#f59e0b; --green:#22c55e;
    --red:#ef4444; --orange:#f97316;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    height: 100%;
    overflow: hidden;
  }
  body {
    display: grid;
    grid-template-rows: 46px 110px 1fr 1fr;
    gap: 12px;
    padding: 16px 22px;
    height: 100vh;
    overflow: hidden;
  }

  /* ── Row 1 Header ─────────────────────────────────────────────────────── */
  .header {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .logo {
    font-size: 1.15rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: var(--text);
  }
  .logo span { color: var(--gold); }
  .date-pill {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 3px 12px;
    font-size: 0.72rem;
    color: var(--muted);
  }
  .spacer { flex: 1; }
  .updated { font-size: 0.68rem; color: var(--muted); }
  .live-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pulse 2s infinite;
    flex-shrink: 0;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  /* ── Row 2 KPI Cards ──────────────────────────────────────────────────── */
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 10px;
  }
  .kpi {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px 8px;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .kpi::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent, var(--cyan));
  }
  .kpi-icon {
    position: absolute;
    top: 8px; right: 10px;
    width: 28px; height: 28px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent, var(--cyan)) 15%, transparent);
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85rem;
  }
  .kpi-label {
    font-size: 0.62rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .kpi-value {
    font-size: 1.45rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
    color: var(--text);
  }
  .kpi-sub { font-size: 0.65rem; color: var(--muted); }
  .kpi-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 0.62rem;
    padding: 2px 7px;
    border-radius: 10px;
    font-weight: 600;
    margin-top: auto;
    width: fit-content;
  }
  .badge-up   { background: color-mix(in srgb, var(--green)  18%, transparent); color: var(--green);  }
  .badge-down { background: color-mix(in srgb, var(--red)    18%, transparent); color: var(--red);    }
  .badge-neutral { background: color-mix(in srgb, var(--muted) 18%, transparent); color: var(--muted); }
  .badge-purple  { background: color-mix(in srgb, var(--purple) 15%, transparent); color: var(--purple); }
  .star-bars { display: flex; flex-direction: column; gap: 1px; margin-top: 2px; }
  .star-bar-row {
    display: flex; align-items: center; gap: 3px;
    font-size: 0.55rem; color: var(--muted);
  }
  .star-bar-track {
    flex: 1; height: 3px;
    background: var(--border); border-radius: 2px; overflow: hidden;
  }
  .star-bar-fill { height: 100%; border-radius: 2px; }
  .sparkline-wrap {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    opacity: 0.35; height: 28px;
  }

  /* ── Shimmer ──────────────────────────────────────────────────────────── */
  .shimmer {
    background: linear-gradient(90deg, var(--border) 25%, var(--card2) 50%, var(--border) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 6px; height: 2rem; width: 80%;
  }
  .shimmer-row {
    background: linear-gradient(90deg, var(--border) 25%, var(--card2) 50%, var(--border) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 6px; height: 1rem; width: 100%; margin: 4px 0;
  }
  @keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }

  /* ── Row 3 Charts ─────────────────────────────────────────────────────── */
  .charts-row {
    display: grid;
    grid-template-columns: 62% 38%;
    gap: 10px;
    min-height: 0;
  }

  /* ── Row 4 Bottom ─────────────────────────────────────────────────────── */
  .bottom-row {
    display: grid;
    grid-template-columns: 22% 22% 56%;
    gap: 10px;
    min-height: 0;
  }

  /* ── Generic card ─────────────────────────────────────────────────────── */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: hidden;
    min-height: 0;
  }
  .card-title {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--text);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    flex-shrink: 0;
  }
  .card-sub { font-size: 0.62rem; color: var(--muted); }
  .chart-wrap { flex: 1; position: relative; min-height: 0; }
  .title-row { display: flex; align-items: center; flex-shrink: 0; }

  /* ── Countries ────────────────────────────────────────────────────────── */
  .country-list {
    display: flex; flex-direction: column;
    gap: 5px; overflow: hidden; flex: 1;
  }
  .country-row {
    display: flex; align-items: center;
    gap: 6px; font-size: 0.68rem;
  }
  .country-flag { font-size: 0.95rem; width: 20px; text-align: center; }
  .country-name { color: var(--muted); width: 52px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .country-bar-track { flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .country-bar-fill { height: 100%; border-radius: 2px; }
  .country-num { width: 32px; text-align: right; font-variant-numeric: tabular-nums; }

  /* ── Health panel ─────────────────────────────────────────────────────── */
  .donut-wrap {
    position: relative;
    width: 90px; height: 90px;
    flex-shrink: 0; margin: 0 auto;
  }
  .donut-center {
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    font-size: 1.1rem; font-weight: 700;
    pointer-events: none;
  }
  .donut-center small { font-size: 0.55rem; color: var(--muted); font-weight: 400; }
  .health-metrics { display: flex; flex-direction: column; gap: 5px; flex: 1; }
  .health-metric-row {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.65rem;
  }
  .health-metric-label { color: var(--muted); }
  .health-metric-value { font-weight: 600; font-variant-numeric: tabular-nums; }
  .dist-bars { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }

  /* ── Reviews grid ─────────────────────────────────────────────────────── */
  .reviews-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    grid-template-rows: repeat(2, 1fr);
    gap: 8px;
    flex: 1; min-height: 0;
  }
  .review-card {
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    display: flex; flex-direction: column;
    gap: 3px; overflow: hidden; min-height: 0;
  }
  .review-top {
    display: flex; justify-content: space-between;
    align-items: flex-start; gap: 4px;
  }
  .review-stars { font-size: 0.65rem; color: var(--gold); flex-shrink: 0; }
  .review-meta { font-size: 0.58rem; color: var(--muted); text-align: right; }
  .platform-ios { font-size: 0.52rem; background: color-mix(in srgb,var(--cyan) 15%,transparent); color:var(--cyan); border-radius:4px; padding:1px 4px; margin-left:3px; }
  .platform-android { font-size: 0.52rem; background: color-mix(in srgb,var(--green) 15%,transparent); color:var(--green); border-radius:4px; padding:1px 4px; margin-left:3px; }
  .review-title {
    font-size: 0.68rem; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .review-body {
    font-size: 0.62rem; color: var(--muted); line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .count-badge {
    display: inline-flex; align-items: center;
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 10px; padding: 1px 8px;
    font-size: 0.62rem; color: var(--muted); margin-left: 6px;
  }
</style>
</head>
<body>

<!-- Row 1: Header -->
<header class="header">
  <div class="logo">&#127860; Coin<span>mania</span></div>
  <div class="date-pill" id="datePill">Loading&hellip;</div>
  <div class="spacer"></div>
  <div class="updated" id="updatedAt">&mdash;</div>
  <div class="live-dot"></div>
</header>

<!-- Row 2: KPI Cards -->
<div class="kpi-row">
  <!-- 1: Since Launch -->
  <div class="kpi" style="--accent:var(--purple)">
    <div class="kpi-icon">&#128640;</div>
    <div class="kpi-label">Since Launch</div>
    <div class="kpi-value" id="k-launch">&mdash;</div>
    <div class="kpi-sub">Total downloads</div>
    <div class="kpi-badge badge-purple" id="k-launch-badge">&mdash; months</div>
  </div>
  <!-- 2: Downloads 30d -->
  <div class="kpi" style="--accent:var(--cyan)">
    <div class="kpi-icon">&#128229;</div>
    <div class="kpi-label">Downloads &middot; 30 Days</div>
    <div class="kpi-value" id="k-30d">&mdash;</div>
    <div class="kpi-sub" id="k-30d-sub"></div>
    <div class="sparkline-wrap" id="k-30d-spark"></div>
    <div class="kpi-badge badge-neutral" id="k-30d-badge">vs prev 30d</div>
  </div>
  <!-- 3: Downloads Yesterday -->
  <div class="kpi" style="--accent:#0ea5e9">
    <div class="kpi-icon">&#8595;</div>
    <div class="kpi-label">Downloads &middot; Yesterday</div>
    <div class="kpi-value" id="k-yday">&mdash;</div>
    <div class="kpi-sub" id="k-yday-sub"></div>
    <div class="sparkline-wrap" id="k-yday-spark"></div>
    <div class="kpi-badge badge-neutral" id="k-7d-badge">7d trend</div>
  </div>
  <!-- 4: Deletions 30d -->
  <div class="kpi" style="--accent:var(--red)">
    <div class="kpi-icon">&#128465;</div>
    <div class="kpi-label">Deletions &middot; 30 Days</div>
    <div id="k-del-wrap"><div class="shimmer"></div></div>
    <div class="kpi-badge badge-neutral" id="k-del-badge" style="margin-top:auto"></div>
  </div>
  <!-- 5: Rating -->
  <div class="kpi" style="--accent:var(--gold)">
    <div class="kpi-icon">&#11088;</div>
    <div class="kpi-label">Rating</div>
    <div class="kpi-value" id="k-rating">&mdash;</div>
    <div class="star-bars" id="k-star-bars"></div>
    <div class="kpi-sub" id="k-rating-sub"></div>
  </div>
  <!-- 6: Active Devices 28d -->
  <div class="kpi" style="--accent:var(--green)">
    <div class="kpi-icon">&#128241;</div>
    <div class="kpi-label">Active Devices &middot; 28d</div>
    <div id="k-active-wrap"><div class="shimmer"></div></div>
    <div class="kpi-sub" id="k-active-sub"></div>
  </div>
</div>

<!-- Row 3: Charts -->
<div class="charts-row">
  <div class="card">
    <div class="title-row">
      <div class="card-title" id="line-title">30-Day Installs</div>
    </div>
    <div class="chart-wrap">
      <canvas id="lineChart"></canvas>
    </div>
  </div>
  <div class="card">
    <div class="title-row">
      <div class="card-title">Monthly Totals</div>
      <span class="card-sub" style="margin-left:8px">Since launch</span>
    </div>
    <div class="chart-wrap">
      <canvas id="barChart"></canvas>
    </div>
  </div>
</div>

<!-- Row 4: Bottom -->
<div class="bottom-row">
  <!-- Countries -->
  <div class="card">
    <div class="card-title">Top Markets</div>
    <div class="country-list" id="countryList"></div>
  </div>
  <!-- App Health -->
  <div class="card">
    <div class="card-title">App Health</div>
    <div class="donut-wrap">
      <canvas id="donutChart"></canvas>
      <div class="donut-center">
        <span id="donut-avg">&mdash;</span>
        <small>avg</small>
      </div>
    </div>
    <div class="dist-bars" id="distBars"></div>
    <div class="health-metrics" id="healthMetrics"></div>
  </div>
  <!-- Reviews -->
  <div class="card">
    <div class="title-row">
      <div class="card-title">Recent Reviews</div>
      <span class="count-badge" id="reviewCount">0</span>
    </div>
    <div class="reviews-grid" id="reviewsGrid"></div>
  </div>

</div>

<script>
// ── Globals ──────────────────────────────────────────────────────────────────
var lineChart = null, barChart = null, donutChart = null;

Chart.defaults.color = '#6b6b7e';
Chart.defaults.borderColor = '#1e1e2d';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";
Chart.defaults.font.size = 10;

// ── Lookup tables ─────────────────────────────────────────────────────────────
var FLAGS = {
  GE:'&#127468;&#127466;', US:'&#127482;&#127480;', GB:'&#127468;&#127463;',
  DE:'&#127465;&#127466;', FR:'&#127467;&#127479;', RU:'&#127479;&#127482;',
  TR:'&#127481;&#127479;', UA:'&#127482;&#127462;', KZ:'&#127472;&#127487;',
  AZ:'&#127462;&#127487;', AM:'&#127462;&#127474;', SA:'&#127480;&#127462;',
  AE:'&#127462;&#127466;', IN:'&#127470;&#127475;', JP:'&#127471;&#127477;',
  CN:'&#127464;&#127475;', KR:'&#127472;&#127479;', CA:'&#127464;&#127462;',
  AU:'&#127462;&#127482;', BR:'&#127463;&#127479;', MX:'&#127474;&#127485;',
  ES:'&#127466;&#127480;', IT:'&#127470;&#127481;', PL:'&#127477;&#127473;',
  NL:'&#127475;&#127473;', SE:'&#127480;&#127466;', IL:'&#127470;&#127473;',
  EG:'&#127466;&#127468;', PK:'&#127477;&#127472;', ID:'&#127470;&#127465;',
  TH:'&#127481;&#127469;', VN:'&#127483;&#127475;', NG:'&#127475;&#127468;',
  ZA:'&#127487;&#127462;', AR:'&#127462;&#127479;', BY:'&#127463;&#127486;',
  MD:'&#127474;&#127465;', UZ:'&#127482;&#127487;'
};
var COUNTRY_NAMES = {
  GE:'Georgia', US:'United States', GB:'United Kingdom', DE:'Germany',
  FR:'France', RU:'Russia', TR:'Turkey', UA:'Ukraine', KZ:'Kazakhstan',
  AZ:'Azerbaijan', AM:'Armenia', SA:'Saudi Arabia', AE:'UAE', IN:'India',
  JP:'Japan', CN:'China', KR:'S. Korea', CA:'Canada', AU:'Australia',
  BR:'Brazil', MX:'Mexico', ES:'Spain', IT:'Italy', PL:'Poland',
  NL:'Netherlands', SE:'Sweden', IL:'Israel', EG:'Egypt', PK:'Pakistan',
  ID:'Indonesia', TH:'Thailand', VN:'Vietnam', NG:'Nigeria', ZA:'S. Africa',
  AR:'Argentina', BY:'Belarus', MD:'Moldova', UZ:'Uzbekistan'
};
var BAR_COLORS = ['#06b6d4','#a78bfa','#f59e0b','#22c55e','#f97316','#ec4899',
                  '#8b5cf6','#14b8a6','#ef4444','#3b82f6','#84cc16','#d946ef'];

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString();
}
function pct(n) {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + Number(n).toFixed(1) + '%';
}
function moLabel(s) {
  if (!s) return '';
  var parts = s.split('-');
  var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return (mo[parseInt(parts[1], 10) - 1] || parts[1]) + " '" + String(parts[0]).slice(2);
}
function dayLabel(s) {
  if (!s) return '';
  var d = new Date(s + 'T00:00:00Z');
  var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return mo[d.getUTCMonth()] + ' ' + d.getUTCDate();
}
function stars(n) {
  n = Math.round(n || 0);
  return '★'.repeat(Math.min(5, Math.max(0, n))) + '☆'.repeat(Math.max(0, 5 - n));
}
function shimmer() {
  return '<div class="shimmer-row"></div>';
}
function sparkSVG(vals, color) {
  if (!vals || !vals.length) return '';
  var w = 120, h = 28;
  var max = Math.max.apply(null, vals.concat([1]));
  var min = Math.min.apply(null, vals.concat([0]));
  var range = max - min || 1;
  var pts = vals.map(function(v, i) {
    var x = (i / Math.max(vals.length - 1, 1)) * w;
    var y = h - ((v - min) / range) * (h - 4) - 2;
    return x + ',' + y;
  });
  var id = 'sg' + Math.random().toString(36).slice(2);
  var areaPath = 'M ' + pts[0] + ' L ' + pts.join(' L ') +
    ' L ' + w + ',' + h + ' L 0,' + h + ' Z';
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg"' +
    ' preserveAspectRatio="none" width="100%" height="100%">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.5"/>' +
    '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>' +
    '</linearGradient></defs>' +
    '<path d="' + areaPath + '" fill="url(#' + id + ')"/>' +
    '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + color +
    '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>' +
    '</svg>';
}

// ── Charts ────────────────────────────────────────────────────────────────────
function buildLineChart(labels, dlData, delData) {
  var ctx = document.getElementById('lineChart').getContext('2d');
  var cyan = '#06b6d4';
  var red = '#ef4444';
  var gradFill = ctx.createLinearGradient(0, 0, 0, 300);
  gradFill.addColorStop(0, 'rgba(6,182,212,0.25)');
  gradFill.addColorStop(1, 'rgba(6,182,212,0)');
  var datasets = [{
    label: 'Downloads',
    data: dlData,
    borderColor: cyan,
    backgroundColor: gradFill,
    fill: true,
    tension: 0.4,
    pointRadius: 0,
    pointHoverRadius: 4,
    borderWidth: 2
  }];
  if (delData && delData.length && delData.some(function(v){ return v > 0; })) {
    datasets.push({
      label: 'Deletions',
      data: delData,
      borderColor: red,
      backgroundColor: 'transparent',
      fill: false,
      tension: 0.4,
      borderDash: [4, 3],
      pointRadius: 0,
      pointHoverRadius: 4,
      borderWidth: 1.5
    });
  }
  var cfg = {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: datasets.length > 1,
          labels: { boxWidth: 12, padding: 8, color: '#6b6b7e' }
        },
        tooltip: {
          backgroundColor: '#14141f',
          borderColor: '#1e1e2d',
          borderWidth: 1,
          titleColor: '#f0f0f5',
          bodyColor: '#a0a0b0'
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { maxTicksLimit: 8, maxRotation: 0 }
        },
        y: {
          beginAtZero: true,
          grid: { color: '#1e1e2d' },
          ticks: { maxTicksLimit: 5 }
        }
      }
    }
  };
  if (lineChart) {
    lineChart.data.labels = labels;
    lineChart.data.datasets = datasets;
    lineChart.options.plugins.legend.display = datasets.length > 1;
    lineChart.update('none');
  } else {
    lineChart = new Chart(ctx, cfg);
  }
}

function buildBarChart(labels, dlData) {
  var ctx = document.getElementById('barChart').getContext('2d');
  var gradBar = ctx.createLinearGradient(0, 0, 0, 300);
  gradBar.addColorStop(0, 'rgba(167,139,250,0.9)');
  gradBar.addColorStop(1, 'rgba(167,139,250,0.4)');
  var cfg = {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        label: 'Downloads',
        data: dlData,
        backgroundColor: gradBar,
        borderRadius: 6,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#14141f',
          borderColor: '#1e1e2d',
          borderWidth: 1,
          titleColor: '#f0f0f5',
          bodyColor: '#a0a0b0',
          callbacks: {
            label: function(ctx) { return ' ' + fmt(ctx.raw); }
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { maxRotation: 0, maxTicksLimit: 8 }
        },
        y: {
          beginAtZero: true,
          grid: { color: '#1e1e2d' },
          ticks: { maxTicksLimit: 5 }
        }
      }
    }
  };
  if (barChart) {
    barChart.data.labels = labels;
    barChart.data.datasets[0].data = dlData;
    barChart.update('none');
  } else {
    barChart = new Chart(ctx, cfg);
  }
}

function buildDonut(dist) {
  var ctx = document.getElementById('donutChart').getContext('2d');
  var colors = ['#22c55e','#84cc16','#f59e0b','#f97316','#ef4444'];
  var labels = ['5★','4★','3★','2★','1★'];
  var vals = [dist[5]||0, dist[4]||0, dist[3]||0, dist[2]||0, dist[1]||0];
  var cfg = {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{
        data: vals,
        backgroundColor: colors,
        borderColor: '#10101a',
        borderWidth: 2,
        hoverOffset: 4
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '72%',
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#14141f',
          borderColor: '#1e1e2d',
          borderWidth: 1,
          titleColor: '#f0f0f5',
          bodyColor: '#a0a0b0'
        }
      }
    }
  };
  if (donutChart) {
    donutChart.data.datasets[0].data = vals;
    donutChart.update('none');
  } else {
    donutChart = new Chart(ctx, cfg);
  }
}

// ── Renderers ─────────────────────────────────────────────────────────────────
function renderKPIs(data, analytics) {
  var sales = (data && data.sales) || {};
  var monthly = (data && data.monthly) || [];
  var reviews = (data && data.reviews) || {};
  var summary = (analytics && analytics.data && analytics.data.summary) || {};
  var aStatus = (analytics && analytics.status) || 'pending';

  // 1. Since Launch
  var monthlyTotal = monthly.reduce(function(s, m){ return s + (m.units || 0); }, 0);
  var todayYM = new Date().toISOString().slice(0,7);
  var mtd = 0;
  if (sales.daily) {
    sales.daily.forEach(function(d) {
      if (d.date && d.date.slice(0,7) === todayYM) mtd += (d.units || 0);
    });
  }
  var launchTotal = monthlyTotal + mtd;
  document.getElementById('k-launch').textContent = fmt(launchTotal);
  document.getElementById('k-launch-badge').textContent = monthly.length + ' months of data';

  // 2. Downloads 30d
  var last30 = (sales.last30d && sales.last30d.units) || 0;
  document.getElementById('k-30d').textContent = fmt(last30);
  var chg30 = sales.change_30d_pct;
  var badge30 = document.getElementById('k-30d-badge');
  if (chg30 != null) {
    badge30.textContent = pct(chg30) + ' vs prev 30d';
    badge30.className = 'kpi-badge ' + (chg30 >= 0 ? 'badge-up' : 'badge-down');
  }
  var spark = (sales.sparkline || []);
  document.getElementById('k-30d-spark').innerHTML = sparkSVG(spark, '#06b6d4');

  // 3. Yesterday
  var ydayUnits = (sales.yesterday && sales.yesterday.units) || 0;
  document.getElementById('k-yday').textContent = fmt(ydayUnits);
  document.getElementById('k-yday-sub').textContent = (sales.yesterday && sales.yesterday.date) || '';
  document.getElementById('k-yday-spark').innerHTML = sparkSVG(spark, '#0ea5e9');
  var last7chg = sales.last7d && sales.last7d.change_pct;
  var b7 = document.getElementById('k-7d-badge');
  if (last7chg != null) {
    b7.textContent = pct(last7chg) + ' 7d';
    b7.className = 'kpi-badge ' + (last7chg >= 0 ? 'badge-up' : 'badge-down');
  }

  // 4. Deletions 30d
  var delWrap = document.getElementById('k-del-wrap');
  var delBadge = document.getElementById('k-del-badge');
  if (aStatus === 'ready' && summary.deletions_30d != null) {
    delWrap.innerHTML = '<div class="kpi-value" style="color:var(--red)">' + fmt(summary.deletions_30d) + '</div>';
    var net = last30 - (summary.deletions_30d || 0);
    delBadge.textContent = 'Net ' + (net >= 0 ? '+' : '') + fmt(net);
    delBadge.className = 'kpi-badge ' + (net >= 0 ? 'badge-up' : 'badge-down');
  } else if (aStatus === 'error') {
    delWrap.innerHTML = '<div class="kpi-value" style="color:var(--muted)">N/A</div>';
  } else {
    delWrap.innerHTML = '<div class="shimmer"></div>';
    delBadge.textContent = '';
  }

  // 5. Rating
  var avg = reviews.average;
  document.getElementById('k-rating').textContent = avg != null ? avg.toFixed(1) + '★' : '—';
  document.getElementById('k-rating-sub').textContent = fmt(reviews.count) + ' reviews';
  var dist = reviews.distribution || {};
  var total = Object.values(dist).reduce(function(s,v){ return s+v; }, 0) || 1;
  var starColors = ['#22c55e','#84cc16','#f59e0b','#f97316','#ef4444'];
  var starHTML = '';
  for (var i = 5; i >= 1; i--) {
    var w = Math.round(((dist[i]||0)/total)*100);
    starHTML += '<div class="star-bar-row"><span>' + i + '</span>' +
      '<div class="star-bar-track"><div class="star-bar-fill" style="width:' + w +
      '%;background:' + starColors[5-i] + '"></div></div></div>';
  }
  document.getElementById('k-star-bars').innerHTML = starHTML;

  // 6. Active devices 28d
  var activeWrap = document.getElementById('k-active-wrap');
  var activeSub = document.getElementById('k-active-sub');
  if (aStatus === 'ready' && summary.active_d28 != null) {
    activeWrap.innerHTML = '<div class="kpi-value" style="color:var(--green)">' + fmt(summary.active_d28) + '</div>';
    if (summary.active_d1 != null) activeSub.textContent = 'DAU: ' + fmt(summary.active_d1);
  } else if (aStatus === 'ready' && summary.sessions_30d != null) {
    activeWrap.innerHTML = '<div class="kpi-value" style="color:var(--green)">' + fmt(summary.sessions_30d) + '</div>';
    activeSub.textContent = 'Sessions 30d';
  } else if (aStatus === 'error') {
    activeWrap.innerHTML = '<div class="kpi-value" style="color:var(--muted)">N/A</div>';
  } else {
    activeWrap.innerHTML = '<div class="shimmer"></div>';
    activeSub.textContent = '';
  }
}

function renderCharts(data, analytics) {
  var sales = (data && data.sales) || {};
  var monthly = (data && data.monthly) || [];
  var aStatus = (analytics && analytics.status) || 'pending';
  var dailyAnalytics = (analytics && analytics.data && analytics.data.daily) || {};

  var dailyArr = (sales.daily || []);
  var labels = dailyArr.map(function(d){ return dayLabel(d.date); });
  var dlVals = dailyArr.map(function(d){ return d.units || 0; });

  var delVals = null;
  if (aStatus === 'ready') {
    var dv = dailyArr.map(function(d){
      var ad = dailyAnalytics[d.date];
      return ad ? (ad.deletions || 0) : 0;
    });
    if (dv.some(function(v){ return v > 0; })) delVals = dv;
    document.getElementById('line-title').textContent = delVals
      ? '30-Day Installs vs Deletions' : '30-Day Installs';
  }

  if (labels.length) buildLineChart(labels, dlVals, delVals);

  if (monthly.length) {
    var mLabels = monthly.map(function(m){ return moLabel(m.month); });
    var mVals = monthly.map(function(m){ return m.units || 0; });
    buildBarChart(mLabels, mVals);
  }
}

function renderCountries(byCountry) {
  var el = document.getElementById('countryList');
  if (!byCountry || !byCountry.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:.7rem">No data</div>';
    return;
  }
  var maxVal = byCountry[0].units || 1;
  var html = '';
  byCountry.slice(0, 12).forEach(function(c, i) {
    var flag = FLAGS[c.code] || '&#127760;';
    var name = COUNTRY_NAMES[c.code] || c.code;
    var w = Math.round((c.units / maxVal) * 100);
    var color = BAR_COLORS[i % BAR_COLORS.length];
    html += '<div class="country-row">' +
      '<span class="country-flag">' + flag + '</span>' +
      '<span class="country-name">' + name + '</span>' +
      '<div class="country-bar-track"><div class="country-bar-fill" style="width:' + w +
      '%;background:' + color + '"></div></div>' +
      '<span class="country-num">' + fmt(c.units) + '</span></div>';
  });
  el.innerHTML = html;
}

function renderHealth(reviews, analytics, android) {
  var dist = (reviews && reviews.distribution) || {};
  var avg = reviews && reviews.average;
  var aStatus = (analytics && analytics.status) || 'pending';
  var summary = (analytics && analytics.data && analytics.data.summary) || {};
  var retention = (analytics && analytics.data && analytics.data.retention) || {};
  var andAvg = android && android.avg_rating;
  var andCount = (android && android.rating_count) || 0;

  // Show combined or iOS avg in donut center, with Android beside it
  var displayAvg = avg != null ? avg.toFixed(1) : '—';
  document.getElementById('donut-avg').textContent = displayAvg;
  if (andAvg != null && andCount > 0) {
    document.getElementById('donut-avg').title = 'iOS: ' + displayAvg + '  Android: ' + andAvg.toFixed(1);
  }
  if (Object.values(dist).some(function(v){ return v > 0; })) buildDonut(dist);

  var total = Object.values(dist).reduce(function(s,v){ return s+v; }, 0) || 1;
  var starColors = ['#22c55e','#84cc16','#f59e0b','#f97316','#ef4444'];
  var dHtml = '';
  for (var i = 5; i >= 1; i--) {
    var w = Math.round(((dist[i]||0)/total)*100);
    dHtml += '<div class="star-bar-row"><span>' + i + '★</span>' +
      '<div class="star-bar-track" style="flex:1"><div class="star-bar-fill" style="width:' + w +
      '%;background:' + starColors[5-i] + '"></div></div>' +
      '<span style="width:28px;text-align:right;font-size:0.58rem;color:var(--muted)">' +
      (dist[i]||0) + '</span></div>';
  }
  document.getElementById('distBars').innerHTML = dHtml;

  var hm = document.getElementById('healthMetrics');
  var hmHtml = '';
  // Always show Android rating if available
  if (andAvg != null && andCount > 0) {
    hmHtml += '<div class="health-metric-row">' +
      '<span class="health-metric-label"><span class="platform-android">Android</span> Rating</span>' +
      '<span class="health-metric-value" style="color:var(--green)">' + andAvg.toFixed(1) + '★ <span style="font-size:0.55rem;color:var(--muted)">(' + andCount + ')</span></span></div>';
  }
  if (aStatus === 'ready') {
    hmHtml += '<div class="health-metric-row">' +
      '<span class="health-metric-label">Sessions 30d</span>' +
      '<span class="health-metric-value">' + fmt(summary.sessions_30d) + '</span></div>';
    var crashFree = (summary.sessions_30d && summary.crashes_30d != null)
      ? (100 - (summary.crashes_30d / summary.sessions_30d * 100)).toFixed(2) + '% CF'
      : '';
    hmHtml += '<div class="health-metric-row">' +
      '<span class="health-metric-label">Crashes 30d</span>' +
      '<span class="health-metric-value" style="color:var(--red)">' + fmt(summary.crashes_30d) +
      (crashFree ? '<span style="font-size:0.55rem;color:var(--muted);margin-left:4px">' + crashFree + '</span>' : '') +
      '</span></div>';
    var retDates = Object.keys(retention).sort().reverse();
    if (retDates.length) {
      var latest = retention[retDates[0]];
      if (latest.d1 != null) {
        hmHtml += '<div class="health-metric-row">' +
          '<span class="health-metric-label">Retention D1</span>' +
          '<span class="health-metric-value" style="color:var(--cyan)">' +
          latest.d1.toFixed(1) + '%</span></div>';
      }
    }
    hm.innerHTML = hmHtml || (shimmer() + shimmer() + shimmer());
  } else {
    hm.innerHTML = hmHtml + shimmer() + shimmer();
  }
}

function renderReviews(reviews, android) {
  var iosRecent = (reviews && reviews.recent) || [];
  var andRecent = (android && android.reviews) || [];
  // Tag iOS reviews
  var iosTagged = iosRecent.map(function(r){ return Object.assign({}, r, {platform: r.platform || 'ios'}); });
  // Merge and sort by date desc, take top 6
  var all = iosTagged.concat(andRecent);
  all.sort(function(a, b){ return (b.date || '').localeCompare(a.date || ''); });
  var totalCount = ((reviews && reviews.count) || 0) + ((android && android.rating_count) || 0);
  document.getElementById('reviewCount').textContent = fmt(totalCount);
  var grid = document.getElementById('reviewsGrid');
  if (!all.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:.7rem;grid-column:1/-1">No reviews</div>';
    return;
  }
  var html = '';
  all.slice(0, 6).forEach(function(r) {
    var rNum = parseInt(r.rating) || 0;
    var starStr = '★'.repeat(rNum) + '☆'.repeat(5 - rNum);
    var platBadge = r.platform === 'android'
      ? '<span class="platform-android">Android</span>'
      : '<span class="platform-ios">iOS</span>';
    var territory = (r.platform === 'android') ? '' : (r.territory || '');
    html += '<div class="review-card">' +
      '<div class="review-top">' +
      '<span class="review-stars">' + starStr + '</span>' +
      '<span class="review-meta">' + (territory || '') +
      (r.date ? '<br>' + r.date : '') + '</span></div>' +
      '<div class="review-title">' + String(r.title || '').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
      platBadge + '</div>' +
      '<div class="review-body">' + String(r.body || '').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>' +
      '</div>';
  });
  grid.innerHTML = html;
}

function render(cache) {
  if (!cache) return;
  var data = cache.data || {};
  var analytics = cache.analytics || { status: 'pending', data: { daily:{}, summary:{}, retention:{} } };
  var android = cache.android || { reviews: [], avg_rating: null, rating_count: 0, dist: {} };
  var now = new Date();
  var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  document.getElementById('datePill').textContent =
    mo[now.getMonth()] + ' ' + now.getDate() + ', ' + now.getFullYear();
  if (cache.updatedAt) {
    var u = new Date(cache.updatedAt);
    document.getElementById('updatedAt').textContent = 'Updated ' + u.toLocaleTimeString();
  }
  renderKPIs(data, analytics);
  renderCharts(data, analytics);
  renderCountries((data.sales && data.sales.by_country) || []);
  renderHealth(data.reviews, analytics, android);
  renderReviews(data.reviews || {}, android);
}

// ── Poll ──────────────────────────────────────────────────────────────────────
function poll() {
  fetch('/data')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) { render(data); })
    .catch(function(e) { console.warn('Poll error:', e); });
}

poll();
setInterval(poll, 5 * 60 * 1000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    refresh()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(refresh, "interval", hours=REFRESH_HOURS)
    scheduler.add_job(analytics_check, "interval", minutes=30)
    scheduler.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
