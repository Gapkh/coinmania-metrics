# Current state — captured 2026-04-28

## Confirmed working ✅

- **App Store Connect API key (Admin role)**: `claude-app-metrics-admin`
  - Key ID: `DQPB76VDR5`
  - Issuer ID: `d0f87f8d-d981-4e96-afe8-93430512e652`
  - .p8: `AuthKey_DQPB76VDR5.p8` (in user's Downloads folder + uploaded to chat)
- **Apple analytics report request**: created with ID `a9036308-36f3-4216-a248-6e1f8e2188e6`
  - Access type: ONGOING (Apple will keep producing reports daily once started)
  - **Reports will be available 24–48 hours from 2026-04-28** — first usable data ~2026-04-29 to 2026-04-30.

## Confirmed via curl (no MCP server, no Python needed for tests)

- `GET /v1/apps` → 1 app: Coinmania App, id 6740985837, com.coinmania.app
- `GET /v1/apps/6740985837/customerReviews` → 6 total, all 5★, Georgian-language. Sample includes "Nina" review by Kira.
- `POST /v1/analyticsReportRequests` → 201, request id above.

## Old, retired key

- Developer-role key `claude-app-metrics` (Key ID `FRA3AGWC39`) → 403s on Sales + Analytics. Should be revoked once we confirm DQPB76VDR5 covers everything.

## Outstanding

- Local Python / MCP-server install on Windows hasn't worked cleanly in this session. The "Python" install at `AppData\Local\Python\bin\python.exe` runs silently with no output.
- The artifact `coinmania-ios-metrics` will show "MCP not connected — demo data" until either we get Python working or we wire up an alternate refresh path.
- Play Console (Android) still blocked on Account Owner `mobiledevelopers@coinmania.ge` (eto) — see HANDOFF-FOR-ADMIN.md.

## Easiest path forward (no Python)

While we wait for Apple's analytics to populate (24–48h), we can refresh the dashboard by running short curl-based PowerShell snippets that save response JSON to this folder. Claude reads the JSON and rebuilds the artifact with the real numbers. No Python install required.

Tomorrow's first task: a refresh script that
1. Generates a fresh JWT (Claude does this in chat, valid 19 min),
2. Calls `GET /v1/analyticsReportRequests/a9036308-36f3-4216-a248-6e1f8e2188e6/reports` to list the report families that came online,
3. Downloads the latest instances of "App Store Installation and Deletion Standard", "App Sessions Standard", "App Crashes Standard",
4. Saves each parsed result to a JSON file in this folder,
5. Claude re-renders the artifact with real downloads / sessions / crashes / active-devices.
