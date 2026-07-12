# Apple Deal Bot 🍏 — Japan · US · UK · EU

Scans **nine markets** — Mercari Japan, Yahoo! Auctions, Rakuma and PayPay
Flea Market (the marketplaces behind Buyee/ZenMarket), eBay US, Swappa,
eBay UK, Gumtree and eBay Germany — for the **best-priced Apple
hardware in every sound condition**:

- **MacBook Pro** 14"/16" (M2 Pro generation onwards)
- **Mac mini** (M2/M4 gens), **Mac Studio** (all gens), **iMac 24"**
  (M3/M4), **Mac Pro** (M2 Ultra)
- **Apple Studio Display**

For every listing it estimates the **full landed cost in GBP** (item +
proxy/forwarder fees + product-sized shipping + UK import VAT where
applicable) and a condition/spec/layout-aware **expected UK price**. New,
open-box, like-new and sound used products then share one ranking: how many
pounds and what percentage the all-in cost sits below that expected price.
The same number drives the dashboard, CSV and **WhatsApp alerts**. A secondary
confidence-weighted score makes risky classifieds, uncertain model matches and
thin benchmark data visible without hiding an exceptional lead.

The dashboard is written to `deals.html` after every local scan — and if you
follow **`CLOUD_SETUP.md`** (recommended, ~20 min once), GitHub's servers run
the scan every 20 minutes for free and host the dashboard as a real website
you can open from **any device, any time**, with your computer off.

> **Why not Facebook Marketplace / OfferUp / Mercari US / Vinted?** All were
> evaluated (July 2026). Facebook Marketplace sits behind a login wall —
> scraping it requires an account session, breaches its terms and gets
> accounts banned. OfferUp blocks all automated access outright and Mercari
> US captchas every request. Vinted's electronics section needs a rotating
> app session and stocks very few Macs. Gumtree made the cut as the UK
> classifieds source — with the caveat that classifieds have **no buyer
> protection** and are mostly local-pickup: treat those finds as
> leads to follow up, not one-click buys.

> **How Yahoo Auctions is reached:** Yahoo! JAPAN has geo-blocked all visitors
> from the UK and EEA since April 2022, so the bot searches **Buyee's mirror**
> of Yahoo Auctions instead (Buyee exists precisely to give overseas buyers
> access). This also means the "Original" link for Yahoo items in `deals.html`
> won't open from a UK connection - use the Buyee or ZenMarket links for those.
> Mercari items' original links work fine.

> **eBay UK is scanned too:** domestic listings have no import VAT, no
> international shipping risk, UK (ISO) keyboards and UK returns — an
> underpriced UK listing beats an import every time, and they often dominate
> the unified deal ranking for exactly that reason.

> **Which US markets and why:** every big US resale marketplace was evaluated
> (July 2026). **eBay US** is the main hunting ground — by far the largest
> inventory of Brand New / Open Box MacBook Pros, "Best Offer" haggling, buyer
> protection, and the only one a UK buyer can purchase from **directly** (many
> sellers ship worldwide via eBay International Shipping) instead of renting a
> US parcel-forwarder address. **Swappa** (human-verified listings, New + Mint
> only) is scanned too, but its Cloudflare bot-wall demands a real,
> stealth-patched Chrome — see the note below. **OfferUp** blocks everything
> outright, **Mercari US** captchas every request (and its parent company has
> been publicly wavering on keeping the US marketplace at all), and
> **Facebook Marketplace** requires a login — those three are out.

> **How Swappa is reached:** Swappa refuses plain HTTP clients *and* vanilla
> headless browsers. The bot drives your real installed **Google Chrome**
> (via the `patchright` library) with its automation fingerprints hidden.
> The window is sent **straight to the Dock, minimized** — it never appears
> on screen — and Chrome is **closed the moment the scan finishes**, so
> nothing lingers between scans. (Only if Cloudflare ever throws a fresh
> challenge that won't clear minimized does a window briefly appear to solve
> it — rare, because the solved cookie is kept in `.swappa_chrome_profile/`.)
> Requires Google Chrome installed; works from home connections, not cloud
> runners. Don't want any of this? Delete `swappa` from `sources:` in
> `config.yaml`.

