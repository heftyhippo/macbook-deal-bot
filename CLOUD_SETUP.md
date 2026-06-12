# Running the bot in the cloud — free, 24/7, not on your computer

This sets the bot up to scan **automatically on GitHub's servers** every 20
minutes and message your Telegram when a 35%+ deal appears — **even when your
laptop is off**. It's free, needs no credit card, and no server administration.

You'll still be able to run `python macdeals.py scan` on your own machine
whenever you like; this just adds an always-on copy in the cloud.

**Time needed:** about 20 minutes, once.

---

## How it works (the short version)

GitHub offers free "Actions" — little jobs that run on their computers on a
schedule. We hand GitHub the bot's code and a timetable ("scan every 20 min").
Each run starts a fresh machine, installs the bot, runs one scan, sends any
alerts to your Telegram, remembers what it already alerted (so you don't get
duplicates), and shuts down. Your Telegram token is stored in GitHub's
encrypted "Secrets" — never written into the code.

---

## ⚠️ One important safety rule

**Never put your Telegram bot token into a public place.** In the steps below,
the code you upload must contain the *placeholder* `config.yaml` (the one with
`PASTE_YOUR_..._HERE` still in it), **not** the version where you typed your real
token. Your real token goes only into GitHub **Secrets** (Step 4). If you ever
paste a token somewhere public by accident, message @BotFather and send
`/revoke` to get a new one.

---

## Step 1 — Make a free GitHub account

Go to https://github.com and sign up. It's free. Verify your email.

## Step 2 — Create a repository ("repo" = a folder for your project)

1. Click the **+** in the top-right of GitHub → **New repository**.
2. Name it anything, e.g. `macbook-deal-bot`.
3. Choose **Public**. (Public repos get *unlimited* free Actions minutes. The
   only thing visible to others is the bot's code and the list of MacBook
   prices it finds — nothing personal. Your token stays in Secrets, which are
   private even in a public repo.)
4. Leave everything else as-is and click **Create repository**.

## Step 3 — Upload the bot's files

You're going to upload the contents of the bot folder. The tricky part is one
hidden folder called `.github` — Mac Finder hides folders that start with a dot,
so we'll create that one by hand.

**3a. Upload the normal files:**

1. On your new repo's page, click **Add file → Upload files**.
2. Open the bot folder on your Mac, select all the visible files
   (`macdeals.py`, `config.yaml`, `pricing.py`, `sources.py`, `store.py`,
   `report.py`, `requirements.txt`, `README.md`, `CLOUD_SETUP.md`, `.gitignore`),
   and drag them onto the GitHub page.
   - **Make sure `config.yaml` here is the placeholder version** (no real token).
     If yours has your token in it, open it first and replace the token/chat_id
     lines with `PASTE_YOUR_BOT_TOKEN_HERE` / `PASTE_YOUR_CHAT_ID_HERE` before
     uploading.
3. Click **Commit changes**.

**3b. Create the workflow file by hand:**

1. Click **Add file → Create new file**.
2. In the filename box at the top, type exactly:
   ```
   .github/workflows/scan.yml
   ```
   (As you type the slashes, GitHub turns it into folders automatically.)
3. Open `scan.yml` from the `.github/workflows` folder in your bot directory
   in TextEdit, copy everything, and paste it into the big editing box on GitHub.
   - Can't see the `.github` folder on your Mac? In Finder press
     **Cmd + Shift + . (period)** to reveal hidden files, or just copy the
     `scan.yml` contents from the file Claude gave you.
4. Click **Commit changes**.

## Step 4 — Add your Telegram secrets

1. In your repo, click **Settings** (top menu) → in the left sidebar,
   **Secrets and variables** → **Actions**.
2. Click **New repository secret**.
   - Name: `TELEGRAM_BOT_TOKEN`  → Secret: paste your BotFather token →
     **Add secret**.
3. Click **New repository secret** again.
   - Name: `TELEGRAM_CHAT_ID`  → Secret: paste your numeric chat id →
     **Add secret**.

That's your token stored safely — encrypted, never shown again, not in the code.

## Step 5 — Turn Actions on and do a test run

1. Click the **Actions** tab. If GitHub asks, click the green button to
   **enable workflows** for this repo.
2. In the left sidebar click **MacBook deal scan**.
3. Click **Run workflow → Run workflow** (this triggers one immediately instead
   of waiting for the schedule).
4. After ~1–3 minutes a run appears. Click it, then click the **scan** job to
   watch the live log. You'll see the same output as on your computer
   (`mercari: NNN raw listings`, the deals table, `telegram alerts sent: N`).
   If there's a 35%+ deal, your phone buzzes.

From now on it runs by itself every 20 minutes. **You can close everything —
your laptop can be off.**

---

## Everyday questions

**Change how often it scans:** edit `.github/workflows/scan.yml` (click the file
on GitHub → pencil icon), and change the `cron` line. `"*/30 * * * *"` = every
30 min, `"0 * * * *"` = hourly. Commit the change.

**Stop it:** Actions tab → MacBook deal scan → the **•••** menu →
**Disable workflow**. Re-enable the same way.

**See what it's doing / did it find anything:** Actions tab → click any run →
**scan** job → read the log. Each run also updates a `last_run.txt` file in your
repo with a timestamp, so you can see at a glance it's alive.

**Stop duplicate alerts working?** The bot commits a file called `seen_items.db`
after each run to remember what it already told you. You'll see these little
"Update dedupe DB" commits in your repo — that's normal and expected.

**Adjust models, prices, thresholds:** edit `config.yaml` on GitHub the same way
(click it → pencil → edit → commit). The next scheduled run uses your changes.
(Your local copy and the cloud copy are separate — change both if you want them
to match.)

---

## Important caveat: will Buyee work from GitHub's servers?

This is the one thing I can't guarantee, and you should know about it up front.

GitHub's computers live in a data centre. Some sites — **Buyee in particular**
(which the bot uses for Yahoo Auctions *and* Rakuma) — are stricter toward data
centre addresses than toward a normal home broadband address like yours. So it's
possible that:

- **Mercari keeps working in the cloud** (it uses an app-style data feed and is
  usually fine from anywhere) — and Mercari is the **bulk** of the listings, so
  you'd still get the large majority of deals as Telegram alerts; **but**
- **Buyee (Yahoo + Rakuma) may get blocked** when run from GitHub. If so, those
  two sources will just log a failure in the run log and be skipped — the scan
  still completes and still alerts you to Mercari deals.

**The test run in Step 5 will tell you which sources work from the cloud** — read
the log. Three outcomes:

1. **All three work** → perfect, nothing to do.
2. **Only Mercari works** → totally fine for most purposes. To silence the
   Yahoo/Rakuma failure messages, add a repository *Variable* (Settings →
   Secrets and variables → Actions → **Variables** tab → New variable) named
   `SOURCES` with value `mercari`. The cloud bot will then scan Mercari only,
   while your **local** bot still scans all three.
3. **You want Yahoo/Rakuma in the cloud too, and they're blocked** → the robust
   answer is a small always-free virtual machine instead of GitHub Actions
   (Oracle Cloud's Always-Free tier can run the full `watch` loop 24/7, browser
   and all). It's more setup and needs a card for identity check. If you reach
   this point, tell Claude and you'll get a step-by-step for that route.

Either way: **run it, read the first log, and we'll know.** Nothing here can
break your local bot, which keeps scanning all sources from your home IP.
