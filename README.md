# MacBook Japan Deal Bot 🇯🇵💻

Scans **Mercari Japan**, **Yahoo! Auctions Japan**, and **Rakuten Rakuma** (the
marketplaces behind Buyee and ZenMarket) for **new / unused MacBook Pro listings** (M2 Pro and newer,
including opened-but-unused units with fewer than 10 battery cycles), estimates the
**full landed cost in GBP** (item + proxy fees + shipping + UK import VAT), compares
it against average UK prices for the same model, and tells you about the best deals —
in your terminal, in a clickable HTML report, and (optionally) as **Telegram alerts**
while it runs in the background.

> **How Yahoo Auctions is reached:** Yahoo! JAPAN has geo-blocked all visitors
> from the UK and EEA since April 2022, so the bot searches **Buyee's mirror**
> of Yahoo Auctions instead (Buyee exists precisely to give overseas buyers
> access). This also means the "Original" link for Yahoo items in `deals.html`
> won't open from a UK connection - use the Buyee or ZenMarket links for those.
> Mercari items' original links work fine.

For every deal it outputs direct purchase links via **Buyee** and **ZenMarket**, plus
the original Japanese listing.

---

## 1. One-time setup (about 15 minutes)

### Step A — Install Python

1. Go to https://www.python.org/downloads/ and download Python 3.12 (or newer).
2. Run the installer.
   - **Windows: tick the box that says "Add python.exe to PATH"** before clicking
     Install. This is the single most important checkbox.
   - Mac: just run the installer normally (or `brew install python` if you use Homebrew).
3. To check it worked, open a terminal:
   - **Windows:** press the Windows key, type `cmd`, press Enter.
   - **Mac:** open the **Terminal** app.

   Then type:
   ```
   python --version
   ```
   (on Mac you may need `python3 --version`). You should see something like
   `Python 3.12.x`. If Windows says "python is not recognized", re-run the installer
   and tick the PATH box.

### Step B — Install the bot's dependencies

1. Unzip `macbook-deal-bot.zip` somewhere easy, e.g. your Documents folder.
2. In your terminal, move into that folder:
   ```
   cd Documents\macbook-deal-bot        (Windows)
   cd ~/Documents/macbook-deal-bot      (Mac)
   ```
3. Install the libraries the bot needs (one-off):
   ```
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```
   (use `python3` instead of `python` on Mac if needed — that applies to every
   command in this guide.) The second command downloads a small invisible
   Chromium browser (~150 MB, one-off) — Buyee's anti-bot check requires real
   browser JavaScript, and this is how the bot passes it.

That's it — the bot now works. Try it:
```
python macdeals.py selftest
python macdeals.py scan --demo
```
The demo uses fake listings so you can see what the output looks like without
touching the internet.

### Step C — Telegram alerts (optional but recommended)

This is what lets the bot message your phone when it spots a great deal.

1. Install Telegram on your phone if you don't have it.
2. In Telegram, search for **@BotFather** (the official one, blue tick), press
   Start, and send: `/newbot`. Give it any name and username. BotFather replies
   with a **token** that looks like `7311234567:AAH8s9d7f...`. Copy it.
3. Now search for **@userinfobot**, press Start, and it replies with your numeric
   **Id** (e.g. `123456789`). Copy that too.
4. Open `config.yaml` (in the bot folder) with Notepad / TextEdit and fill in:
   ```yaml
   telegram:
     enabled: true
     bot_token: "7311234567:AAH8s9d7f..."
     chat_id: "123456789"
   ```
5. **Important:** open a chat with *your own bot* in Telegram (search its username)
   and press **Start** once — bots can't message you until you do this.
6. Test it:
   ```
   python macdeals.py test-telegram
   ```
   You should get a "test message" on your phone within seconds.

---

## 2. Everyday use

