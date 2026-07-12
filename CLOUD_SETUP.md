# Running the bot in the cloud — free, 24/7, not on your computer

This sets the bot up to scan **automatically on GitHub's servers** every 20
minutes, **WhatsApp you** when a qualifying deal appears (35%+ below the
condition/spec-aware expected UK price, with landed costs included), and
publish the **dashboard as a website** you can
open from any device — phone, work computer, anywhere:

```
https://<your-username>.github.io/<your-repo-name>/
```

It's free, needs no credit card, and no server administration. Your laptop
can be off. You'll still be able to run `python macdeals.py scan` locally
whenever you like (and local scans are the only way to cover Swappa — see
the caveats at the bottom).

**Time needed:** about 20 minutes, once.

---

## How it works (the short version)

GitHub offers free "Actions" — little jobs that run on their computers on a
schedule. We hand GitHub the bot's code and a timetable ("scan every 20 min").
Each run starts a fresh machine, installs the bot, runs one scan, sends any
WhatsApp alerts, updates the dashboard website, and shuts down. The bot's
memory of what it already alerted lives in GitHub's build cache, so you don't
get duplicate alerts between runs. Your WhatsApp API key is stored in
GitHub's encrypted "Secrets" — never written into the code.

---

## ⚠️ One important safety rule

**Never put your CallMeBot API key or phone number into the code you
upload.** The `config.yaml` you upload must be the *placeholder* version
(with `PASTE-YOUR-...-HERE` still in it). Your real phone + apikey go only
into GitHub **Secrets** (Step 4). The repo is public — anyone can see the
code, but Secrets are private even in a public repo.

---

## Step 0 — WhatsApp alerts (if you haven't already)

Follow README Step C: add **+34 644 53 78 49** to your phone's contacts,
WhatsApp it the message `I allow callmebot to send me messages`, and note
the **apikey** it replies with. (Check callmebot.com if the number has
changed.) That apikey plus your own WhatsApp number (with country code,
e.g. `+447712345678`) are what you'll paste into GitHub Secrets below.

## Step 1 — Make a free GitHub account

Go to https://github.com and sign up. It's free. Verify your email.

## Step 2 — Create a repository ("repo" = a folder for your project)

1. Click the **+** in the top-right of GitHub → **New repository**.
2. Name it anything, e.g. `macbook-deal-bot`.
3. Choose **Public**. (Public repos get *unlimited* free Actions minutes and
   free GitHub Pages. The only things visible to others are the bot's code
   and the dashboard of Apple deals — nothing personal. Your WhatsApp
   key stays in Secrets.)
4. Leave everything else as-is and click **Create repository**.

## Step 3 — Upload the bot's files

1. On your new repo's page, click **Add file → Upload files**.
2. Open the bot folder on your Mac, select the visible files
   (`macdeals.py`, `config.yaml`, `pricing.py`, `sources.py`, `store.py`,
   `report.py`, `requirements.txt`, `README.md`, `CLOUD_SETUP.md`,
   `.gitignore`), and drag them onto the GitHub page.
   - **Make sure `config.yaml` is the placeholder version** (no real apikey).
3. Click **Commit changes**.
4. Now the workflow file (Mac Finder hides the `.github` folder — press
   **Cmd + Shift + .** in Finder to reveal it, or just do this):
   click **Add file → Create new file**, and in the filename box type exactly
   ```
   .github/workflows/scan.yml
   ```
   (GitHub turns the slashes into folders automatically.) Open
   `.github/workflows/scan.yml` from the bot folder in TextEdit, copy
   everything, paste it into the big editor box, and **Commit changes**.

## Step 4 — Add your WhatsApp secrets

1. In your repo, click **Settings** (top menu) → in the left sidebar,
   **Secrets and variables** → **Actions**.
2. Click **New repository secret**.
   - Name: `WHATSAPP_PHONE` → Secret: your WhatsApp number with country
     code, e.g. `+447712345678` → **Add secret**.
3. Click **New repository secret** again.
   - Name: `WHATSAPP_APIKEY` → Secret: the apikey CallMeBot sent you →
     **Add secret**.

## Step 5 — Turn on the dashboard website (GitHub Pages)

1. Still in **Settings**, click **Pages** in the left sidebar.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.

That's the whole step. After the first scan runs, your dashboard will be at
`https://<your-username>.github.io/<your-repo-name>/` — **bookmark it on
your phone** (in Safari: Share → Add to Home Screen, and it behaves like an
app). It shows the freshest scan and auto-refreshes itself every 10 minutes
while open.

## Step 6 — Turn Actions on and do a test run

1. Click the **Actions** tab. If GitHub asks, click the green button to
   **enable workflows** for this repo.
2. In the left sidebar click **Apple deal scan**.
3. Click **Run workflow → Run workflow** (this triggers one immediately
   instead of waiting for the schedule).
4. After ~2–4 minutes the run finishes. Click it → click the **scan** job to
   read the log: you'll see the same output as on your computer
   (`mercari: NNN raw listings`, the deals table, `whatsapp alerts sent: N`).
5. Open your dashboard URL — the deals are there.

From now on it runs by itself every 20 minutes. **You can close everything —
your laptop can be off.**

---

## Everyday questions

**Change how often it scans:** edit `.github/workflows/scan.yml` on GitHub
(click the file → pencil icon) and change the `cron` line.
`"*/30 * * * *"` = every 30 min, `"0 * * * *"` = hourly.

**Stop it:** Actions tab → Apple deal scan → the **•••** menu →
**Disable workflow**. Re-enable the same way.

**Adjust models, prices, thresholds:** edit `config.yaml` on GitHub the same
way (pencil → edit → commit). The next run uses your changes. (Your local
copy and the cloud copy are separate — change both if you want them to match.)

**Why didn't I get an alert twice for the same deal?** That's the dedupe
memory in the Actions cache working. It also means: if you run the local
watcher AND the cloud scan at the same time, each has its own memory, so a
great deal may buzz you twice (once from each). Fine as redundancy — or keep
the local watcher for Swappa-only scans.

**Does the dashboard update while I look at it?** It reloads itself every
10 minutes. Each scan replaces the page with fresh data.

---

## Important caveat: will Buyee and eBay work from GitHub's servers?

GitHub's computers live in a data centre, and some sites are stricter toward
data-centre addresses than toward home broadband:

- **Mercari usually works in the cloud** (app-style data feed) — and it's
  the bulk of the JP listings. The three eBay sites (UK/US/DE) usually work
  too.
- **Buyee (Yahoo + Rakuma + PayPay) may get blocked** from GitHub's address
  ranges. If so, those sources just log a failure and are skipped — the
  scan still completes and still alerts on whatever works.
- **Swappa and Gumtree don't run in the cloud** — Swappa needs a real Chrome
  on a home connection, and Gumtree rate-limits datacenter addresses — so the
  workflow doesn't try them. Your local scans still cover both.

**The test run in Step 6 tells you which sources work — read the log.** To
silence sources that always fail in the cloud, add a repository *Variable*
(Settings → Secrets and variables → Actions → **Variables** tab → New
variable) named `SOURCES` listing only the working ones, e.g.
`mercari,ebay_uk,ebay_us,ebay_de`. Your **local** bot still scans everything.

If you ever want the blocked sources in the cloud too, the fallback is a
small always-free VM (Oracle Cloud's Always-Free tier) running the full
`watch` loop — more setup, needs a card for identity check, and a
data-centre IP may *still* be blocked by Buyee. Ask Claude for a
step-by-step if you reach that point.