For every JP deal it outputs direct purchase links via **Buyee** and
**ZenMarket**, plus the original Japanese listing; eBay US deals link straight
to the listing.

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

### Step C — WhatsApp alerts (optional but recommended)

This is what lets the bot message your phone when it spots a great deal.
It uses **CallMeBot**, a free WhatsApp-API service for personal use — no
account, no card, one-time 2-minute setup:

1. Add this number to your phone's contacts: **+34 644 53 78 49**
   (name it "CallMeBot" or anything you like; if it ever stops working,
   check https://www.callmebot.com for the current number).
2. Send it this exact WhatsApp message:
   `I allow callmebot to send me messages`
3. Within a couple of minutes it replies with your personal **apikey**
   (a number like `123456`). Copy it.
4. Create `config.local.yaml` beside `config.yaml` and put only this in it:
   ```yaml
   whatsapp:
     enabled: true
     phone: "+447712345678"   # YOUR WhatsApp number, with country code
     apikey: "123456"
   ```
   This file is ignored by Git and should stay private; `config.yaml` contains
   publish-safe placeholders. This project copy has already been migrated to
   that layout.
5. Test it:
   ```
   python macdeals.py test-whatsapp
   ```
   You should get a WhatsApp message within seconds.

(Why WhatsApp and not iMessage/SMS? iMessage can only be sent by a Mac
that's awake — useless once the bot runs in the cloud — and SMS gateways
all cost money. CallMeBot's WhatsApp API is free and works from anywhere.)

---

## 2. Everyday use

| What you want | Command |
|---|---|
| Scan right now, show best deals | `python macdeals.py scan` |
| Scan without sending WhatsApp alerts | `python macdeals.py scan --no-alert` |
| Scan only some markets this run | `python macdeals.py scan --sources mercari,ebay_us` |
| Also save results to a spreadsheet | `python macdeals.py scan --csv deals.csv` |
| Scan on a loop in this terminal, alert me | `python macdeals.py watch` |
| ...with a custom full-scan interval (minutes) | `python macdeals.py watch --interval 30` |
| **Turn background scanning ON** (no terminal needed) | `python macdeals.py background start` |
| **Turn background scanning OFF** | `python macdeals.py background stop` |
| Is background scanning running? | `python macdeals.py background status` |
| Refresh UK average prices from eBay UK sold listings | `python macdeals.py ukprices` |
| ...and write those averages into config.yaml | `python macdeals.py ukprices --write` |
| Send a WhatsApp test message | `python macdeals.py test-whatsapp` |
| Check the bot's logic is healthy | `python macdeals.py selftest` |

Every scan also writes **`deals.html`** in the bot folder — double-click it to
open the dashboard in your browser. It has one ranked **Best Apple deals**
list, simple search/product/market/condition filters, optional confidence and
keyboard controls, and direct Buyee / ZenMarket / marketplace links. The cloud
setup publishes this same dashboard as a website — see `CLOUD_SETUP.md`.

### Background mode in practice — built to WIN deals, not just see them

Great deals last **minutes**. Watch mode is therefore built around speed:

- **Two cadences.** A full scan of every source runs every
  `watch_interval_minutes` (default 20), and in between the bot re-checks
  just the cheap, fast markets (`fast_sources`: Mercari + eBay UK) every
  `fast_interval_minutes` (default 5). A fresh Mercari or eBay bargain is
  spotted within ~5 minutes of being listed.
- **Last-good source snapshots.** A quick pass updates those fast sources and
  merges them with the last successful results from every other market. A
  blocked marketplace or five-minute pass can no longer make most of the
  dashboard disappear; retained results are clearly marked with their age.
- **Alerts fire per source, immediately.** The moment one market's listings
  are processed, qualifying deals go to your WhatsApp — a Mercari find never
  waits for Swappa's browser to finish the scan.
- **Background scanning with an on/off switch.** No terminal window needed,
  and it only ever runs when you say so:
  ```
  python3 macdeals.py background start    # ON  - scans until you stop it
  python3 macdeals.py background stop     # OFF
  python3 macdeals.py background status   # which is it right now?
  ```
  While ON it restarts itself if it crashes; it does **not** start at login
  and does **not** survive a reboot — after a restart it stays off until you
  `background start` again. Check on it any time with `tail -f macdeals.log`.
  If the Mac goes to sleep, scanning simply pauses and picks up again on
  wake — no need to touch your sleep settings.

**The 60-second playbook when your phone buzzes:** open the alert → tap the
link (eBay/Buyee/ZenMarket/Swappa) → sanity-check photos + the flags in the
message → buy or offer. Being SET UP beforehand is most of winning: keep
WhatsApp notifications ON and loud, install the eBay app, keep Buyee and
ZenMarket accounts logged in with a card saved (JP checkouts are 5+ taps if
you're not), and know your walk-away numbers per model in advance. For eBay
"Best Offer" listings, a fast reasonable offer usually beats a slow full-price
click from someone else.

**Don't want your Mac involved at all?** See **`CLOUD_SETUP.md`** — GitHub's
servers run the scan every 20 minutes for free, send the same WhatsApp
alerts, and host the dashboard as a website you can check from any device.
Note the cloud and local copies share no memory: if both are running with
alerts on you may get the same deal twice (fine as redundancy). Local scans
are still the only way to cover Swappa.

---

## 3. What the dashboard shows

`deals.html` has one decision surface: **Best Apple deals**. Every buyable
result is sorted by the same headline metric:

```text
saving % = (expected UK price - all-in cost) / expected UK price
```

Each row/card shows the percentage and pound saving first, then the exact
product, all-in cost, expected price, condition, market and a direct action.
The top result is highlighted but remains part of the same list. Search,
product, market and condition are always visible; less common controls live
under “More filters”. Only the first results are rendered initially, so the
page stays quick and readable even after a large scan.

The **expected UK price** starts with the configured exact model and base spec,
then adjusts for:

- RAM and SSD capacity (including current 8TB/16TB and high-memory Studio
  configurations);
- new, open-box, like-new or used condition;
- a JIS/EU keyboard and Studio Display variants;
- battery cycles above the normal baseline for that condition.

Valid recent used medians are used when available. Clearly contaminated data
(for example, a used median higher than new) is rejected in favour of the
documented fallback rather than silently clamped. Missing size chooses the
lowest compatible benchmark, so uncertainty cannot manufacture a bargain.

The dashboard also shows a secondary **overall score**. It is deliberately
transparent: the saving is multiplied by benchmark confidence and listing /
buyability confidence. eBay/Swappa evidence therefore scores above an
unverified collection-only advert at the same saving, while the raw saving
remains visible. This score never replaces the headline £/% calculation.

## 4. How the bot decides what's a "deal"

For every listing it computes an **estimated landed cost**:

**Japan listings (Mercari / Yahoo / Rakuma):**
```
item price (¥)
+ proxy service fee            (default ¥800 — ZenMarket's Mercari fee; Buyee is often cheaper)
+ Japan domestic shipping      (¥0 Mercari — usually seller-paid; ¥1,200 Yahoo estimate)
+ international shipping       (¥8,000 estimate for a laptop by air)
→ converted to GBP at the live exchange rate
× 1.20  (UK import VAT — charged on goods + shipping)
+ £12   (courier handling fee, e.g. DHL/FedEx disbursement)
```

**US listings (eBay US):**
```
item price ($)
+ US sales tax                 (default 0% — exports and no-sales-tax forwarder
                                states aren't taxed; see config comments)
+ US domestic shipping         ($10 buffer — many listings ship free)
+ package-forwarder fee        ($12 — skipped entirely if the seller ships to
                                the UK directly / via eBay International Shipping,
                                so real cost is often a bit better)
+ international shipping       ($85 estimate, forwarder → UK express, 2-4 kg)
→ converted to GBP at the live exchange rate
× 1.20  (UK import VAT — laptops are 0% customs duty)
+ £12   (courier handling fee)
```

That landed GBP figure is compared with the condition/spec-aware **expected UK
price** described above. `alerts.min_savings_pct` is one threshold everywhere
(35% by default), because keyboard/layout disadvantages and import costs are
now included explicitly in the calculation. Deals below it still appear on
the dashboard. Suspiciously cheap or low-confidence results remain visible as
leads but cannot buzz your phone; auctions are not ranked because a current bid
is not a payable price.

Other guardrails baked in:

- Mercari condition grades 1–4 are searched: unused, nearly unused, no visible
  wear and light wear. eBay and Swappa likewise include their sound used
  grades. Each condition is scored against its own expected UK price, so a
  used unit no longer gets an unfair comparison with new stock.
- Listings mentioning ジャンク (junk), 箱のみ (box only), 整備済 (refurbished),
  parts-only, broken, etc. are excluded automatically — plus English trap words
  for eBay (refurb, for parts, cracked, activation lock / MDM, box only,
  local-pickup-only listings a UK buyer can't receive, ...).
- eBay listings that accept a **Best Offer** are flagged — the displayed
  saving is the floor, not the ceiling; haggle.
- If a seller states a **battery cycle count** (in the title or description,
  e.g. 「充放電回数：4回」), the bot reads it. The defaults are 10 cycles for
  new/unused claims, 60 for like-new and 800 for used; excess wear also lowers
  the expected price. Unknown counts are called out for manual checking.
- **Keyboard layout** is flagged: JP listings are usually JIS layout. "US配列" /
  "USキーボード" in the listing = US layout (closest to UK). The bot can't detect
  genuine UK layout — it's near-nonexistent on the JP market.
- The same item won't alert you twice unless its price drops a further 5%+.

---

## 5. UK price benchmarks — keep them fresh

`config.yaml` holds two benchmark prices per model:

- `uk_avg_gbp` — what a UK buyer pays for a **new / open-box-unused** unit
  (the starting point for new and like-new estimates);
- `uk_used_gbp` — the **eBay UK sold median for used units** (drives the
  condition-aware expected price for used and like-new stock).

Both are refreshed from real recent eBay UK SOLD listings (medians only
written when there are **10+ sales** behind them — small samples are noise):

```
python macdeals.py ukprices          # shows fresh eBay-UK-sold medians, changes nothing
python macdeals.py ukprices --write  # also updates config.yaml
```

You can always hand-edit any `uk_avg_gbp:` number in `config.yaml` if you know
better — it's your benchmark.

---

## 6. Things to know before you buy (important!)

1. **Keyboards: JP listings are JIS, US listings are ANSI.** Japanese MacBooks
   have the JIS layout (extra keys, different Enter) — fine for many people,
   dealbreaker for others; JP listings flagged `US` are US-layout exports.
   eBay US machines are ANSI ("US") layout, the closest thing to UK you'll
   find — true UK (ISO-GB) layout effectively doesn't exist on either market.
2. **"未使用" / "Open Box" relies on the seller's honesty.** The cycle-count
   check helps, but when in doubt ask the seller (Buyee/ZenMarket's question
   feature, or eBay messages) for a screenshot of System Report → Power showing
   the cycle count, before bidding/buying.
3. **Auctions vs Buy-It-Now.** Yahoo listings marked as auctions show the *current*
   bid — the final price can be much higher. The bot prefers the Buy-It-Now (即決)
   price when one exists. eBay US is searched Buy-It-Now-only by default, and
   listings that accept a Best Offer are flagged — try a cheeky offer.
4. **Fees are estimates.** Proxy/forwarder fees, shipping, and the courier
   handling charge vary by service, plan, weight and courier. The bot's defaults
   are deliberately slightly pessimistic, so real landed cost is usually a touch
   *better* than shown. Tune every number under `costs:` in `config.yaml`.
   For eBay US items sold with **eBay International Shipping**, the checkout
   quotes you the exact all-in figure (shipping + UK VAT) before you commit —
   compare it against the bot's estimate.
5. **VAT is not optional.** UK import VAT (20%) applies above £135 — every MacBook,
   from Japan or the US alike. Some couriers collect it on delivery rather than at
   checkout. The bot already includes it; don't be surprised when DHL emails you
   for payment. (Laptops carry 0% customs duty from both countries.)
6. **US sales tax.** Buying an export (seller ships to the UK) isn't US-taxed.
   If you use a US parcel forwarder, pick one in a no-sales-tax state
   (Oregon, Delaware, Montana, New Hampshire) and keep `us_sales_tax_pct: 0`;
   a forwarder in e.g. Florida means ~7% — set it in `config.yaml`.
7. **Watch for corporate stock in US listings.** Ex-company machines can be
   MDM/DEP-enrolled ("remote management") — a nasty surprise on first boot. The
   bot excludes titles admitting it (MDM / activation lock / demo unit), but ask
   the seller if a "new open box" price looks too easy.
8. **Apple warranty:** Apple's limited warranty for Mac is generally honoured
   internationally, but AppleCare bought in Japan/the US and consumer-law rights
   differ. Check serial coverage at checkcoverage.apple.com after purchase.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognised (Windows) | Re-run the Python installer, tick "Add to PATH". Or use `py` instead of `python`. |
| `ModuleNotFoundError: mercapi` (etc.) | Run `python -m pip install -r requirements.txt` again, in the bot folder. |
| WhatsApp test fails | Phone or apikey has a typo (keep the quotes, include the country code), or you never sent the activation message to CallMeBot (README step C). If it worked before and stopped, check https://www.callmebot.com — the activation number occasionally changes. |
| Yahoo/Buyee error mentioning Playwright | Run the two one-off commands: `python -m pip install playwright` then `python -m playwright install chromium`. |
| Scan finds 0 Yahoo/Buyee items | Run `python macdeals.py scan --debug` — it saves raw pages as `debug_buyee_*.html` (or `debug_buyee_blocked.html` if Buyee refused the request). Send those files to Claude; Buyee occasionally changes its page layout. |
| Scan finds 0 eBay US items | Occasional one-offs are normal (eBay serves several page layouts; the bot handles the known ones and retries once). If it *persists*, run `python macdeals.py scan --debug` and send `debug_ebay_us_*.html` to Claude. |
| Swappa errors about patchright / Chrome | Swappa needs two things the other sources don't: `python3 -m pip install patchright`, and Google Chrome installed. No Chrome / no desktop session (e.g. a headless server) = Swappa gets skipped; everything else still works. |
| Swappa "challenge did not clear" | Cloudflare escalated for your connection. Delete the `.swappa_chrome_profile` folder and rescan; if it persists, Swappa may have tightened things — run with `--debug` and send `debug_swappa_blocked.html` to Claude. |
| Gumtree returns 0 / "HTTP 247" | Gumtree rate-limits query bursts aggressively. The bot already paces itself; if you scanned repeatedly in a short window, wait 15–30 min and it recovers on its own. |
| Yahoo/Buyee scan is slow on first query | Normal: the invisible browser solves Buyee's bot-check once (a few seconds), then the bot reuses the earned token at full speed. |
| Lots of weird matches / misses | Check `queries:` in config — you can add/remove search phrases freely. |
| Exchange rate shows "(fallback)" | The free FX API was unreachable; the bot used `fx.fallback_jpy_per_gbp` from config. Update that number occasionally. |
| A scammy-looking listing appears | Suspiciously low and low-confidence rows are kept as visible leads but blocked from WhatsApp. Add any recurring trap words to `filters.exclude_keywords`. |

**An honest note on scrapers:** Mercari, Yahoo, Gumtree and eBay change their websites from
time to time. When that happens a source may suddenly return 0 results — the bot
keeps the last successful snapshot for a few hours and continues through every
other source. Gumtree's current category feeds, descriptions, exact price fields
and pagination were live-tested in July 2026. If a source stays stale, run with
`--debug` and inspect the matching `debug_*.html` file.

---

## 8. Files in this folder

| File | What it is |
|---|---|
| `macdeals.py` | the program you run |
| `config.yaml` | **everything you might want to change** — models, prices, fees, thresholds, WhatsApp |
| `pricing.py` / `sources.py` / `store.py` / `report.py` | the bot's internals |
| `requirements.txt` | list of libraries for pip |
| `deals.html` | the dashboard, rewritten after every scan (open it in a browser) |
| `seen_items.db` | memory of already-alerted items (delete to reset) |
| `.swappa_chrome_profile/` | Chrome profile holding Swappa's Cloudflare cookie (delete to reset) |
| `com.macdeals.watch.plist` | what `background start/stop` switches on and off (used in place — don't move it) |
| `macdeals.log` | the background watcher's log (`tail -f` it) |
| `CLOUD_SETUP.md` | run it all in the cloud for free — alerts + dashboard website, laptop off |
| `.github/workflows/scan.yml` | the cloud runner's timetable (used by CLOUD_SETUP) |

Happy hunting! 🎯