| What you want | Command |
|---|---|
| Scan right now, show best deals | `python macdeals.py scan` |
| Scan without sending Telegram alerts | `python macdeals.py scan --no-alert` |
| Also save results to a spreadsheet | `python macdeals.py scan --csv deals.csv` |
| **Run in the background**, scan every 15 min, alert me | `python macdeals.py watch` |
| Background mode with a custom interval (minutes) | `python macdeals.py watch --interval 30` |
| Refresh UK average prices from eBay UK sold listings | `python macdeals.py ukprices` |
| ...and write those averages into config.yaml | `python macdeals.py ukprices --write` |
| Send a Telegram test message | `python macdeals.py test-telegram` |
| Check the bot's logic is healthy | `python macdeals.py selftest` |

Every scan also writes **`deals.html`** in the bot folder — double-click it to open
in your browser and get clickable Buyee / ZenMarket / original-listing links for
every deal found.

### Background mode in practice

`watch` keeps running until you close the terminal window or press `Ctrl+C`.
It pings your Telegram once at startup so you know it's alive, then only messages
you when it finds something worth your attention. Leave the terminal window
minimised, or run it on a spare machine / mini PC that stays on.

Don't set the interval below ~10 minutes — it's unnecessary (good deals last
minutes-to-hours, not seconds) and hammering the sites risks getting your IP
temporarily blocked.

**Want it running 24/7 without your computer being on?** See **`CLOUD_SETUP.md`**
— it walks you through hosting the scan for free on GitHub's servers (no credit
card, ~20 min one-time setup). Your local `scan`/`watch` keep working too.

---

## 3. How the bot decides what's a "deal"

For every listing it computes an **estimated landed cost**:

```
item price (¥)
+ proxy service fee            (default ¥800 — ZenMarket's Mercari fee; Buyee is often cheaper)
+ Japan domestic shipping      (¥0 Mercari — usually seller-paid; ¥1,200 Yahoo estimate)
+ international shipping       (¥8,000 estimate for a laptop by air)
→ converted to GBP at the live exchange rate
× 1.20  (UK import VAT — charged on goods + shipping)
+ £12   (courier handling fee, e.g. DHL/FedEx disbursement)
```

That landed GBP figure is compared with the **UK average price** for the exact
model (stored in `config.yaml`, see section 4). The difference is the **saving %**.

**Alert thresholds** (changeable in `config.yaml` under `alerts:`):

| Saving vs UK average | What happens |
|---|---|
| under 35% | shown in scan results, no alert |
| **≥ 35%** | 📣 Telegram alert — a genuinely good deal |
| **≥ 45%** | 🔥 "INCREDIBLE" alert — drop everything and look |
| **≥ 55%** | ⚠️ alert still sent but flagged *suspiciously cheap* — likely a scam, box-only, or mis-listed item. Read the listing very carefully. |

Other guardrails baked in:

- Only Mercari condition grades **新品、未使用** (brand new, unused) and
  **未使用に近い** (almost unused) are fetched; Yahoo is searched with its
  "unused" filter.
- Listings mentioning ジャンク (junk), 箱のみ (box only), 整備済 (refurbished),
  parts-only, broken, etc. are excluded automatically.
- If a seller states a **battery cycle count** (in the title or description, e.g.
  「充放電回数：4回」), the bot reads it. Anything over **10 cycles** is never
  alerted (edit `max_battery_cycles` to change). "cyc?" in the results means the
  seller didn't state it — worth asking via the proxy's "contact seller" feature.
- **Keyboard layout** is flagged: JP listings are usually JIS layout. "US配列" /
  "USキーボード" in the listing = US layout (closest to UK). The bot can't detect
  genuine UK layout — it's near-nonexistent on the JP market.
- The same item won't alert you twice unless its price drops a further 5%+.

---

## 4. UK average prices — keep them fresh

`config.yaml` ships with researched June-2026 averages for what a UK buyer
realistically pays for a **new or open-box-unused** unit of each model (eBay UK
sold prices, CeX "A/new" pricing, retailer discounts — not Apple RRP). Prices move,
so refresh them every few weeks:

