# Hand-off — for the Coinmania LLC App Store Connect / Play Console Admin

Hi Mamuka — Giorgi is setting up read-only access to Coinmania's Play Console
and App Store Connect metrics so we can build a live internal dashboard. The
two things below take ~10 minutes total and require Admin-level access on
each console (which Giorgi doesn't have, so we're asking you).

Everything below is **read-only** and **revocable in one click** if anything
ever feels off.

---

## What's already done (no action needed from you)

- A new isolated Google Cloud project `coinmania-app-metrics` exists under
  the `coinmania.ge` organization.
- Three read-only APIs are enabled in that project: Google Play Developer
  Reporting, Google Play Android Developer, Cloud Storage.
- A service account `claude-app-metrics@coinmania-app-metrics.iam.gserviceaccount.com`
  has been created with **no project-level roles** — it can do nothing
  outside of access we explicitly grant it on Coinmania resources.
- A JSON private key for that service account has been generated and is
  stored locally on Giorgi's machine.

---

## What we need you to do

### 1. Play Console — grant the service account read-only access (≈3 min)

- Open Play Console with your admin account: https://play.google.com/console/
- In the left sidebar (or under your account icon), open **Settings → API access**.
  You should see this page; Giorgi doesn't.
- Under "Linked Google Cloud projects", click **Link existing project** and
  pick `coinmania-app-metrics`. (One-time link.)
- Scroll down to the "Service accounts" section — you should now see
  `claude-app-metrics@coinmania-app-metrics.iam.gserviceaccount.com`.
  Click **Grant access** next to it.
- Permissions to grant:
  - "View app information and download bulk reports (read-only)" ✅
  - "View financial data, orders, and cancellation survey responses" ✅
  - Apply to **the Coinmania app** (`com.coinmania.app`).
- Click **Invite user / Save** at the bottom.

That's it for the Play Console API access. The dashboard can then read
crash rate, ANR rate, and reviews.

### 2. (Optional but recommended) Cloud Storage — install/uninstall reports

This step lets the dashboard show daily downloads/uninstalls/active devices.
If skipped, the dashboard still works but won't have those three metrics.

- In Play Console, open **Download reports → Statistics**.
- At the bottom of that page is a Cloud Storage URI like
  `gs://pubsite_prod_rev_XXXXXXXXXXXXX/`. **Copy that URI** and send it to
  Giorgi.
- (Either Mamuka or Giorgi, with bucket-IAM permission can do this part:)
  In Google Cloud Console → Cloud Storage → that bucket → **Permissions →
  Grant access**, paste
  `claude-app-metrics@coinmania-app-metrics.iam.gserviceaccount.com`,
  role **Storage Object Viewer**.

### 3. App Store Connect — create a Team API key (≈3 min) — **time-sensitive**

> *Why time-sensitive:* Apple's async Analytics Reports (which power most of
> the iOS panels — sources, conversion funnel, sessions, per-version
> crashes) only **start producing data 24–48 hours after the first API call
> from a key**. The clock doesn't start until the key exists and we've made
> one initial `analyticsReportRequest`. So the sooner this step happens, the
> sooner the dashboard has full data.

- Open https://appstoreconnect.apple.com/access/integrations/api with your
  admin account. (Giorgi gets redirected away from this URL; you should see
  it.)
- Tab "**Team Keys**" (not "Individual Keys").
- Click **+** to create a new key:
  - Name: `claude-app-metrics`
  - Access: **Developer** (broader than "Sales and Reports" — covers reviews +
    analytics; still no write access to apps/builds)
- After creation, click **Download API Key** — this saves an
  `AuthKey_XXXXXXXXXX.p8` file. Apple **only lets you download this once**,
  so:
  - Save it somewhere safe (and send a copy to Giorgi over a secure channel —
    this is the equivalent of a password).
- From the same page, also send Giorgi:
  - **Key ID** (10-character string)
  - **Issuer ID** (UUID, shown at the top of the Keys page)

### 4. Vendor Number (≈30 sec)

While you're in App Store Connect, also send Giorgi the **Vendor Number**
(8–9 digit number, found on the main account page or the Sales and Trends
page). Needed for the Sales Reports endpoint.

---

## What you do **not** need to do

- No password sharing — everything above is via API keys you generate.
- No billing/payment changes.
- No team or app changes.
- No write access of any kind. The Apple "Developer" role and the Play
  Console "View app information" role both **cannot modify any app, build,
  listing, price, or user**.

## How to revoke any of this later

- Play Console: **Settings → API access** → revoke service account access.
- Google Cloud: IAM → delete the `claude-app-metrics` service account.
- App Store Connect: **Users and Access → Integrations → Team Keys** → click
  the key → **Revoke**. Effective immediately.

Thanks!
