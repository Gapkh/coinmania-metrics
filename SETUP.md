# Credential Setup — Google Play Console & App Store Connect

This is the only "manual" part of the project. You'll generate read-only API credentials in each console and drop them into a local `.env` file. Once done, the MCP server reads them and Claude can pull live metrics.

**Security model in one paragraph:** every credential below is a *scoped, revocable API key* you create yourself. We never use your console password. Keys live in a `.env` file on your machine, are read by the local MCP server, and are never sent to Claude or any artifact. If anything ever feels off, both consoles let you revoke a key in one click.

---

## Part A — Google Play Console

You'll need three things: a Google Cloud project, a service account with the right APIs enabled, and a Cloud Storage export configured for installs/uninstalls.

### A.1 — Create a Google Cloud project (if you don't have one)

1. Go to https://console.cloud.google.com/
2. Top bar → project dropdown → "New Project". Name it something like `app-metrics-claude`.
3. Note the **Project ID** (looks like `app-metrics-claude-123456`).

### A.2 — Enable the APIs

In the project, go to "APIs & Services" → "Library" and enable:

- **Google Play Developer Reporting API** — vitals (crashes, ANR, slow start, slow rendering)
- **Google Play Android Developer API** — reviews + ratings
- **Cloud Storage API** — to read the install/uninstall CSV exports

### A.3 — Create the service account

1. "APIs & Services" → "Credentials" → "Create credentials" → "Service account".
2. Name it `claude-app-metrics`. Skip the optional grants.
3. After creation, click into it → "Keys" tab → "Add key" → "Create new key" → JSON. A `.json` file downloads. **Keep this file.**

### A.4 — Grant the service account access in Play Console

1. Go to https://play.google.com/console/ → Settings (gear icon) → "API access".
2. Find your linked Google Cloud project. If not linked, link it.
3. Under "Service accounts", find `claude-app-metrics@...iam.gserviceaccount.com` → "Grant access".
4. Permissions:
   - "View app information and download bulk reports (read-only)" ✅
   - "View financial data" ✅ (needed for some metric sets)
   - Apply to your specific app(s).
5. Save.

### A.5 — Configure Cloud Storage export (for installs / uninstalls / active users)

The Play Reporting API doesn't expose install counts directly — those come via daily CSV exports.

1. In Play Console, find the **Cloud Storage URI** for reports: it's shown in Play Console → "Download reports" → "Statistics" → bottom of the page (e.g. `gs://pubsite_prod_rev_01234567890/`). Copy this URI.
2. Grant your service account `Storage Object Viewer` on that bucket (Cloud Console → Cloud Storage → your bucket → Permissions → Grant access → paste the service account email → role: "Storage Object Viewer").

### A.6 — Note your package name

The app ID like `com.yourcompany.yourapp`. You'll find it in Play Console → your app → URL or app dashboard.

**Outputs of Part A:**
- `service-account.json` (downloaded file)
- Play package name (e.g. `com.yourcompany.yourapp`)
- Cloud Storage bucket URI (e.g. `gs://pubsite_prod_rev_01234567890/`)

---

## Part B — App Store Connect

You'll generate a `.p8` private key + Issuer ID + Key ID. The MCP server signs JWTs with these to call the App Store Connect API.

### B.1 — Create an API key

1. Go to https://appstoreconnect.apple.com/access/integrations/api
2. Tab "Team Keys" (not "Individual Keys").
3. Click "+" → name it `claude-app-metrics` → access role: **Developer** (or **Sales and Reports** if you only want downloads). Developer is broader and covers reviews + analytics.
4. After creation, click "Download API Key" — you get a `.p8` file. **You can only download this once.** Save it somewhere safe.
5. From the same page, copy:
   - **Key ID** (10-char string like `ABC123XYZ4`)
   - **Issuer ID** (UUID at top of page, like `12a3b456-7890-1234-5678-...`)

### B.2 — Find your app's numeric ID

1. https://appstoreconnect.apple.com/apps → click your app.
2. Look at the URL: `.../apps/<APP_ID>/...` — that number is your `ASC_APP_ID`.

### B.3 — Find your Vendor Number

1. https://appstoreconnect.apple.com/access/users → "Sales and Reports" or main account page.
2. Vendor # is shown near the top (8-9 digit number).

**Outputs of Part B:**
- `AuthKey_ABC123XYZ4.p8` file
- Key ID
- Issuer ID
- App ID (numeric)
- Vendor Number

---

## Part C — Drop everything into `.env`

Once you have all the above, copy `.env.example` → `.env` in the project folder and fill in:

```
# Google Play
GOOGLE_SA_JSON=/absolute/path/to/service-account.json
PLAY_PACKAGE_NAME=com.yourcompany.yourapp
PLAY_GCS_BUCKET=pubsite_prod_rev_01234567890

# App Store Connect
ASC_KEY_ID=ABC123XYZ4
ASC_ISSUER_ID=12a3b456-7890-1234-5678-1234567890ab
ASC_PRIVATE_KEY_PATH=/absolute/path/to/AuthKey_ABC123XYZ4.p8
ASC_VENDOR_NUMBER=12345678
ASC_APP_ID=1234567890
```

Then continue to `README.md` for run instructions.

---

## Revoking access (if ever needed)

- **Google Play**: Cloud Console → IAM → delete the service account, OR Play Console → API access → revoke.
- **App Store Connect**: API Keys page → click your key → Revoke. Effective immediately.
