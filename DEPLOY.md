# How to deploy the TV dashboard — step by step

No coding knowledge required. This guide takes ~20 minutes.

The dashboard will live at a public URL like `https://coinmania-metrics.up.railway.app`.
Open that URL on any TV, phone, or laptop — it updates automatically every 6 hours.

---

## What you need

- A **GitHub** account (free) — github.com
- A **Railway** account (free) — railway.app
- The files in this folder on your computer
- Your `AuthKey_DQPB76VDR5.p8` file (already downloaded)

---

## Step 1 — Put this folder on GitHub

1. Go to **github.com** and sign in (or create a free account).
2. Click the green **"New"** button (top-left) to create a new repository.
3. Name it `coinmania-metrics`. Leave everything else as default. Click **"Create repository"**.
4. On the next page, GitHub shows upload instructions. Click **"uploading an existing file"** link.
5. Drag and drop **all the files from this folder** into the upload area:
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - `server.py` (keep it, it's fine)
   - Any other files here
   - **Do NOT upload** `.env`, `*.p8`, `*.json` (service account) — these are secrets
6. Click **"Commit changes"** (green button at the bottom).

Your code is now on GitHub. ✅

---

## Step 2 — Deploy on Railway

1. Go to **railway.app** and sign in with your GitHub account.
2. Click **"New Project"** → **"Deploy from GitHub repo"**.
3. Select the `coinmania-metrics` repo you just created.
4. Railway will detect the `Procfile` and start building. Wait ~2 minutes.
5. Once it shows **"Active"**, click on the service name to open its settings.

---

## Step 3 — Set your secret credentials

In Railway, click on your service → **"Variables"** tab → **"New Variable"** for each line below.

| Variable name | Value |
|---|---|
| `ASC_KEY_ID` | `DQPB76VDR5` |
| `ASC_ISSUER_ID` | `d0f87f8d-d981-4e96-afe8-93430512e652` |
| `ASC_VENDOR_NUMBER` | `89476434` |
| `ASC_APP_ID` | `6740985837` |
| `ASC_PRIVATE_KEY` | *(see below)* |
| `REFRESH_HOURS` | `6` |

### How to paste the private key (ASC_PRIVATE_KEY)

1. Find the file `AuthKey_DQPB76VDR5.p8` on your computer (it's in your Downloads folder).
2. Right-click → **"Open with" → Notepad** (Windows) or **TextEdit** (Mac).
3. You'll see text that looks like this:
   ```
   -----BEGIN PRIVATE KEY-----
   MIGHAgEAMBMGByq...several lines of letters...
   -----END PRIVATE KEY-----
   ```
4. Press **Ctrl+A** (select all), then **Ctrl+C** (copy).
5. In Railway, create a new variable named `ASC_PRIVATE_KEY` and paste the entire copied text as the value.
6. Click **"Add"**.

After adding all variables, Railway will automatically redeploy (takes ~1 minute).

---

## Step 4 — Get your public URL

1. In Railway, click on your service → **"Settings"** tab → **"Networking"** section.
2. Click **"Generate Domain"**.
3. You'll get a URL like `https://coinmania-metrics-production.up.railway.app`.
4. Open that URL in your browser — you should see the dashboard with live data! ✅

---

## Step 5 — Put it on the TV

1. On the smart TV, open the **browser app** (usually called "Browser", "Internet", or "Chrome").
2. Navigate to your Railway URL from Step 4.
3. **Bookmark it** or set it as the browser's homepage so you don't have to type it again.
4. For a full-screen experience: press **F11** (if using a keyboard) or look for "Full screen" in the browser menu.

The dashboard will:
- **Automatically refresh data** every 6 hours without you doing anything.
- **Show the latest snapshot** even if it was last refreshed hours ago.
- **Work from any device** — TV, phone, tablet, laptop — as long as it has internet.

---

## Troubleshooting

**"Error: 401 Unauthorized"** — The key ID, Issuer ID, or private key content is wrong.
Double-check that `ASC_KEY_ID` matches the filename of your `.p8` (e.g. `DQPB76VDR5`),
and that you pasted the *entire* content of the file including the `-----BEGIN...` lines.

**"Report not yet available for this date"** — This is normal. Apple publishes sales reports
~24 hours late. The number will appear the next day. Everything else (reviews, ratings) works immediately.

**Dashboard shows old data** — Railway's free tier keeps the server running continuously.
If data looks stale, you can force a refresh by opening:
`https://your-railway-url.up.railway.app/refresh?secret=` (POST request — easiest via browser extension
or just wait for the next auto-refresh).

**TV browser doesn't load the page** — Try a different browser on the TV, or open the URL
on your phone first to confirm it works.

---

## Railway free tier limits

Railway's free tier gives you $5 of credit/month. This server uses roughly $0.50–1.00/month
(it's very lightweight). Well within the free tier. No credit card needed initially.

---

## Updating the dashboard later

If you want to change how the dashboard looks or what data it shows:
1. Edit the files on your computer.
2. Go to your GitHub repo → find the file → click the pencil icon → paste the new version → "Commit".
3. Railway automatically redeploys within 1–2 minutes.
