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

import historical as _hist

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
ANDROID_GCS_BUCKET = os.environ.get("ANDROID_GCS_BUCKET", "pubsite_prod_8621543385680213141")

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
    "crash_rate": None,       # 7-day user-weighted crash rate (fraction, e.g. 0.002)
    "anr_rate": None,         # 7-day user-weighted ANR rate
    "crash_count_30d": None,  # total crash reports last 30d
    "distinct_users": None,   # latest distinct users from vitals
    # GCS installs data
    "active_installs": None,    # latest Active Device Installs
    "total_installs": None,     # Total User Installs (all-time, from latest row)
    "installs_yesterday": None,      # Daily User Installs for yesterday
    "installs_yesterday_date": None, # date string that installs_yesterday refers to
    "daily_installs": None,          # avg Daily User Installs (last 30 days)
    "daily_uninstalls": None,   # avg Daily User Uninstalls (last 30 days)
    "installs_30d": None,       # total Daily User Installs summed over last 30 days
    "installs_prev_30d": None,  # total Daily User Installs summed over days 31-60
    "uninstalls_30d": None,     # total Daily User Uninstalls summed over last 30 days
    "version_data": [],         # [{version, installs, platform}] sorted by installs desc
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
        "prev30d": {"units": prev30_units},
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
    """Paginate through ALL iOS reviews (up to 500)."""
    next_url = f"{BASE_URL}/v1/apps/{ASC_APP_ID}/customerReviews"
    next_params = {
        "sort": "-createdDate",
        "limit": 200,
        "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate,territory",
    }
    all_raw = []
    try:
        while next_url and len(all_raw) < 500:
            resp = _get(next_url, params=next_params, timeout=30)
            data = resp.json()
            all_raw.extend(data.get("data", []))
            next_url = data.get("links", {}).get("next")
            next_params = None  # next_url already contains query params
    except Exception as e:
        log.warning("Error fetching reviews: %s", e)
        if not all_raw:
            return {"average": None, "count": 0, "distribution": {}, "recent": []}

    ratings = []
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    recent = []

    for item in all_raw:
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
    log.info("iOS reviews fetched: %d total", len(recent))
    return {
        "average": avg,
        "count": len(ratings),
        "distribution": dist,
        "recent": recent,  # all reviews, no artificial limit
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
        # Paginate through ALL Android reviews
        all_android_raw = []
        page_token = None
        while len(all_android_raw) < 500:
            kwargs = {"packageName": ANDROID_PACKAGE, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            result = svc.reviews().list(**kwargs).execute()
            all_android_raw.extend(result.get("reviews", []))
            page_token = result.get("tokenPagination", {}).get("nextPageToken")
            if not page_token:
                break
        log.info("Android reviews fetched: %d total", len(all_android_raw))

        ratings = []
        for item in all_android_raw:
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

    # --- Fetch Android vitals via Play Developer Reporting API ---
    crash_rate = None
    anr_rate = None
    crash_count_30d = None
    distinct_users = None
    try:
        import requests as _req
        import google.auth.transport.requests as _gatr

        vcreds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/playdeveloperreporting"],
        )
        vcreds.refresh(_gatr.Request(session=_req.Session()))
        vtoken = vcreds.token

        # freshness: data available up to ~3 days ago
        today = datetime.now(timezone.utc).date()
        end_date = today - timedelta(days=3)
        start_date = today - timedelta(days=33)  # 30 days + 3 day lag

        def _vitals_timeline(start, end):
            return {
                "aggregationPeriod": "DAILY",
                "startTime": {"year": start.year, "month": start.month, "day": start.day},
                "endTime":   {"year": end.year,   "month": end.month,   "day": end.day},
            }

        # Crash rate
        cr = _req.post(
            f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{ANDROID_PACKAGE}/crashRateMetricSet:query",
            headers={"Authorization": f"Bearer {vtoken}", "Content-Type": "application/json"},
            json={"timelineSpec": _vitals_timeline(start_date, end_date),
                  "metrics": ["crashRate7dUserWeighted", "distinctUsers"], "dimensions": []},
            timeout=30
        )
        if cr.status_code == 200:
            rows = cr.json().get("rows", [])
            if rows:
                last = rows[-1]["metrics"]
                for m in last:
                    if m["metric"] == "crashRate7dUserWeighted":
                        crash_rate = float(m["decimalValue"]["value"])
                    if m["metric"] == "distinctUsers":
                        distinct_users = int(float(m["decimalValue"]["value"]))
            log.info("Android crash rate: %s, distinct users: %s", crash_rate, distinct_users)

        # ANR rate
        ar = _req.post(
            f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{ANDROID_PACKAGE}/anrRateMetricSet:query",
            headers={"Authorization": f"Bearer {vtoken}", "Content-Type": "application/json"},
            json={"timelineSpec": _vitals_timeline(start_date, end_date),
                  "metrics": ["anrRate7dUserWeighted"], "dimensions": []},
            timeout=30
        )
        if ar.status_code == 200:
            rows = ar.json().get("rows", [])
            if rows:
                last = rows[-1]["metrics"]
                for m in last:
                    if m["metric"] == "anrRate7dUserWeighted":
                        anr_rate = float(m["decimalValue"]["value"])
            log.info("Android ANR rate: %s", anr_rate)

        # Total crash count last 30 days
        ec = _req.post(
            f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{ANDROID_PACKAGE}/errorCountMetricSet:query",
            headers={"Authorization": f"Bearer {vtoken}", "Content-Type": "application/json"},
            json={"timelineSpec": _vitals_timeline(start_date, end_date),
                  "metrics": ["errorReportCount"], "dimensions": ["reportType"]},
            timeout=30
        )
        if ec.status_code == 200:
            total = 0
            for row in ec.json().get("rows", []):
                dims = {d["dimension"]: d.get("stringValue", "") for d in row.get("dimensions", [])}
                if dims.get("reportType") == "CRASH":
                    for m in row.get("metrics", []):
                        if m["metric"] == "errorReportCount":
                            total += int(float(m["decimalValue"]["value"]))
            crash_count_30d = total
            log.info("Android crash count 30d: %s", crash_count_30d)

    except Exception as e:
        log.warning("Android vitals error: %s", e)

    # --- Fetch installs/uninstalls from GCS bulk reports ---
    active_installs = None
    total_installs = None
    installs_yesterday = None
    installs_yesterday_date = None
    daily_installs = None
    version_data = []
    daily_uninstalls = None
    installs_30d = None
    installs_prev_30d = None
    uninstalls_30d = None
    try:
        from google.oauth2 import service_account as _sa
        from google.cloud import storage as _gcs

        gcs_creds = _sa.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
        )
        gcs_client = _gcs.Client(credentials=gcs_creds, project=sa_info.get("project_id"))
        gcs_bucket_name = ANDROID_GCS_BUCKET

        today = datetime.now(timezone.utc).date()
        # We need current month and optionally previous month to get ~30 days
        months_to_try = []
        months_to_try.append(today.strftime("%Y%m"))  # current month e.g. "202604"
        # also grab last month for earlier rows
        first_of_month = today.replace(day=1)
        prev_month = (first_of_month - timedelta(days=1)).strftime("%Y%m")
        months_to_try.append(prev_month)

        def _read_installs_csv(blob_name):
            """Download and parse a Play Console installs overview CSV. Returns list of dicts."""
            try:
                bucket = gcs_client.bucket(gcs_bucket_name)
                blob = bucket.blob(blob_name)
                raw = blob.download_as_bytes(timeout=20)
                # Files are UTF-16 LE with BOM
                txt = raw.decode("utf-16")
                rows = []
                lines = txt.strip().splitlines()
                if not lines:
                    return []
                hdrs = [h.strip() for h in lines[0].split(",")]
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    parts = line.split(",")
                    row = dict(zip(hdrs, parts))
                    rows.append(row)
                return rows
            except Exception as e:
                log.debug("GCS read %s: %s", blob_name, e)
                return None  # None = access error; [] = empty file

        all_rows = []
        for ym in months_to_try:
            blob_name = f"stats/installs/installs_{ANDROID_PACKAGE}_{ym}_overview.csv"
            rows = _read_installs_csv(blob_name)
            if rows is None:
                log.info("GCS installs access denied or unavailable for %s", ym)
                break  # stop trying; access not yet granted
            all_rows.extend(rows)

        if all_rows:
            # Sort by date descending
            all_rows.sort(key=lambda r: r.get("Date", ""), reverse=True)
            # Date boundaries
            yesterday_str = (today - timedelta(days=1)).isoformat()
            cutoff_30 = (today - timedelta(days=30)).isoformat()
            cutoff_60 = (today - timedelta(days=60)).isoformat()

            recent_30 = [r for r in all_rows if r.get("Date", "") >= cutoff_30]
            recent_prev = [r for r in all_rows if cutoff_60 <= r.get("Date", "") < cutoff_30]
            if not recent_30:
                recent_30 = all_rows[:30]

            # Active Device Installs + Total User Installs from latest row
            latest = all_rows[0]
            try:
                active_installs = int(float(latest.get("Active Device Installs", 0) or 0))
            except (ValueError, TypeError):
                pass
            try:
                total_installs = int(float(latest.get("Total User Installs", 0) or 0))
            except (ValueError, TypeError):
                pass

            # Yesterday's installs
            yest_rows = [r for r in all_rows if r.get("Date", "") == yesterday_str]
            if yest_rows:
                try:
                    installs_yesterday = int(float(yest_rows[0].get("Daily User Installs", 0) or 0))
                    installs_yesterday_date = yesterday_str
                except (ValueError, TypeError):
                    pass
            else:
                # GCS may lag 1-2 days; use the most recent available row
                sorted_gcs = sorted(all_rows, key=lambda r: r.get("Date", ""), reverse=True)
                for r in sorted_gcs:
                    try:
                        v = int(float(r.get("Daily User Installs", 0) or 0))
                        installs_yesterday = v
                        installs_yesterday_date = r.get("Date", "")
                        break
                    except (ValueError, TypeError):
                        continue

            # Last 30 days and previous 30 days
            inst_vals, uninst_vals, prev_vals = [], [], []
            for r in recent_30:
                try:
                    inst_vals.append(int(float(r.get("Daily User Installs", 0) or 0)))
                    uninst_vals.append(int(float(r.get("Daily User Uninstalls", 0) or 0)))
                except (ValueError, TypeError):
                    pass
            for r in recent_prev:
                try:
                    prev_vals.append(int(float(r.get("Daily User Installs", 0) or 0)))
                except (ValueError, TypeError):
                    pass
            if inst_vals:
                installs_30d = sum(inst_vals)
                daily_installs = round(sum(inst_vals) / len(inst_vals), 1)
            if uninst_vals:
                uninstalls_30d = sum(uninst_vals)
                daily_uninstalls = round(sum(uninst_vals) / len(uninst_vals), 1)
            if prev_vals:
                installs_prev_30d = sum(prev_vals)

            log.info("Android installs: active=%s, total=%s, yesterday=%s, 30d=%s, prev30d=%s",
                     active_installs, total_installs, installs_yesterday, installs_30d, installs_prev_30d)

        # --- App version breakdown ---
        ver_rows = _read_installs_csv(
            f"stats/installs/installs_{ANDROID_PACKAGE}_{months_to_try[0]}_app_version.csv"
        )
        if ver_rows:
            ver_agg = {}
            for r in ver_rows:
                ver = r.get("App Version Code") or r.get("App Version Name") or "unknown"
                try:
                    cnt = int(float(r.get("Daily Device Installs", 0) or 0))
                except (ValueError, TypeError):
                    cnt = 0
                ver_agg[ver] = ver_agg.get(ver, 0) + cnt
            version_data = sorted(
                [{"version": v, "installs": c, "platform": "android"}
                 for v, c in ver_agg.items() if c > 0],
                key=lambda x: x["installs"], reverse=True
            )[:8]
        else:
            version_data = []

    except Exception as e:
        log.warning("Android GCS installs error: %s", e)

    # --- Historical CSV fallback: fill any None values from embedded CSV data ---
    try:
        fb = _hist.get_android_fallback()
        # Use `not X` so 0 (from GCS column mismatch) also triggers fallback
        if not active_installs:   active_installs   = fb["active_installs"]
        if not total_installs:    total_installs    = fb["total_installs"]
        if installs_yesterday is None:
            installs_yesterday      = fb["installs_yesterday"]
            installs_yesterday_date = fb.get("installs_yesterday_date")
        if installs_yesterday_date is None:
            installs_yesterday_date = fb.get("installs_yesterday_date")
        if daily_installs    is None: daily_installs    = fb["daily_installs"]
        if daily_uninstalls  is None: daily_uninstalls  = fb["daily_uninstalls"]
        if installs_30d      is None: installs_30d      = fb["installs_30d"]
        if installs_prev_30d is None: installs_prev_30d = fb["installs_prev_30d"]
        if uninstalls_30d    is None: uninstalls_30d    = fb["uninstalls_30d"]
        if not distinct_users:    distinct_users    = fb["distinct_users"]
        log.info("Android historical fallback applied: total=%s active=%s 30d=%s",
                 total_installs, active_installs, installs_30d)
    except Exception as e:
        log.warning("Historical fallback error: %s", e)

    with _android_lock:
        _android_state["reviews"] = reviews  # all paginated reviews
        _android_state["avg_rating"] = avg_rating
        _android_state["rating_count"] = rating_count
        _android_state["dist"] = dist
        _android_state["crash_rate"] = crash_rate
        _android_state["anr_rate"] = anr_rate
        _android_state["crash_count_30d"] = crash_count_30d
        _android_state["distinct_users"] = distinct_users
        _android_state["active_installs"] = active_installs
        _android_state["total_installs"] = total_installs
        _android_state["installs_yesterday"] = installs_yesterday
        _android_state["installs_yesterday_date"] = installs_yesterday_date
        _android_state["daily_installs"] = daily_installs
        _android_state["daily_uninstalls"] = daily_uninstalls
        _android_state["installs_30d"] = installs_30d
        _android_state["installs_prev_30d"] = installs_prev_30d
        _android_state["uninstalls_30d"] = uninstalls_30d
        _android_state["version_data"] = version_data
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
        # Merge with historical CSV: fill in months where API returned 0 or nothing
        hist_monthly = {m["month"]: m["units"] for m in _hist.get_ios_monthly_historical()}
        api_monthly = {m["month"]: m["units"] for m in monthly}
        merged = {}
        for month in set(list(hist_monthly.keys()) + list(api_monthly.keys())):
            api_val = api_monthly.get(month, 0)
            hist_val = hist_monthly.get(month, 0)
            # Prefer API value if non-zero, else use historical
            merged[month] = api_val if api_val > 0 else hist_val
        result["monthly"] = [{"month": m, "units": v} for m, v in sorted(merged.items()) if v > 0]
    except Exception as e:
        log.error("Monthly sales fetch error: %s", e)
        result["monthly"] = [{"month": m["month"], "units": m["units"]}
                             for m in _hist.get_ios_monthly_historical()]
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
    # Android historical monthly downloads for bar chart
    android_snap["monthly_hist"] = _hist.get_android_monthly_historical()
    # Android daily last 30 days for line chart
    _today = datetime.now(timezone.utc).date()
    _cutoff30 = (_today - timedelta(days=30)).isoformat()
    android_snap["daily_30d"] = sorted(
        [{"date": k, "units": v} for k, v in _hist.AND_DOWNLOADS.items() if k >= _cutoff30],
        key=lambda x: x["date"]
    )
    # iOS DAU latest from historical CSV
    android_snap["ios_dau_latest"] = _hist._latest(_hist.IOS_DAU)
    # iOS MAU latest from historical CSV
    android_snap["ios_mau_latest"] = _hist._latest(_hist.IOS_MAU)
    # MAU time series for chart (weekly samples)
    android_snap["mau_hist"] = _hist.get_mau_series()
    # Android day-before-yesterday for Yesterday % change
    _and_dl_sorted = sorted(k for k in _hist.AND_DOWNLOADS if k < _today.isoformat())
    android_snap["and_day_before"] = _hist.AND_DOWNLOADS.get(_and_dl_sorted[-2]) if len(_and_dl_sorted) >= 2 else None
    # Previous-day DAU values for DAU % change
    _ios_dau_keys = sorted(_hist.IOS_DAU.keys())
    _and_dau_keys = sorted(_hist.AND_DAU.keys())
    android_snap["ios_dau_prev"] = _hist.IOS_DAU.get(_ios_dau_keys[-2]) if len(_ios_dau_keys) >= 2 else None
    android_snap["and_dau_prev"] = _hist.AND_DAU.get(_and_dau_keys[-2]) if len(_and_dau_keys) >= 2 else None
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
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
  :root{
    --bg:#06060f;--card:#0d0d1a;--card2:#111122;--border:#1a1a2e;
    --text:#eeeef5;--muted:#5a5a72;
    --ios:#06b6d4;--android:#22c55e;
    --gold:#f59e0b;--red:#ef4444;--purple:#a78bfa;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  html,body{height:97vh;overflow:hidden;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
  /* Rows: 32 hdr + 137 kpi + 1fr charts + 205 bottom + 3×7 gaps + 16 padding ≈ 97vh */
  body{display:grid;grid-template-rows:32px 137px 1fr 205px;gap:7px;padding:8px 14px;height:97vh}

  /* ── Header ─────────────────────────────────────────────────── */
  .hdr{display:flex;align-items:center;gap:10px}
  .logo img{height:26px;width:auto;display:block}
  .pill{background:var(--card);border:1px solid var(--border);border-radius:20px;
    padding:2px 10px;font-size:.68rem;color:var(--muted)}
  .live-badge{display:flex;align-items:center;gap:5px;margin-left:auto;
    font-size:.65rem;color:var(--muted)}
  .live-dot{width:7px;height:7px;border-radius:50%;background:var(--android);
    animation:pulse-dot 2s ease infinite}
  @keyframes pulse-dot{
    0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(34,197,94,.4)}
    50%{opacity:.7;box-shadow:0 0 0 6px rgba(34,197,94,0)}
  }

  /* ── KPI Row ────────────────────────────────────────────────── */
  .kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:8px 12px 8px;display:flex;flex-direction:column;
    position:relative;overflow:hidden}
  .kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:var(--accent,var(--ios));border-radius:10px 10px 0 0;
    box-shadow:0 0 16px var(--accent,var(--ios))}
  .kpi-lbl{font-size:.58rem;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.6px;flex-shrink:0;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .kpi-val{font-size:2rem;font-weight:800;line-height:1.05;letter-spacing:-1.5px;
    margin:4px 0 2px;flex-shrink:0;color:var(--text)}
  .kpi-trend{font-size:.66rem;font-weight:700;min-height:16px;flex-shrink:0;margin-bottom:2px}
  /* Platform breakdown rows */
  .kpi-plat{flex:1;min-height:0;display:flex;flex-direction:column;
    justify-content:flex-end;gap:4px}
  .kpi-prow{display:flex;justify-content:space-between;align-items:center}
  .plt{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
  .plt.ios{color:var(--ios)} .plt.and{color:var(--android)}
  .pnum{font-size:.7rem;font-weight:700;color:var(--text)}
  /* iOS/Android ratio bar */
  .kpi-ratio{height:3px;border-radius:3px;flex-shrink:0;margin-top:7px;
    background:var(--android)}
  .kpi-ratio-ios{height:100%;border-radius:3px;background:var(--ios);
    transition:width .6s ease}
  /* Special KPI: CF sparkline */
  .kpi-split{font-size:.6rem;color:var(--muted);flex-shrink:0;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .kpi-split .ios{color:var(--ios)} .kpi-split .and{color:var(--android)}
  .sep{color:#2e2e46}
  .kpi-spark{flex:1;min-height:0;margin-top:4px;position:relative}
  /* Special KPI: Rating stars */
  .kpi-stars{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
    gap:3px;min-height:0}
  .star-row{display:grid;grid-template-columns:22px 1fr 20px;align-items:center;gap:4px;font-size:.55rem;color:var(--muted)}
  .sbar-bg{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
  .sbar-fill{height:100%;background:var(--gold);border-radius:2px}

  /* ── Charts Row ─────────────────────────────────────────────── */
  .charts-row{display:grid;grid-template-columns:1fr 1fr;gap:7px}
  .chart-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:8px 12px 6px;display:flex;flex-direction:column;min-height:0}
  .chart-title{font-size:.6rem;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;
    display:flex;align-items:center;gap:10px}
  .ldot{display:inline-flex;align-items:center;gap:4px;font-size:.6rem;color:var(--muted)}
  .ldot::before{content:'';display:block;width:10px;height:3px;border-radius:2px;
    background:var(--dc,var(--ios))}
  .cw{flex:1;min-height:0;position:relative}

  /* ── Bottom Row ─────────────────────────────────────────────── */
  .bot-row{display:grid;grid-template-columns:1fr 1fr;gap:7px}
  .bot-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:8px 12px 8px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
  .card-ttl{font-size:.6rem;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.5px;margin-bottom:7px;flex-shrink:0}

  /* ── Version Table ──────────────────────────────────────────── */
  .ver-table{flex:1;display:flex;flex-direction:column;gap:6px;overflow:hidden}
  .ver-row{display:grid;grid-template-columns:80px 1fr 46px;align-items:center;gap:8px}
  .ver-lbl{font-size:.68rem;font-weight:500;color:var(--text);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .vbar-bg{height:6px;border-radius:3px;background:var(--border);overflow:hidden}
  .vbar-fill{height:100%;border-radius:3px;transition:width .5s ease}
  .ver-cnt{font-size:.62rem;color:var(--muted);text-align:right}

  /* ── Reviews Ticker ─────────────────────────────────────────── */
  .rev-wrap{flex:1;overflow:hidden;position:relative;min-height:0}
  .rev-track{display:flex;flex-direction:column;gap:6px}
  .rev-track.animate{animation:revScroll var(--dur,20s) linear infinite}
  @keyframes revScroll{0%{transform:translateY(0)}100%{transform:translateY(-50%)}}
  .rev-card{background:var(--card2);border:1px solid var(--border);border-radius:8px;
    padding:8px 10px;display:flex;flex-direction:column;gap:2px;overflow:hidden;flex-shrink:0}
  .rev-hdr{display:flex;align-items:center;gap:6px}
  .rev-stars{color:var(--gold);font-size:.65rem;letter-spacing:1px;line-height:1}
  .plat{font-size:.55rem;padding:1px 5px;border-radius:3px;font-weight:700;letter-spacing:.3px}
  .ios-p{background:rgba(6,182,212,.12);color:var(--ios)}
  .and-p{background:rgba(34,197,94,.12);color:var(--android)}
  .rev-date{font-size:.54rem;color:var(--muted);margin-left:auto}
  .rev-author{font-size:.62rem;font-weight:600;color:var(--text)}
  .rev-body{font-size:.63rem;color:var(--muted);line-height:1.35;overflow:hidden;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}

  /* ── Shimmer ─────────────────────────────────────────────────── */
  @keyframes shim{0%{background-position:-400px 0}100%{background-position:400px 0}}
  .shim{background:linear-gradient(90deg,var(--card) 25%,#16162a 50%,var(--card) 75%);
    background-size:800px 100%;animation:shim 1.5s infinite;border-radius:4px;display:block}
  .up{color:var(--android)} .dn{color:var(--red)} .mu{color:var(--muted)}

  /* ── Yesterday card per-platform rows ──────────────────────────── */
  .yd-row{display:grid;grid-template-columns:46px auto 1fr auto;align-items:center;
    gap:4px;flex-shrink:0}
  .yd-num{font-size:.85rem;font-weight:800;color:var(--text);letter-spacing:-.5px}
  .yd-dt{font-size:.52rem;color:var(--muted);white-space:nowrap}
  .yd-pct{font-size:.6rem;font-weight:700;white-space:nowrap;text-align:right}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <span class="logo"><img src="https://coinmania.ge/Images/coinmania.webp" alt="Coinmania"></span>
  <span class="pill" id="datePill">&#8212;</span>
  <span class="pill" id="updatedAt">&#8212;</span>
  <span class="live-badge"><span class="live-dot"></span>LIVE</span>
</div>

<!-- Row 1: KPIs -->
<div class="kpi-row">
  <!-- 1. Since Launch -->
  <div class="kpi" style="--accent:var(--ios)">
    <div class="kpi-lbl">Downloads &middot; Since Launch</div>
    <div class="kpi-val" id="k-sl">&#8212;</div>
    <div class="kpi-trend"></div>
    <div class="kpi-plat">
      <div class="kpi-prow"><span class="plt ios">iOS</span><span class="pnum" id="k-sl-ios">&#8212;</span></div>
      <div class="kpi-prow"><span class="plt and">Android</span><span class="pnum" id="k-sl-and">&#8212;</span></div>
    </div>
    <div class="kpi-ratio"><div class="kpi-ratio-ios" id="k-sl-bar" style="width:50%"></div></div>
  </div>
  <!-- 2. Last 30 Days -->
  <div class="kpi" style="--accent:var(--ios)">
    <div class="kpi-lbl">Downloads &middot; Last 30 Days</div>
    <div class="kpi-val" id="k-dl">&#8212;</div>
    <div class="kpi-trend" id="k-dl-t"></div>
    <div class="kpi-plat">
      <div class="kpi-prow"><span class="plt ios">iOS</span><span class="pnum" id="k-dl-ios">&#8212;</span></div>
      <div class="kpi-prow"><span class="plt and">Android</span><span class="pnum" id="k-dl-and">&#8212;</span></div>
    </div>
    <div class="kpi-ratio"><div class="kpi-ratio-ios" id="k-dl-bar" style="width:50%"></div></div>
  </div>
  <!-- 3. Yesterday -->
  <div class="kpi" style="--accent:var(--ios)">
    <div class="kpi-lbl">Downloads &middot; Latest Day</div>
    <div class="kpi-val" id="k-yd" style="font-size:1.45rem;margin:3px 0 5px">&#8212;</div>
    <div class="kpi-plat" style="gap:7px">
      <div class="yd-row">
        <span class="plt ios">iOS</span>
        <span class="yd-num" id="k-yd-ios">&#8212;</span>
        <span class="yd-dt" id="k-yd-ios-d"></span>
        <span class="yd-pct" id="k-yd-ios-t"></span>
      </div>
      <div class="yd-row">
        <span class="plt and">Android</span>
        <span class="yd-num" id="k-yd-and">&#8212;</span>
        <span class="yd-dt" id="k-yd-and-d"></span>
        <span class="yd-pct" id="k-yd-and-t"></span>
      </div>
    </div>
    <div class="kpi-ratio"><div class="kpi-ratio-ios" id="k-yd-bar" style="width:50%"></div></div>
  </div>
  <!-- 4. DAU -->
  <div class="kpi" style="--accent:var(--android)">
    <div class="kpi-lbl">Daily Active Users</div>
    <div class="kpi-val" id="k-dau">&#8212;</div>
    <div class="kpi-trend" id="k-dau-t"></div>
    <div class="kpi-plat">
      <div class="kpi-prow"><span class="plt ios">iOS</span><span class="pnum" id="k-dau-ios">&#8212;</span></div>
      <div class="kpi-prow"><span class="plt and">Android</span><span class="pnum" id="k-dau-and">&#8212;</span></div>
    </div>
    <div class="kpi-ratio"><div class="kpi-ratio-ios" id="k-dau-bar" style="width:50%"></div></div>
  </div>
  <!-- 5. Crash-Free -->
  <div class="kpi" style="--accent:var(--gold)">
    <div class="kpi-lbl">Crash-Free Sessions</div>
    <div class="kpi-val" id="k-cf">&#8212;</div>
    <div class="kpi-split" id="k-cf-s"></div>
    <div class="kpi-spark"><canvas id="cfSpark"></canvas></div>
  </div>
  <!-- 6. Rating -->
  <div class="kpi" style="--accent:var(--gold)">
    <div class="kpi-lbl">Average Rating</div>
    <div class="kpi-val" id="k-rt">&#8212;</div>
    <div class="kpi-split" id="k-rt-s"></div>
    <div class="kpi-stars" id="k-stars"></div>
  </div>
</div>

<!-- Row 2: Charts -->
<div class="charts-row">
  <div class="chart-card">
    <div class="chart-title">
      30-Day Installs
      <span class="ldot" style="--dc:var(--ios)">iOS</span>
      <span class="ldot" style="--dc:var(--android)">Android</span>
    </div>
    <div class="cw"><canvas id="lineC"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="chart-title">
      Monthly Totals &middot; Last 12 Months
      <span class="ldot" style="--dc:var(--ios)">iOS</span>
      <span class="ldot" style="--dc:var(--android)">Android</span>
    </div>
    <div class="cw"><canvas id="barC"></canvas></div>
  </div>
</div>

<!-- Row 3: Versions + Reviews -->
<div class="bot-row">
  <div class="bot-card">
    <div class="card-ttl" style="display:flex;align-items:center;gap:8px">Monthly Active Users &middot; All Time
      <span class="ldot" style="--dc:var(--ios)">iOS</span>
      <span class="ldot" style="--dc:var(--android)">Android</span>
    </div>
    <div class="cw"><canvas id="mauC"></canvas></div>
  </div>
  <div class="bot-card">
    <div class="card-ttl">Recent Reviews</div>
    <div class="rev-wrap"><div class="rev-track" id="revTrack"></div></div>
  </div>
</div>

<script>
var IOS  = '#06b6d4';
var AND  = '#22c55e';
var GOLD = '#f59e0b';

Chart.defaults.color = '#5a5a72';
Chart.defaults.borderColor = '#1a1a2e';
Chart.defaults.font.family = "-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif";
Chart.defaults.font.size = 10;

function fmtDateShort(s){
  if(!s)return'';
  var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var p=s.split('-');
  return mo[parseInt(p[1])-1]+' '+parseInt(p[2]);
}
function fmt(n){
  if(n==null)return'&#8212;';
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'K';
  return String(Math.round(n));
}
function fmtN(n){ return n==null?'&#8212;':Math.round(n).toLocaleString(); }
function sh(h,w){ return '<span class="shim" style="height:'+h+';width:'+(w||'65%')+'"></span>'; }

// Set platform breakdown rows + ratio bar
function setPlat(iosId, andId, barId, iosVal, andVal){
  var ie=document.getElementById(iosId);
  var ae=document.getElementById(andId);
  var be=document.getElementById(barId);
  if(ie) ie.textContent = iosVal!=null ? Math.round(iosVal).toLocaleString() : '—';
  if(ae) ae.textContent = andVal!=null ? Math.round(andVal).toLocaleString() : '—';
  if(be && iosVal!=null && andVal!=null){
    var tot=iosVal+andVal;
    be.style.width = tot>0 ? (iosVal/tot*100).toFixed(1)+'%' : '50%';
  }
}

var _cfChart=null, _lineChart=null, _barChart=null, _mauChart=null;

// ── Card 1: Since Launch ──────────────────────────────────────────────────
function renderSL(data,android){
  var monthly=(data.monthly)||[];
  var iosTotal=0;
  monthly.forEach(function(m){ iosTotal+=(m.units||0); });
  var curMonth=new Date().toISOString().slice(0,7);
  var daily=(data.sales&&data.sales.daily)||[];
  daily.forEach(function(d){
    if(d.date&&d.date.slice(0,7)===curMonth){ iosTotal+=(d.units||0); }
  });
  var andTotal=android.total_installs||null;
  var total=(iosTotal||0)+(andTotal||0);
  document.getElementById('k-sl').innerHTML=fmtN(total||null);
  setPlat('k-sl-ios','k-sl-and','k-sl-bar',iosTotal||null,andTotal);
}

// ── Card 2: Downloads 30D ────────────────────────────────────────────────
function renderDL(data,android){
  var daily=(data.sales&&data.sales.daily)||[];
  var ios30=0;
  daily.forEach(function(d){ ios30+=(d.units||0); });
  var and30=android.installs_30d||0;
  var total=ios30+(and30||0);
  document.getElementById('k-dl').innerHTML=fmtN(total||ios30);
  // Combined trend: (ios30+and30) vs (iosPrev30+andPrev30)
  var te=document.getElementById('k-dl-t');
  var iosPrev30=(data.sales&&data.sales.prev30d)?data.sales.prev30d.units:null;
  var andPrev30=android.installs_prev_30d||null;
  var combinedPrev=(iosPrev30||0)+(andPrev30||0);
  if(combinedPrev>0){
    var pct=Math.round((total-combinedPrev)/combinedPrev*100);
    te.innerHTML=(pct>=0?'&#9650; +':'&#9660; ')+pct+'% vs prev 30d';
    te.className='kpi-trend '+(pct>=0?'up':'dn');
  } else if(data.sales&&data.sales.change_30d_pct!=null){
    var p=Math.round(data.sales.change_30d_pct);
    te.innerHTML=(p>=0?'&#9650; +':'&#9660; ')+p+'% vs prev 30d';
    te.className='kpi-trend '+(p>=0?'up':'dn');
  }
  setPlat('k-dl-ios','k-dl-and','k-dl-bar',ios30||null,and30||null);
}

// ── Card 3: Yesterday ────────────────────────────────────────────────────
function renderYD(data,android){
  var daily=(data.sales&&data.sales.daily)||[];
  var sortedD=daily.slice().sort(function(a,b){return a.date<b.date?-1:1;});

  // iOS: skip zero-unit entries (API lag returns 0 for dates not yet available)
  var validD=sortedD.filter(function(d){return (d.units||0)>0;});
  var iosLatest=validD.length?validD[validD.length-1]:null;
  var iosPrev=validD.length>1?validD[validD.length-2]:null;
  var iosVal=iosLatest?(iosLatest.units||0):0;
  var iosPrevVal=iosPrev?(iosPrev.units||0):0;
  var iosDateStr=iosLatest?iosLatest.date:null;

  // Android: from backend (GCS or historical fallback)
  var andVal=android.installs_yesterday||0;
  var andDb=android.and_day_before||0;
  var andDateStr=android.installs_yesterday_date||null;

  // Combined total for header
  var total=(iosVal||0)+(andVal||0);
  document.getElementById('k-yd').innerHTML=total>0?fmtN(total):sh('1.5rem');

  // iOS row
  var iosNumEl=document.getElementById('k-yd-ios');
  var iosDtEl=document.getElementById('k-yd-ios-d');
  var iosTEl=document.getElementById('k-yd-ios-t');
  if(iosNumEl) iosNumEl.textContent=iosVal>0?Math.round(iosVal).toLocaleString():'—';
  if(iosDtEl)  iosDtEl.textContent=fmtDateShort(iosDateStr);
  if(iosTEl&&iosPrevVal>0){
    var pct=Math.round((iosVal-iosPrevVal)/iosPrevVal*100);
    iosTEl.innerHTML=(pct>=0?'&#9650; +':'&#9660; ')+pct+'%';
    iosTEl.className='yd-pct '+(pct>=0?'up':'dn');
  }

  // Android row
  var andNumEl=document.getElementById('k-yd-and');
  var andDtEl=document.getElementById('k-yd-and-d');
  var andTEl=document.getElementById('k-yd-and-t');
  if(andNumEl) andNumEl.textContent=andVal>0?Math.round(andVal).toLocaleString():'—';
  if(andDtEl)  andDtEl.textContent=fmtDateShort(andDateStr);
  if(andTEl&&andDb>0&&andVal>0){
    var pct=Math.round((andVal-andDb)/andDb*100);
    andTEl.innerHTML=(pct>=0?'&#9650; +':'&#9660; ')+pct+'%';
    andTEl.className='yd-pct '+(pct>=0?'up':'dn');
  }

  // Ratio bar
  var be=document.getElementById('k-yd-bar');
  if(be&&iosVal&&andVal){
    be.style.width=(iosVal/(iosVal+andVal)*100).toFixed(1)+'%';
  }
}

// ── Card 4: DAU ──────────────────────────────────────────────────────────
function renderDAU(analytics,android){
  var aD=(analytics.status==='ready')?(analytics.data||{}):{};
  var sum=aD.summary||{};
  // iOS DAU: from analytics if available, else from historical CSV
  var iosDau=null;
  if(sum.sessions_30d) iosDau=Math.round(sum.sessions_30d/30);
  if(!iosDau && android.ios_dau_latest) iosDau=android.ios_dau_latest;
  var andU=android.distinct_users||null;
  var total=(iosDau||0)+(andU||0);
  document.getElementById('k-dau').innerHTML=total>0?fmtN(total):(andU?fmtN(andU):sh('2rem'));
  // % change vs previous day (using historical data)
  var te=document.getElementById('k-dau-t');
  if(te){
    var iosLatest=android.ios_dau_latest||null;
    var iosPrev=android.ios_dau_prev||null;
    var andPrev=android.and_dau_prev||null;
    var cur=(iosLatest||0)+(andU||0);
    var prev=(iosPrev||0)+(andPrev||0);
    if(prev>0&&cur>0){
      var pct=Math.round((cur-prev)/prev*100);
      te.innerHTML=(pct>=0?'&#9650; +':'&#9660; ')+pct+'% vs prev day';
      te.className='kpi-trend '+(pct>=0?'up':'dn');
    }
  }
  setPlat('k-dau-ios','k-dau-and','k-dau-bar',iosDau,andU);
}

// ── Card 5: Crash-Free ───────────────────────────────────────────────────
function renderCF(analytics,android){
  var aD=(analytics.status==='ready')?(analytics.data||{}):{};
  var sum=aD.summary||{};
  var iosCF=null;
  if(sum.sessions_30d&&sum.crashes_30d!=null)
    iosCF=(1-sum.crashes_30d/sum.sessions_30d)*100;
  var andCF=android.crash_rate!=null?(1-android.crash_rate)*100:null;
  var show=null;
  if(iosCF!=null&&andCF!=null) show=(iosCF+andCF)/2;
  else show=andCF!=null?andCF:iosCF;
  document.getElementById('k-cf').innerHTML=show!=null?show.toFixed(2)+'%':sh('2rem');
  var parts=[];
  if(iosCF!=null) parts.push('<span class="ios">iOS '+iosCF.toFixed(2)+'%</span>');
  if(andCF!=null) parts.push('<span class="and">Android '+andCF.toFixed(2)+'%</span>');
  document.getElementById('k-cf-s').innerHTML=parts.join('<span class="sep"> &middot; </span>');
  var daily=aD.daily||{};
  var dates=Object.keys(daily).sort().slice(-7);
  var vals=[];
  dates.forEach(function(d){
    var day=daily[d];
    if(day&&day.sessions) vals.push((1-(day.crashes||0)/day.sessions)*100);
  });
  buildCFSpark(vals);
}
function buildCFSpark(vals){
  var el=document.getElementById('cfSpark');
  if(!el)return;
  var ctx=el.getContext('2d');
  if(_cfChart){_cfChart.destroy();_cfChart=null;}
  if(!vals||vals.length<2)return;
  _cfChart=new Chart(ctx,{
    type:'line',
    data:{labels:vals.map(function(_,i){return i;}),
      datasets:[{data:vals,borderColor:GOLD,borderWidth:2,pointRadius:0,
        tension:.4,fill:true,backgroundColor:'rgba(245,158,11,.08)'}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{enabled:false},datalabels:{display:false}},
      scales:{x:{display:false},y:{display:false}},animation:false}
  });
}

// ── Card 6: Rating ───────────────────────────────────────────────────────
function renderRating(reviews,android){
  var iosA=reviews&&reviews.average!=null?reviews.average:null;
  var andA=android.avg_rating!=null?android.avg_rating:null;
  var combined=null;
  if(iosA!=null&&andA!=null) combined=(iosA+andA)/2;
  else combined=andA!=null?andA:iosA;
  document.getElementById('k-rt').innerHTML=combined!=null?combined.toFixed(1)+' &#9733;':sh('2rem');
  var parts=[];
  if(iosA!=null) parts.push('<span class="ios">iOS '+iosA.toFixed(1)+'</span>');
  if(andA!=null) parts.push('<span class="and">Android '+andA.toFixed(1)+'</span>');
  document.getElementById('k-rt-s').innerHTML=parts.join('<span class="sep"> &middot; </span>');
  var dist=(reviews&&reviews.distribution)||{};
  var aD=android.dist||{};
  var total=0;
  for(var s=1;s<=5;s++) total+=(parseInt(dist[s])||0)+(parseInt(aD[s])||0);
  var wrap=document.getElementById('k-stars');
  if(total>0){
    var h='';
    for(var star=5;star>=1;star--){
      var cnt=(parseInt(dist[star])||0)+(parseInt(aD[star])||0);
      var p=cnt/total*100;
      h+='<div class="star-row"><span>'+star+'&#9733;</span>'+
        '<div class="sbar-bg"><div class="sbar-fill" style="width:'+p.toFixed(1)+'%"></div></div>'+
        '<span>'+cnt+'</span></div>';
    }
    wrap.innerHTML=h;
  }
}

// ── Chart 1: 30-Day Line ─────────────────────────────────────────────────
function renderLineChart(data,android){
  var daily=(data.sales&&data.sales.daily)||[];
  var sorted=daily.slice().sort(function(a,b){return a.date<b.date?-1:1;}).slice(-30);
  // Android daily from historical
  var andDaily=android.daily_30d||[];
  var andMap={};
  andDaily.forEach(function(d){andMap[d.date]=d.units||0;});
  var labels=sorted.map(function(d){
    var dt=new Date(d.date+'T12:00:00');
    return (dt.getMonth()+1)+'/'+(dt.getDate());
  });
  var iosD=sorted.map(function(d){return d.units||0;});
  var andD=sorted.map(function(d){return andMap[d.date]||0;});
  var combD=sorted.map(function(d,i){return(iosD[i]||0)+(andD[i]||0);});
  var n=sorted.length;
  var ctx=document.getElementById('lineC').getContext('2d');
  if(_lineChart){_lineChart.destroy();}
  _lineChart=new Chart(ctx,{
    type:'line',
    data:{labels:labels,datasets:[
      {label:'Android',data:andD,borderColor:AND,
        backgroundColor:'rgba(34,197,94,.06)',
        borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:.3,fill:true,
        datalabels:{display:false}},
      {label:'iOS',data:iosD,borderColor:IOS,
        backgroundColor:'rgba(6,182,212,.08)',
        borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:.3,fill:true,
        datalabels:{display:false}},
      {label:'_comb',data:combD,
        borderColor:'rgba(255,255,255,.15)',borderWidth:1,
        backgroundColor:'rgba(0,0,0,0)',
        pointRadius:0,tension:.3,fill:false,
        datalabels:{
          display:function(ctx){
            var i=ctx.dataIndex;
            return i===n-1||i%5===0;
          },
          anchor:'center',align:'top',offset:6,
          color:'rgba(200,200,220,.8)',font:{size:8,weight:'600'},
          formatter:function(v){return v>0?v.toLocaleString():null;}
        }}
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#0d0d1a',titleColor:'#eeeef5',
          bodyColor:'#9999b8',borderColor:'#1a1a2e',borderWidth:1,padding:10,
          filter:function(item){return item.dataset.label!=='_comb';},
          callbacks:{
            afterBody:function(items){
              var fl=items.filter(function(i){return i.dataset.label!=='_comb';});
              var tot=fl.reduce(function(s,i){return s+(i.parsed.y||0);},0);
              return tot>0?['─────────','Total: '+tot.toLocaleString()]:'';
            }
          }
        },
        datalabels:{display:false}
      },
      scales:{
        x:{grid:{color:'rgba(26,26,46,.8)'},ticks:{maxTicksLimit:10,maxRotation:0}},
        y:{grid:{color:'rgba(26,26,46,.8)'},beginAtZero:true,ticks:{precision:0}}
      }
    },
    plugins:[ChartDataLabels]
  });
}

// ── Chart 2: Monthly Stacked Bar ─────────────────────────────────────────
function renderBarChart(data,android){
  var iosMonthly=(data.monthly||[]).slice(-12);
  var andMonthly=(android.monthly_hist||[]);
  var andMap={};
  andMonthly.forEach(function(m){andMap[m.month]=m.units||0;});
  var months=iosMonthly.map(function(m){return m.month;});
  var names=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var lbls=months.map(function(mo){
    var p=mo.split('-');
    return names[parseInt(p[1])-1]+" '"+p[0].slice(2);
  });
  var iosV=iosMonthly.map(function(m){return m.units||0;});
  var andV=months.map(function(mo){return andMap[mo]||0;});
  var maxBar=0;
  months.forEach(function(mo,i){maxBar=Math.max(maxBar,(iosV[i]||0)+(andV[i]||0));});
  var ctx=document.getElementById('barC').getContext('2d');
  if(_barChart){_barChart.destroy();}
  _barChart=new Chart(ctx,{
    type:'bar',
    data:{labels:lbls,datasets:[
      {
        label:'Android',data:andV,
        backgroundColor:'rgba(34,197,94,.5)',
        borderColor:'rgba(34,197,94,.8)',borderWidth:1,
        borderRadius:0,stack:'s',
        datalabels:{
          display:function(ctx){return(ctx.dataset.data[ctx.dataIndex]||0)>=100;},
          anchor:'center',align:'center',
          color:'rgba(255,255,255,.9)',font:{size:8,weight:'600'},
          formatter:function(v){return v>0?Math.round(v).toLocaleString():null;}
        }
      },
      {
        label:'iOS',data:iosV,
        backgroundColor:'rgba(6,182,212,.6)',
        borderColor:'rgba(6,182,212,.9)',borderWidth:1,
        borderRadius:{topLeft:4,topRight:4},stack:'s',
        datalabels:{
          display:function(ctx){return(ctx.dataset.data[ctx.dataIndex]||0)>=100;},
          anchor:'center',align:'center',
          color:'rgba(255,255,255,.9)',font:{size:8,weight:'600'},
          formatter:function(v){return v>0?Math.round(v).toLocaleString():null;}
        }
      },
      {
        label:'_tot',data:months.map(function(){return 1;}),
        backgroundColor:'rgba(0,0,0,0)',borderWidth:0,
        stack:'s',
        datalabels:{
          labels:{
            num:{
              display:true,
              anchor:'end',align:'top',offset:5,
              color:'#c0c0d8',font:{size:9,weight:'bold'},
              formatter:function(v,ctx){
                var i=ctx.dataIndex;
                var tot=(iosV[i]||0)+(andV[i]||0);
                return tot>0?tot.toLocaleString():null;
              }
            },
            pct:{
              display:function(ctx){
                var i=ctx.dataIndex;
                return i>0&&((iosV[i-1]||0)+(andV[i-1]||0))>0;
              },
              anchor:'end',align:'top',offset:18,
              color:function(ctx){
                var i=ctx.dataIndex;
                var tot=(iosV[i]||0)+(andV[i]||0);
                var prev=(iosV[i-1]||0)+(andV[i-1]||0);
                return tot>=prev?'#4ade80':'#f87171';
              },
              font:{size:8,weight:'700'},
              formatter:function(v,ctx){
                var i=ctx.dataIndex;
                if(i===0)return null;
                var tot=(iosV[i]||0)+(andV[i]||0);
                var prev=(iosV[i-1]||0)+(andV[i-1]||0);
                if(!prev)return null;
                var p=Math.round((tot-prev)/prev*100);
                return(p>=0?'▲ +':'▼ ')+p+'%';
              }
            }
          }
        }
      }
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,
      layout:{padding:{top:42}},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#0d0d1a',titleColor:'#eeeef5',
          bodyColor:'#9999b8',borderColor:'#1a1a2e',borderWidth:1,padding:10,
          filter:function(item){return item.dataset.label!=='_tot';},
          callbacks:{
            label:function(item){
              return '  '+item.dataset.label+': '+item.parsed.y.toLocaleString();
            },
            afterBody:function(items){
              var fl=items.filter(function(i){return i.dataset.label!=='_tot';});
              if(!fl.length)return'';
              var i=fl[0].dataIndex;
              var tot=(iosV[i]||0)+(andV[i]||0);
              return tot>0?['','  Total: '+tot.toLocaleString()]:'';
            }
          }
        },
        datalabels:{}
      },
      scales:{
        x:{grid:{display:false},stacked:true,ticks:{maxRotation:0}},
        y:{grid:{color:'rgba(26,26,46,.8)'},stacked:true,beginAtZero:true,
          suggestedMax:Math.max(500,Math.ceil(maxBar*1.22/100)*100),
          ticks:{precision:0}}
      }
    },
    plugins:[ChartDataLabels]
  });
}

// ── MAU Chart ────────────────────────────────────────────────────────────
function renderMAUChart(android){
  var series=android.mau_hist||[];
  if(!series.length)return;
  var labels=series.map(function(d){
    var dt=new Date(d.date+'T12:00:00');
    return (dt.getMonth()+1)+'/'+(dt.getDate());
  });
  var iosD=series.map(function(d){return d.ios||0;});
  var andD=series.map(function(d){return d.and||0;});
  var el=document.getElementById('mauC');
  if(!el)return;
  var ctx=el.getContext('2d');
  if(_mauChart){_mauChart.destroy();_mauChart=null;}
  _mauChart=new Chart(ctx,{
    type:'line',
    data:{labels:labels,datasets:[
      {label:'Android',data:andD,borderColor:AND,
        backgroundColor:'rgba(34,197,94,.07)',
        borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:.35,fill:true},
      {label:'iOS',data:iosD,borderColor:IOS,
        backgroundColor:'rgba(6,182,212,.09)',
        borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:.35,fill:true}
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#0d0d1a',titleColor:'#eeeef5',
          bodyColor:'#9999b8',borderColor:'#1a1a2e',borderWidth:1,padding:10,
          callbacks:{
            afterBody:function(items){
              var tot=items.reduce(function(s,i){return s+(i.parsed.y||0);},0);
              return tot>0?['─────────','Total: '+tot.toLocaleString()]:'';
            }
          }
        },
        datalabels:{display:false}
      },
      scales:{
        x:{grid:{color:'rgba(26,26,46,.8)'},ticks:{maxTicksLimit:9,maxRotation:0}},
        y:{grid:{color:'rgba(26,26,46,.8)'},beginAtZero:true,ticks:{precision:0}}
      }
    }
  });
}

// ── Version Adoption ─────────────────────────────────────────────────────
function renderVersions(android){
  var vd=android.version_data||[];
  var el=document.getElementById('verTable');
  if(!vd.length){
    var h='';
    for(var i=0;i<6;i++)
      h+='<div class="ver-row">'+sh('.75rem','65px')+sh('6px','100%')+sh('.75rem','35px')+'</div>';
    el.innerHTML=h; return;
  }
  var max=Math.max.apply(null,vd.map(function(v){return v.installs;}));
  el.innerHTML=vd.map(function(v){
    var p=max>0?(v.installs/max*100):0;
    var c=v.platform==='ios'?IOS:AND;
    return '<div class="ver-row">'+
      '<span class="ver-lbl">v'+v.version+'</span>'+
      '<div class="vbar-bg"><div class="vbar-fill" style="width:'+p.toFixed(1)+'%;background:'+c+'"></div></div>'+
      '<span class="ver-cnt">'+fmt(v.installs)+'</span>'+
      '</div>';
  }).join('');
}

// ── Reviews Ticker ───────────────────────────────────────────────────────
function renderReviews(reviews,android){
  var ios=((reviews&&reviews.recent)||[]).map(function(r){return Object.assign({},r,{platform:'ios'});});
  var and=(android.reviews||[]).map(function(r){return Object.assign({},r,{platform:'android'});});
  var out=[],ii=0,ai=0;
  while(ii<ios.length||ai<and.length){
    if(ai<and.length) out.push(and[ai++]);
    if(ii<ios.length) out.push(ios[ii++]);
  }
  var el=document.getElementById('revTrack');
  if(!out.length){
    el.className='rev-track';
    el.innerHTML='<div class="rev-card">'+sh('.7rem')+sh('.62rem')+sh('2.5rem','100%')+'</div>'.repeat(4);
    return;
  }
  function cardHTML(r){
    var stars='';
    for(var i=1;i<=5;i++) stars+=i<=(r.rating||0)?'&#9733;':'&#9734;';
    var platCls=r.platform==='ios'?'ios-p':'and-p';
    var platTxt=r.platform==='ios'?'iOS':'Android';
    var body=(r.body||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    var author=(r.author||'Anonymous').replace(/</g,'&lt;');
    return '<div class="rev-card">'+
      '<div class="rev-hdr">'+
        '<span class="rev-stars">'+stars+'</span>'+
        '<span class="plat '+platCls+'">'+platTxt+'</span>'+
        '<span class="rev-date">'+(r.date||'')+'</span>'+
      '</div>'+
      '<div class="rev-author">'+author+'</div>'+
      '<div class="rev-body">'+(body||'<em style="color:var(--muted)">No text</em>')+'</div>'+
      '</div>';
  }
  // Duplicate list for seamless loop
  var html=out.map(cardHTML).join('');
  el.innerHTML=html+html;
  // Speed: ~4s per card
  var dur=Math.max(10,out.length*4);
  el.style.setProperty('--dur',dur+'s');
  el.className='rev-track animate';
}

// ── Main Render ───────────────────────────────────────────────────────────
function render(cache){
  if(!cache)return;
  var data=cache.data||{};
  var analytics=cache.analytics||{status:'pending',data:{daily:{},summary:{},retention:{}}};
  var android=cache.android||{};
  var now=new Date();
  var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  document.getElementById('datePill').textContent=mo[now.getMonth()]+' '+now.getDate()+', '+now.getFullYear();
  if(cache.updatedAt){
    var u=new Date(cache.updatedAt);
    document.getElementById('updatedAt').textContent='Updated '+u.toLocaleTimeString();
  }
  renderSL(data,android);
  renderDL(data,android);
  renderYD(data,android);
  renderDAU(analytics,android);
  renderCF(analytics,android);
  renderRating(data.reviews||{},android);
  renderLineChart(data,android);
  renderBarChart(data,android);
  renderMAUChart(android);
  renderReviews(data.reviews||{},android);
}

function poll(){
  fetch('/data')
    .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(d){render(d);})
    .catch(function(e){console.warn('Poll:',e);});
}
poll();
setInterval(poll,5*60*1000);
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