```
python macdeals.py ukprices          # shows fresh eBay-UK-sold medians, changes nothing
python macdeals.py ukprices --write  # also updates config.yaml (old file backed up)
```

You can always hand-edit any `uk_avg_gbp:` number in `config.yaml` if you know
better — it's your benchmark.

---

## 5. Things to know before you buy (important!)

1. **Keyboards are JIS by default.** Japanese MacBooks have the JIS layout (extra
   keys, different Enter). Fine for many people, dealbreaker for others. Listings
   flagged `US` are US-layout exports. UK (ISO-GB) layout effectively doesn't exist
   on the JP second-hand market.
2. **"未使用" relies on the seller's honesty.** The cycle-count check helps, but
   when in doubt use Buyee/ZenMarket's question feature and ask for a screenshot of
   System Report → Power showing the cycle count, before bidding/buying.
3. **Auctions vs Buy-It-Now.** Yahoo listings marked as auctions show the *current*
   bid — the final price can be much higher. The bot prefers the Buy-It-Now (即決)
   price when one exists.
4. **Fees are estimates.** Proxy fees, shipping, and the courier handling charge
   vary by service, plan, weight and courier. The bot's defaults are deliberately
   slightly pessimistic, so real landed cost is usually a touch *better* than shown.
   Tune every number under `costs:` in `config.yaml`.
5. **VAT is not optional.** UK import VAT (20%) applies above £135 — every MacBook.
   Some couriers collect it on delivery rather than at checkout. The bot already
   includes it; don't be surprised when DHL emails you for payment.
6. **Apple warranty:** Apple's limited warranty for Mac is generally honoured
   internationally, but AppleCare bought in Japan and consumer-law rights differ.
   Check serial coverage at checkcoverage.apple.com after purchase.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognised (Windows) | Re-run the Python installer, tick "Add to PATH". Or use `py` instead of `python`. |
| `ModuleNotFoundError: mercapi` (etc.) | Run `python -m pip install -r requirements.txt` again, in the bot folder. |
| Telegram test fails | Token or chat_id has a typo (keep the quotes), or you never pressed **Start** on your own bot. |
| Yahoo/Buyee error mentioning Playwright | Run the two one-off commands: `python -m pip install playwright` then `python -m playwright install chromium`. |
| Scan finds 0 Yahoo/Buyee items | Run `python macdeals.py scan --debug` — it saves raw pages as `debug_buyee_*.html` (or `debug_buyee_blocked.html` if Buyee refused the request). Send those files to Claude; Buyee occasionally changes its page layout. |
| Yahoo/Buyee scan is slow on first query | Normal: the invisible browser solves Buyee's bot-check once (a few seconds), then the bot reuses the earned token at full speed. |
| Lots of weird matches / misses | Check `queries:` in config — you can add/remove search phrases freely. |
| Exchange rate shows "(fallback)" | The free FX API was unreachable; the bot used `fx.fallback_jpy_per_gbp` from config. Update that number occasionally. |
| It alerted a scammy-looking listing | That's what the ⚠️ ≥55% flag is for — the bot surfaces, you judge. Add recurring junk words to `filters.exclude_keywords`. |

**A honest note on scrapers:** Mercari and Yahoo change their websites from time to
time. When that happens a source may suddenly return 0 results — the bot won't
crash, but it'll go quiet on that source. This bot was built and logic-tested in a
sandbox without live access to the JP sites, so your first real `scan` is the true
test. If anything errors or returns nothing, copy the terminal output (and
`yahoo_debug.html` if relevant) back to me and I'll patch it.

---

## 7. Files in this folder

| File | What it is |
|---|---|
| `macdeals.py` | the program you run |
| `config.yaml` | **everything you might want to change** — models, prices, fees, thresholds, Telegram |
| `pricing.py` / `sources.py` / `store.py` / `report.py` | the bot's internals |
| `requirements.txt` | list of libraries for pip |
| `deals.html` | clickable report, rewritten after every scan |
| `seen_items.db` | memory of already-alerted items (delete to reset) |

Happy hunting! 🎯
