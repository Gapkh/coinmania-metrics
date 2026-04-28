"""
iOS smoke test — verify App Store Connect credentials work for our use case.

Runs four checks:
  1. JWT auth works at all (GET /v1/apps)
  2. Customer reviews endpoint returns data for the configured app
  3. Sales report endpoint returns data for yesterday
  4. Analytics report request kicks off (asynchronously, returns request id)

Usage:
  python test_apple.py [path/to/AuthKey_XXXX.p8]

Defaults the .p8 path to ./AuthKey_FRA3AGWC39.p8 if no arg given.
"""
import sys
import os
import json
import time
import gzip
import io
import csv
import datetime as dt
from pathlib import Path

import httpx
import jwt

KEY_ID     = "DQPB76VDR5"
ISSUER_ID  = "d0f87f8d-d981-4e96-afe8-93430512e652"
APP_ID     = "6740985837"
VENDOR_NUM = "89476434"
ASC_BASE   = "https://api.appstoreconnect.apple.com"


def make_jwt(p8_path: str) -> str:
    private_key = Path(p8_path).read_text()
    now = int(time.time())
    headers = {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    payload = {
        "iss": ISSUER_ID,
        "iat": now,
        "exp": now + 60 * 19,
        "aud": "appstoreconnect-v1",
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def get(token: str, path: str, params=None, accept="application/json") -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}", "Accept": accept}
    r = httpx.get(f"{ASC_BASE}{path}", headers=headers, params=params, timeout=60.0)
    return r


def post(token: str, path: str, body: dict) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return httpx.post(f"{ASC_BASE}{path}", headers=headers, json=body, timeout=60.0)


def divider(label: str):
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


def main(p8_path: str):
    # Force UTF-8 stdout so emoji in reviews don't crash the script on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not Path(p8_path).exists():
        print(f"ERROR: .p8 file not found at {p8_path}")
        print("Move AuthKey_FRA3AGWC39.p8 into this folder, or pass its path as arg.")
        sys.exit(1)

    print(f"Using .p8: {p8_path}")
    token = make_jwt(p8_path)
    print(f"JWT generated (length {len(token)})")

    # ---------------- 1. List apps ----------------
    divider("1. JWT auth — list apps")
    r = get(token, "/v1/apps", params={"limit": 50})
    if r.status_code != 200:
        print(f"FAILED ({r.status_code}): {r.text[:500]}")
        sys.exit(1)
    apps = r.json().get("data", [])
    print(f"OK — {len(apps)} app(s) visible to this key:")
    for a in apps:
        attrs = a.get("attributes", {})
        print(f"   - {a['id']}  {attrs.get('name')}  bundle={attrs.get('bundleId')}")
    if not any(a["id"] == APP_ID for a in apps):
        print(f"WARNING: configured APP_ID {APP_ID} not in the list above.")

    # ---------------- 2. Customer reviews ----------------
    divider("2. Customer reviews — last 5")
    r = get(token, f"/v1/apps/{APP_ID}/customerReviews", params={"limit": 5, "sort": "-createdDate"})
    if r.status_code != 200:
        print(f"FAILED ({r.status_code}): {r.text[:500]}")
    else:
        data = r.json().get("data", [])
        print(f"OK — got {len(data)} reviews")
        for rv in data:
            a = rv["attributes"]
            print(f"   {a.get('rating')}* | {a.get('territory'):>3} | {a.get('createdDate', '')[:10]} | {a.get('title') or ''}")

    # ---------------- 3. Sales report (yesterday) ----------------
    divider("3. Sales report — yesterday DAILY")
    yesterday = (dt.datetime.utcnow().date() - dt.timedelta(days=1)).isoformat()
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": VENDOR_NUM,
        "filter[reportDate]": yesterday,
        "filter[version]": "1_1",
    }
    r = get(token, "/v1/salesReports", params=params, accept="application/a-gzip")
    if r.status_code == 200:
        try:
            tsv = gzip.decompress(r.content).decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
            ours = [row for row in rows if row.get("Apple Identifier") == APP_ID] or rows
            print(f"OK — {yesterday}: {len(rows)} total rows, {len(ours)} for our app")
            if ours:
                # Show a couple of the most useful columns from the first row
                row = ours[0]
                interesting = ["Apple Identifier", "Title", "Product Type Identifier",
                               "Country Code", "Units", "Customer Price", "Developer Proceeds"]
                print("   Sample row (first):")
                for k in interesting:
                    if k in row:
                        print(f"     {k}: {row[k]}")
        except Exception as e:
            print(f"got 200 but couldn't parse: {e}")
            print(r.content[:200])
    elif r.status_code == 404:
        print(f"OK — endpoint reachable but no report for {yesterday} (Apple usually publishes ~24h late)")
    else:
        print(f"FAILED ({r.status_code}): {r.text[:500]}")

    # ---------------- 4. Analytics report request ----------------
    divider("4. Analytics report request — kick off async flow (one-time)")
    body = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {"accessType": "ONGOING"},
            "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}},
        }
    }
    r = post(token, "/v1/analyticsReportRequests", body)
    if r.status_code in (201, 200):
        rid = r.json().get("data", {}).get("id")
        print(f"OK — request created, id={rid}")
        print("   Apple will start producing reports for this key within 24-48h.")
        print("   Save this ID — we'll use it to fetch reports from now on.")
    elif r.status_code == 409:
        print("OK — already exists (a previous request was created for this app+access type).")
        print("   Apple is already producing reports.")
    else:
        prin