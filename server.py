"""
App Metrics MCP Server
======================
Exposes read-only Google Play + App Store Connect metrics as MCP tools so
Claude (and Claude artifacts) can pull live download/uninstall/active-user/
crash/rating data.

Run locally:
    python server.py

Configure in Cowork as an MCP server pointing at this file.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import time
import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import jwt
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build as gbuild
from google.cloud import storage as gcs
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GOOGLE_SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")
PLAY_PACKAGE_NAME = os.environ.get("PLAY_PACKAGE_NAME", "")
PLAY_GCS_BUCKET = os.environ.get("PLAY_GCS_BUCKET", "")

ASC_KEY_ID = os.environ.get("ASC_KEY_ID", "")
ASC_ISSUER_ID = os.environ.get("ASC_ISSUER_ID", "")
ASC_PRIVATE_KEY_PATH = os.environ.get("ASC_PRIVATE_KEY_PATH", "")
ASC_VENDOR_NUMBER = os.environ.get("ASC_VENDOR_NUMBER", "")
ASC_APP_ID = os.environ.get("ASC_APP_ID", "")

ASC_BASE = "https://api.appstoreconnect.apple.com"

mcp = FastMCP("app-metrics")


# ---------------------------------------------------------------------------
# Google Play helpers
# ---------------------------------------------------------------------------
def _play_credentials(scopes: list[str]):
    if not GOOGLE_SA_JSON or not Path(GOOGLE_SA_JSON).exists():
        raise RuntimeError(
            "GOOGLE_SA_JSON not set or file missing. See SETUP.md Part A."
        )
    return service_account.Credentials.from_service_account_file(
        GOOGLE_SA_JSON, scopes=scopes
    )


def _play_reporting_client():
    creds = _play_credentials(
        ["https://www.googleapis.com/auth/playdeveloperreporting"]
    )
    return gbuild("playdeveloperreporting", "v1beta1", credentials=creds)


def _play_publisher_client():
    creds = _play_credentials(
        ["https://www.googleapis.com/auth/androidpublisher"]
    )
    return gbuild("androidpublisher", "v3", credentials=creds)


def _play_gcs_client():
    creds = _play_credentials(["https://www.googleapis.com/auth/devstorage.read_only"])
    return gcs.Client(credentials=creds, project=creds.project_id)


# ---------------------------------------------------------------------------
# App Store Connect helpers
# ---------------------------------------------------------------------------
def _asc_jwt() -> str:
    if not (ASC_KEY_ID and ASC_ISSUER_ID and ASC_PRIVATE_KEY_PATH):
        raise RuntimeError(
            "App Store Connect credentials missing. See SETUP.md Part B."
        )
    with open(ASC_PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    now = int(time.time())
    headers = {"alg": "ES256", "kid": ASC_KEY_ID, "typ": "JWT"}
    payload = {
        "iss": ASC_ISSUER_ID,
        "iat": now,
        "exp": now + 60 * 19,  # max 20 min
        "aud": "appstoreconnect-v1",
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def _asc_get(path: str, params: dict | None = None, accept: str = "application/json") -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {_asc_jwt()}",
        "Accept": accept,
    }
    url = path if path.startswith("http") else f"{ASC_BASE}{path}"
    r = httpx.get(url, headers=headers, params=params, timeout=60.0)
    r.raise_for_status()
    return r


def _asc_post(path: str, body: dict) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {_asc_jwt()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    r = httpx.post(f"{ASC_BASE}{path}", headers=headers, json=body, timeout=60.0)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Tools — Google Play
# ---------------------------------------------------------------------------
@mcp.tool()
def play_vitals(
    metric_set: str = "crashRate",
    days: int = 28,
) -> dict[str, Any]:
    """
    Query a Play Developer Reporting API metric set for the given time window.

    Args:
        metric_set: One of: crashRate, anrRate, slowStart, slowRendering20Fps,
                    slowRendering30Fps, stuckBackgroundWakelockRate,
                    excessiveWakeupRate, errorCount.
        days: Number of trailing days to fetch (default 28).

    Returns: { metricSet, rows: [{ date, dimensions, metrics }] }
    """
    parent = f"apps/{PLAY_PACKAGE_NAME}"

    set_to_endpoint = {
        "crashRate":                  ("crashRateMetricSet",                 ["crashRate", "userPerceivedCrashRate"]),
        "anrRate":                    ("anrRateMetricSet",                   ["anrRate", "userPerceivedAnrRate"]),
        "slowStart":                  ("slowStartRateMetricSet",             ["slowStartRate"]),
        "slowRendering20Fps":         ("slowRenderingRate20FpsMetricSet",    ["slowRenderingRate20Fps"]),
        "slowRendering30Fps":         ("slowRenderingRate30FpsMetricSet",    ["slowRenderingRate30Fps"]),
        "stuckBackgroundWakelockRate":("stuckBackgroundWakelockRateMetricSet",["stuckBgWakelockRate"]),
        "excessiveWakeupRate":        ("excessiveWakeupRateMetricSet",       ["excessiveWakeupRate"]),
        "errorCount":                 ("errorCountMetricSet",                ["errorReportCount"]),
    }
    if metric_set not in set_to_endpoint:
        raise ValueError(f"Unknown metric_set: {metric_set}")
    endpoint, metrics = set_to_endpoint[metric_set]

    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=days)
    body = {
        "timelineSpec": {
            "aggregationPeriod": "DAILY",
            "startTime": {"year": start.year, "month": start.month, "day": start.day},
            "endTime":   {"year": end.year,   "month": end.month,   "day": end.day},
        },
        "metrics": metrics,
    }

    # Call the Reporting API REST endpoint directly — simpler than navigating
    # the discovery client's nested .vitals().<set>().query() routing.
    set_name = endpoint[0].lower() + endpoint[1:]
    name = f"{parent}/{set_name}"
    url = f"https://playdeveloperreporting.googleapis.com/v1beta1/{name}:query"

    creds = _play_credentials(["https://www.googleapis.com/auth/playdeveloperreporting"])
    creds.refresh(__import__("google.auth.transport.requests", fromlist=["Request"]).Request())
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    r = httpx.post(url, headers=headers, json=body, timeout=60.0)
    r.raise_for_status()
    return {"metricSet": metric_set, "raw": r.json()}


@mcp.tool()
def play_reviews(max_results: int = 50) -> dict[str, Any]:
    """
    Recent Play Store reviews + summary.
    Returns the most recent reviews and computes average rating across them.
    """
    client = _play_publisher_client()
    resp = client.reviews().list(
        packageName=PLAY_PACKAGE_NAME,
        maxResults=max_results,
    ).execute()
    reviews = resp.get("reviews", [])
    ratings = []
    for rv in reviews:
        for c in rv.get("comments", []):
            uc = c.get("userComment") or {}
            if "starRating" in uc:
                ratings.append(uc["starRating"])
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "count": len(reviews),
        "averageRecent": avg,
        "ratingDistribution": {str(s): ratings.count(s) for s in [1,2,3,4,5]},
        "sample": [
            {
                "rating": (rv.get("comments", [{}])[0].get("userComment") or {}).get("starRating"),
                "text":   (rv.get("comments", [{}])[0].get("userComment") or {}).get("text"),
                "lastModified": rv.get("comments", [{}])[0].get("userComment", {}).get("lastModified"),
            }
            for rv in reviews[:10]
        ],
    }


@mcp.tool()
def play_installs_uninstalls(days: int = 30) -> dict[str, Any]:
    """
    Read installs / uninstalls / active-device counts from the daily Cloud Storage
    CSV exports Play writes to your pubsite bucket. Requires PLAY_GCS_BUCKET
    set and the service account having Storage Object Viewer on that bucket.

    Returns daily series for: installs, uninstalls, currentDeviceInstalls (active).
    """
    if not PLAY_GCS_BUCKET:
        raise RuntimeError("PLAY_GCS_BUCKET not set. See SETUP.md Part A.5.")

    client = _play_gcs_client()
    bucket = client.bucket(PLAY_GCS_BUCKET)

    end_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    months = []
    cur = end_month
    for _ in range(2):  # current + previous month covers 30+ days
        months.append(cur)
        cur = (cur - dt.timedelta(days=1)).replace(day=1)

    series: dict[str, dict[str, int]] = {}  # date -> metric -> value

    for m in months:
        # Path: stats/installs/installs_<package>_<YYYYMM>_overview.csv
        prefix = f"stats/installs/installs_{PLAY_PACKAGE_NAME}_{m.strftime('%Y%m')}_overview.csv"
        blob = bucket.blob(prefix)
        if not blob.exists():
            continue
        data = blob.download_as_text(encoding="utf-16")
        reader = csv.DictReader(io.StringIO(data))
        for row in reader:
            date = row.get("Date") or row.get("﻿Date")
            if not date:
                continue
            entry = series.setdefault(date, {})
            # Column names in the overview CSV:
            for col, key in [
                ("Daily User Installs", "installs"),
                ("Daily User Uninstalls", "uninstalls"),
                ("Daily Device Installs", "deviceInstalls"),
                ("Daily Device Uninstalls", "deviceUninstalls"),
                ("Active Device Installs", "activeDevices"),
                ("Total User Installs", "totalUserInstalls"),
            ]:
                if col in row and row[col]:
                    try:
                        entry[key] = int(row[col])
                    except ValueError:
                        pass

    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)).isoformat()
    rows = sorted([{"date": d, **vals} for d, vals in series.items() if d >= cutoff],
                  key=lambda r: r["date"])
    return {"package": PLAY_PACKAGE_NAME, "rows": rows}


# ---------------------------------------------------------------------------
# Tools — App Store Connect
# ---------------------------------------------------------------------------
@mcp.tool()
def appstore_sales(report_date: str | None = None, frequency: str = "DAILY") -> dict[str, Any]:
    """
    Fetch App Store Sales report (synchronous; gzipped TSV).

    NOTE: Apple's salesReports endpoint requires the API key's role to be Admin,
    Finance, or Sales-and-Reports. Keys created with the Developer role (which
    we use for everything else) get HTTP 403 here. If you hit a 403, use the
    appstore_analytics_* tools instead — they cover downloads/installs and have
    Developer-role access.

    Args:
        report_date: YYYY-MM-DD for DAILY, YYYY-MM-DD (Sunday) for WEEKLY,
                     YYYY-MM for MONTHLY, YYYY for YEARLY. Defaults to yesterday.
        frequency: DAILY | WEEKLY | MONTHLY | YEARLY.

    Returns: parsed rows, filtered to your ASC_APP_ID where applicable.
    """
    if report_date is None:
        report_date = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()

    params = {
        "filter[frequency]": frequency,
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": ASC_VENDOR_NUMBER,
        "filter[reportDate]": report_date,
        "filter[version]": "1_1",
    }
    r = _asc_get("/v1/salesReports", params=params, accept="application/a-gzip")
    raw = gzip.decompress(r.content).decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw), delimiter="\t")
    rows = []
    for row in reader:
        # Filter to our app if Apple Identifier matches
        if ASC_APP_ID and row.get("Apple Identifier") not in ("", ASC_APP_ID):
            continue
        rows.append(row)
    return {"frequency": frequency, "reportDate": report_date, "rows": rows}


@mcp.tool()
def appstore_reviews(limit: int = 50, territory: str | None = None) -> dict[str, Any]:
    """
    Recent customer reviews + average for the configured app.
    """
    params = {"limit": min(limit, 200)}
    if territory:
        params["filter[territory]"] = territory
    path = f"/v1/apps/{ASC_APP_ID}/customerReviews"
    r = _asc_get(path, params=params)
    data = r.json().get("data", [])
    ratings = [d["attributes"].get("rating") for d in data if d.get("attributes", {}).get("rating") is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "count": len(data),
        "averageRecent": avg,
        "ratingDistribution": {str(s): ratings.count(s) for s in [1,2,3,4,5]},
        "sample": [
            {
                "rating": d["attributes"].get("rating"),
                "title":  d["attributes"].get("title"),
                "body":   d["attributes"].get("body"),
                "createdDate": d["attributes"].get("createdDate"),
                "territory":   d["attributes"].get("territory"),
            }
            for d in data[:10]
        ],
    }


@mcp.tool()
def appstore_analytics_request(access_type: str = "ONGOING") -> dict[str, Any]:
    """
    Create an analytics report request for the configured app. This must be
    done once per app per access_type; afterwards reports are generated on a
    schedule and you fetch instances via appstore_analytics_list_reports.

    access_type: ONE_TIME_SNAPSHOT | ONGOING.
    """
    body = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {"accessType": access_type},
            "relationships": {
                "app": {"data": {"type": "apps", "id": ASC_APP_ID}}
            },
        }
    }
    r = _asc_post("/v1/analyticsReportRequests", body)
    return r.json()


@mcp.tool()
def appstore_analytics_list_reports(request_id: str, name_contains: str | None = None) -> dict[str, Any]:
    """
    List the report families inside an analyticsReportRequest. Common report
    names of interest: "App Store Installation and Deletion Standard",
    "App Sessions Standard", "App Store Discovery and Engagement Standard",
    "App Crashes Standard".
    """
    params = {"limit": 200}
    if name_contains:
        params["filter[name]"] = name_contains
    r = _asc_get(f"/v1/analyticsReportRequests/{request_id}/reports", params=params)
    return r.json()


@mcp.tool()
def appstore_analytics_list_instances(report_id: str, granularity: str = "DAILY", days: int = 30) -> dict[str, Any]:
    """
    List the available instances (date snapshots) for a given analytics report.
    granularity: DAILY | WEEKLY | MONTHLY.
    """
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=days)
    params = {
        "filter[granularity]": granularity,
        "filter[processingDate]": f"{start.isoformat()},{end.isoformat()}",
        "limit": 200,
    }
    r = _asc_get(f"/v1/analyticsReports/{report_id}/instances", params=params)
    return r.json()


@mcp.tool()
def appstore_analytics_download(instance_id: str) -> dict[str, Any]:
    """
    Download all segments for an analytics report instance and parse them as TSV.
    Returns a flat list of rows.
    """
    seg = _asc_get(f"/v1/analyticsReportInstances/{instance_id}/segments").json()
    rows: list[dict] = []
    for s in seg.get("data", []):
        url = s["attributes"]["url"]
        gz = httpx.get(url, timeout=120.0).content
        text = gzip.decompress(gz).decode("utf-8")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows.extend(list(reader))
    return {"instanceId": instance_id, "rowCount": len(rows), "rows": rows[:5000]}


@mcp.tool()
def list_apps() -> dict[str, Any]:
    """List apps visible to both credential sets — sanity check after setup."""
    out: dict[str, Any] = {"play": [], "appStore": []}
    try:
        rep = _play_reporting_client()
        apps = rep.apps().search().execute().get("apps", [])
        out["play"] = [{"name": a.get("displayName"), "package": a.get("name")} for a in apps]
    except Exception as e:
        out["play"] = {"error": str(e)}
    try:
        r = _asc_get("/v1/apps", params={"limit": 50})
        out["appStore"] = [
            {"id": a["id"], "name": a["attributes"].get("name"), "bundleId": a["attributes"].get("bundleId")}
            for a in r.json().get("data", [])
        ]
    except Exception as e:
        out["appStore"] = {"error": str(e)}
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
