# Final steps — connect the dashboard to live data

Two things left, both copy-paste:

1. One PowerShell block to install remaining deps, write `.env` correctly, and refresh the project copy in your home folder.
2. One JSON snippet for Cowork's MCP settings.

After that, restart Cowork once and the artifact goes live.

---

## Step 1 — finish the local install (PowerShell)

Paste this whole block into the PowerShell window you've been using and press Enter:

```powershell
$src = "C:\Users\Giorgi Apkhazava\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\local-agent-mode-sessions\ff79054e-f7da-44d8-8068-667d53e7ced2\1199e131-cb1c-48ad-8f2f-f6886cd85092\local_632d06c3-199c-4b7a-83a1-c5be5534799f\outputs\app-metrics-mcp"
$dst = "$env:USERPROFILE\app-metrics-mcp"
New-Item -ItemType Directory -Force $dst | Out-Null

# Refresh project files (server.py, requirements.txt, etc.)
Copy-Item -Recurse -Force "$src\*" $dst

# Write a working .env (iOS-only for now; Android joins later when eto unblocks)
@"
# App Store Connect
ASC_KEY_ID=FRA3AGWC39
ASC_ISSUER_ID=d0f87f8d-d981-4e96-afe8-93430512e652
ASC_VENDOR_NUMBER=89476434
ASC_APP_ID=6740985837
ASC_PRIVATE_KEY_PATH=$env:USERPROFILE\Downloads\AuthKey_FRA3AGWC39.p8

# Google Play (left blank intentionally — will be filled in when Play access is granted)
PLAY_PACKAGE_NAME=com.coinmania.app
GOOGLE_SA_JSON=
PLAY_GCS_BUCKET=
"@ | Out-File -FilePath "$dst\.env" -Encoding utf8

# Install all server deps (some you've already got; --quiet so it stays tidy)
python -m pip install --user --quiet -r "$dst\requirements.txt"

# Sanity check: can the server module load?
$env:PYTHONIOENCODING = "utf-8"
python -c "import sys; sys.path.insert(0, r'$dst'); import server; print('server.py loads OK')"

Write-Host ""
Write-Host "Setup complete. Server path:" -ForegroundColor Green
Write-Host "  $dst\server.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "Next: paste the JSON snippet from FINAL-STEPS.md step 2 into Cowork's MCP settings, then restart Cowork."
```

When this finishes, you should see `server.py loads OK` and the resolved server path. If you see any red errors, paste them back here.

---

## Step 2 — register the server with Cowork

Open Cowork → Settings → MCP servers (the exact path depends on your Cowork version; usually under "Connectors" or "Developer"). Add a new MCP server with this configuration:

```json
{
  "name": "app-metrics",
  "command": "python",
  "args": [
    "C:\\Users\\Giorgi Apkhazava\\app-metrics-mcp\\server.py"
  ],
  "env": {
    "PYTHONIOENCODING": "utf-8"
  }
}
```

Save. Then restart Cowork (full quit + relaunch — not just close the window).

---

## Step 3 — open the dashboard

Open the artifact named **coinmania-ios-metrics** from your Cowork sidebar. You should see:

- Status banner turns yellow with text like *"Connected. Reviews live; downloads and active-device data pending Apple analytics (24–48h after first request)."*
- App info card filled with your real Coinmania App data
- KPI cards: real average rating + recent review count from Apple
- Reviews panel with the latest reviews
- Daily downloads chart shows a placeholder ("Apple is generating reports for the first time")

The artifact also automatically fires off the App Store Connect Analytics Report request the first time it loads. From that moment, Apple has 24–48h to start producing data. After that, the chart and the two "pending" KPIs fill in on the next refresh.

If the banner stays red ("MCP server not connected"), Cowork couldn't start the server — most common causes:
- Python path issue → in PowerShell, run `(Get-Command python).Source` and copy that absolute path into the `command` field of the JSON above.
- `.env` wasn't written → re-run Step 1.
- Cowork wasn't fully restarted (just closing the window doesn't kill the MCP host).

---

## When eto grants Play Console access

Edit your `.env` and fill in:
- `GOOGLE_SA_JSON` = full path to `coinmania-app-metrics-df3bd2856256.json`
- `PLAY_GCS_BUCKET` = the `pubsite_prod_rev_*` bucket name eto sends you

Restart Cowork and the Android-side panels light up automatically (separate Android dashboard artifact will be added then).
