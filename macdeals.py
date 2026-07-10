#!/usr/bin/env python3
"""
macdeals.py - MacBook Pro deal scanner: Japan (Mercari / Yahoo Auctions /
Rakuma via Buyee & ZenMarket) and the US (eBay), landed-cost compared
against UK average prices.

Commands
--------
  python macdeals.py scan                 one-off scan, prints table + writes deals.html
  python macdeals.py watch                scan on a loop in THIS terminal (Ctrl+C stops)
  python macdeals.py background start     scan in the BACKGROUND until you stop it
  python macdeals.py background stop      turn background scanning off
  python macdeals.py background status    is background scanning on?
  python macdeals.py ukprices [--write]   refresh UK price benchmarks from eBay UK SOLD listings
  python macdeals.py test-whatsapp        send a test message to your WhatsApp
  python macdeals.py selftest             run built-in checks (no internet needed)

Useful flags:  --demo (fake data, test the pipeline)   --debug (save raw pages)
               --csv deals.csv   --no-alert   --interval 20
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime

import yaml

import pricing
import report
import sources
import store

CONFIG_FILE = "config.yaml"


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Environment variables override config.yaml. This is how the free cloud
    # runner (GitHub Actions) injects your WhatsApp secrets WITHOUT them ever
    # being written into a file in the repo. Locally, no env vars are set, so
    # your config.yaml values are used as normal.
    import os
    phone = os.environ.get("WHATSAPP_PHONE", "").strip()
    key = os.environ.get("WHATSAPP_APIKEY", "").strip()
    if phone or key:
        w = cfg.setdefault("whatsapp", {})
        if phone:
            w["phone"] = phone
        if key:
            w["apikey"] = key
        w["enabled"] = True
    # Optional: override the sources list from an env var, e.g. on a server IP
    # where Buyee blocks the browser you might set SOURCES=mercari,ebay_us.
    src = os.environ.get("SOURCES", "").strip()
    if src:
        cfg.setdefault("scan", {})["sources"] = [x.strip() for x in src.split(",") if x.strip()]
    return cfg


# ----------------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------------

SCANNERS = [
    # fastest first: alerts fire per source, so a Mercari find must not
    # wait for Swappa's browser or Buyee's bot-check
    ("mercari", "mercari", lambda cfg, dbg: sources.scan_mercari(cfg)),
    ("ebay_uk", "ebay uk", sources.scan_ebay_uk),
    ("ebay_us", "ebay us", sources.scan_ebay_us),
    ("ebay_de", "ebay germany", sources.scan_ebay_de),
    ("gumtree", "gumtree uk", sources.scan_gumtree),
    ("craigslist", "craigslist us", sources.scan_craigslist),
    ("rakuma", "rakuma", sources.scan_rakuma),
    ("paypay", "paypay flea market", sources.scan_paypay),
    ("yahoo", "yahoo auctions", sources.scan_yahoo),
    ("swappa", "swappa", sources.scan_swappa),
]

CYCLE_FETCHERS = {"mercari": sources.fetch_mercari_cycles,
                  "rakuma": sources.fetch_rakuma_cycles,
                  "paypay": sources.fetch_paypay_cycles,
                  "yahoo": sources.fetch_yahoo_cycles,
                  "ebay_us": sources.fetch_ebay_us_cycles,
                  "ebay_uk": sources.fetch_ebay_uk_cycles,
                  "ebay_de": sources.fetch_ebay_de_cycles,
                  "craigslist": sources.fetch_craigslist_cycles,
                  "gumtree": sources.fetch_gumtree_cycles,
                  "swappa": sources.fetch_swappa_cycles}


def run_scan(cfg: dict, send_alerts: bool, debug: bool, demo: bool,
             csv_path: str | None) -> None:
    t0 = datetime.now().strftime("%H:%M:%S")
    print(f"[{t0}] scanning...")

    rates, fx_note = pricing.get_fx(cfg["fx"])
    a = cfg["alerts"]
    # alert bars are per REGION (UK/US 35%, JP 50% by default - see config);
    # global_min is the lowest of them, the point below which nothing alerts
    global_min = pricing.global_min_alert_pct(cfg)
    budget = int(cfg["scan"]["max_detail_fetch"])
    matched: list[pricing.Listing] = []
    dupes: set = set()
    sent = 0

    def process_batch(batch: list[pricing.Listing]) -> None:
        """Filter, score, cycle-enrich and (immediately) alert one source's
        listings, then bank them for the end-of-scan report."""
        nonlocal budget, sent
        out: list[pricing.Listing] = []
        for l in batch:
            if pricing.is_excluded(l.title, cfg["filters"]["exclude_keywords"]):
                continue
            pricing.parse_listing_specs(l)
            if not l.family:
                continue      # not a tracked product (or pre-2022 / an Air)
            pricing.match_model(l, cfg["models"], cfg)
            if not l.model_id:
                continue
            # collapse identical relists (same market, title and price under
            # different item ids) - one row is enough
            sig = (l.source, l.title.strip(), round(l.price))
            if sig in dupes:
                continue
            dupes.add(sig)
            store.upsert_seen(l.item_id, l.source, l.title, int(l.price))
            pricing.score(l, cfg, rates)
            # a price implausibly low for a whole unit IS a part/accessory -
            # drop it outright rather than displaying it with a warning
            if any("PRICE TOO LOW" in f for f in l.flags):
                continue
            out.append(l)
        out.sort(key=lambda x: x.savings_pct, reverse=True)
        # enrich the most promising MacBooks with battery-cycle info from the
        # listing page (only MacBooks have a battery worth checking) -
        # "promising" = within 8 points of its own region's alert bar
        for l in out:
            if budget <= 0 or demo:
                break
            if l.savings_pct < pricing.alert_thresholds(
                    cfg, l.source, l.family)["min"] - 8:
                break         # sorted by savings - the rest are further away
            if l.family != "macbook" or l.cycles is not None:
                continue
            l.cycles = CYCLE_FETCHERS[l.source](l.item_id)
            budget -= 1
            time.sleep(1.0)
            l.flags = []      # rescore with cycle info
            pricing.score(l, cfg, rates)
        # alert NOW - great deals last minutes, not scan-lengths
        if send_alerts:
            for l in out:
                if l.savings_pct < global_min:
                    break     # sorted desc - nothing below can alert anywhere
                if l.grade not in ("resale", "personal"):
                    continue  # "good" listings live in the value section only
                if l.savings_pct < pricing.alert_thresholds(
                        cfg, l.source, l.family)["min"]:
                    continue  # below this REGION's bar for this product class
                if (l.family == "macbook" and l.cycles is not None
                        and l.cycles > pricing.max_cycles_for(l.grade, cfg)):
                    continue
                if not store.should_alert(l.item_id, int(l.price),
                                          a["realert_drop_pct"]):
                    continue
                if store.whatsapp_send(cfg, report.whatsapp_message(l, cfg)):
                    store.mark_alerted(l.item_id, int(l.price))
                    sent += 1
        matched.extend(out)

    if demo:
        process_batch(demo_listings())
    else:
        for src, label, scanner in SCANNERS:
            if src not in cfg["scan"]["sources"]:
                continue
            r = scanner(cfg, debug)
            print(f"  {label}: {len(r)} raw listings")
            process_batch(r)
        # don't leave the Swappa Chrome open between scans
        sources.swappa_release()

    store.prune_stale(90)
    matched.sort(key=lambda x: x.savings_pct, reverse=True)

    # best-value section: every buyable listing (all tiers, all markets)
    # re-scored for price relative to condition
    v = cfg.get("value", {})
    best_value: list[pricing.Listing] = []
    if v.get("enabled", True):
        floor = float(v.get("implausible_value_ratio", 0.45))
        for l in matched:
            if l.is_auction:
                continue      # a current bid is not a price you can pay
            if any("PRICE TOO LOW" in f for f in l.flags):
                continue
            if (l.cycles is not None
                    and l.cycles > int(v.get("max_battery_cycles", 800))):
                continue      # heavily used - fails the condition baseline
            # a "brand new" listing stating a well-used battery is lying
            # about one of the two - either way, not a deal to rank
            if (l.grade == "resale" and l.cycles is not None
                    and l.cycles > pricing.max_cycles_for("personal", cfg)):
                continue
            pricing.value_score(l, cfg)
            # far below fair-for-condition = scam/damage/mislist territory
            rate = rates.get(l.currency)
            if (rate and l.fair_gbp
                    and (l.price / rate) < l.fair_gbp * floor):
                continue
            best_value.append(l)
        best_value.sort(key=lambda x: x.value_pct, reverse=True)
        best_value = best_value[:int(v.get("top_n", 100))]

    # console output - best 30 of each tier so neither crowds the other out
    top = ([l for l in matched if l.grade == "resale"][:30]
           + [l for l in matched if l.grade == "personal"][:30])
    report.console_table(top, best_value, rates, fx_note, cfg)
    # dashboard - the overall best PLUS the best few of EVERY model, so a
    # rare product (Studio Display, Mac Pro, ...) stays visible even when
    # MacBooks dominate the top of the ranking. MacBooks whose stated cycle
    # count busts the tier ceiling aren't near-new - keep them off entirely
    # (they were already barred from alerting).
    flips = [l for l in matched
             if l.grade in ("resale", "personal")
             and not (l.family == "macbook" and l.cycles is not None
                      and l.cycles > pricing.max_cycles_for(l.grade, cfg))]
    dash, per_model = [], {}
    for l in flips:                      # sorted by savings desc already
        n = per_model.get(l.model_id, 0)
        if len(dash) < 150 or n < 8:
            dash.append(l)
            per_model[l.model_id] = n + 1
        if len(dash) >= 450:
            break
    report.write_html(dash, best_value, "deals.html", rates, cfg)
    n_res = sum(1 for l in matched if l.grade == "resale")
    n_per = sum(1 for l in matched if l.grade == "personal")
    n_good = len(matched) - n_res - n_per
    print(f"  wrote deals.html ({len(matched)} matched: {n_res} resale-grade, "
          f"{n_per} practically-new, {n_good} good-condition; "
          f"{len(best_value)} in the best-value ranking)")
    if csv_path:
        report.write_csv(matched, csv_path)
        print(f"  wrote {csv_path}")

    if send_alerts:
        print(f"  whatsapp alerts sent: {sent}")


def run_watch(cfg: dict, interval: int | None, debug: bool) -> None:
    """Two cadences: a FULL scan (all sources) every `watch_interval_minutes`,
    with quick passes over the cheap `fast_sources` every
    `fast_interval_minutes` in between - so new Mercari/eBay listings are
    spotted within minutes while the slow browser sources stay polite."""
    s = cfg["scan"]
    full_min = interval or int(s.get("watch_interval_minutes", 20))
    full_min = max(full_min, 10)
    fast_min = max(int(s.get("fast_interval_minutes", 5)), 3)
    fast_srcs = [x for x in s.get("fast_sources", ["mercari", "ebay_uk", "ebay_us"])
                 if x in s["sources"]]
    print(f"Watch mode: full scan every {full_min} min"
          + (f", quick {'/'.join(fast_srcs)} pass every {fast_min} min"
             if fast_srcs else "")
          + ". (In a terminal, Ctrl+C stops it; in background mode, "
            "`python3 macdeals.py background stop`.)")
    bars = ", ".join(
        f"{r.upper()} {pricing.alert_thresholds(cfg, src)['min']:.0f}%+"
        for r, src in (("uk", "ebay_uk"), ("us", "ebay_us"),
                       ("eu", "ebay_de"), ("jp", "mercari")))
    kbless = pricing.alert_thresholds(cfg, "mercari", "mac_mini")["min"]
    store.whatsapp_send(cfg, f"👀 Apple deal bot is watching. Full scan of "
                             f"{', '.join(s['sources'])} every {full_min} min; "
                             f"quick pass of {', '.join(fast_srcs)} every "
                             f"{fast_min} min. Alerting at {bars} savings "
                             f"(keyboardless products: JP {kbless:.0f}%+).")
    fast_cfg = dict(cfg)
    fast_cfg["scan"] = dict(s)
    fast_cfg["scan"]["sources"] = fast_srcs

    def one(run_cfg, label):
        try:
            run_scan(run_cfg, send_alerts=True, debug=debug, demo=False,
                     csv_path=None)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  {label} cycle error (will retry): {e}")

    while True:
        cycle_end = time.time() + full_min * 60 + random.randint(-30, 30)
        one(cfg, "full scan")
        while fast_srcs and time.time() + fast_min * 60 <= cycle_end:
            try:
                time.sleep(fast_min * 60 + random.randint(-20, 20))
            except KeyboardInterrupt:
                print("\nStopped.")
                return
            one(fast_cfg, "quick pass")
        wait = max(cycle_end - time.time(), 30)
        nxt = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))
        print(f"  next full scan ~{nxt}")
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


# ----------------------------------------------------------------------------
# Background on/off switch (macOS launchd)
# ----------------------------------------------------------------------------

LAUNCHD_LABEL = "com.macdeals.watch"
LAUNCHD_PLIST = "com.macdeals.watch.plist"


def run_background(action: str) -> int:
    """Turn the background watcher on/off. The agent is loaded straight from
    the bot folder, so it only ever runs when YOU start it - it does not
    start at login and does not survive a reboot."""
    import os
    import subprocess
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    plist = os.path.abspath(LAUNCHD_PLIST)

    # if an old always-on copy exists in LaunchAgents (earlier setup advice),
    # remove it so nothing auto-starts at login without your say-so
    old = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_PLIST}")
    if os.path.exists(old):
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        os.remove(old)
        print("(removed the old always-on copy from ~/Library/LaunchAgents - "
              "the bot no longer auto-starts at login)")

    def is_running() -> bool:
        r = subprocess.run(["launchctl", "print", target], capture_output=True)
        return r.returncode == 0

    if action == "start":
        if is_running():
            print("Background scanning is already ON. (`background stop` to turn off.)")
            return 0
        r = subprocess.run(["launchctl", "bootstrap", domain, plist],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Could not start: {(r.stderr or r.stdout).strip()}")
            return 1
        print("Background scanning is ON. It will keep scanning (and restart "
              "itself if it crashes) until you run `background stop`, log "
              "out, or reboot - it never starts without you.\n"
              "Watch it work:  tail -f macdeals.log")
        return 0

    if action == "stop":
        if not is_running():
            print("Background scanning was not running.")
            return 0
        r = subprocess.run(["launchctl", "bootout", target],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Could not stop: {(r.stderr or r.stdout).strip()}")
            return 1
        print("Background scanning is OFF.")
        return 0

    # status
    if is_running():
        print("Background scanning is ON  (turn off: python3 macdeals.py background stop)")
    else:
        print("Background scanning is OFF (turn on:  python3 macdeals.py background start)")
    return 0


# ----------------------------------------------------------------------------
# UK price refresh (eBay UK sold listings)
# ----------------------------------------------------------------------------

def run_ukprices(cfg: dict, write: bool, debug: bool) -> None:
    print("Fetching recent eBay UK SOLD prices, UK located:")
    print("  - New + Open box  -> uk_avg_gbp   (the new-unit benchmark)")
    print("  - Used            -> uk_used_gbp  (feeds condition-aware fair value)")
    print("This takes a few minutes - two polite requests per model.\n")
    results = []
    for mdl in cfg["models"]:
        med_new, n1 = sources.ebay_uk_sold_median(mdl["ebay_query"], debug=debug)
        time.sleep(2.5)
        med_used, n2 = sources.ebay_uk_sold_median(mdl["ebay_query"], debug=debug,
                                                   conditions="3000")
        time.sleep(2.5)
        # medians from fewer than 10 sales are too noisy to overwrite with
        if med_new and n1 < 10:
            med_new = None
        if med_used and n2 < 10:
            med_used = None
        cur = mdl["uk_avg_gbp"]
        cur_used = mdl.get("uk_used_gbp", "-")
        s_new = (f"new median £{med_new:>6.0f} ({n1})" if med_new
                 else f"new: too little data ({n1})")
        s_used = (f"used median £{med_used:>6.0f} ({n2})" if med_used
                  else f"used: too little data ({n2})")
        print(f"  {mdl['id']:<10} now £{cur:>5}/£{cur_used!s:>5}   {s_new}   {s_used}")
        results.append((mdl["id"], med_new, med_used))
    if write:
        updated = 0
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        import re as _re
        for mid, med_new, med_used in results:
            if med_new:
                pat = _re.compile(r"(\{id:\s*" + _re.escape(mid) + r".*?uk_avg_gbp:\s*)(\d+)")
                text, c = pat.subn(lambda m: m.group(1) + str(int(med_new)), text, count=1)
                updated += c
            if med_used:
                # update uk_used_gbp if present, else insert it after uk_avg_gbp
                pat = _re.compile(r"(\{id:\s*" + _re.escape(mid)
                                  + r".*?uk_avg_gbp:\s*\d+)(?:,\s*uk_used_gbp:\s*\d+)?")
                text, c = pat.subn(
                    lambda m: m.group(1) + f", uk_used_gbp: {int(med_used)}",
                    text, count=1)
                updated += c
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\nWrote {updated} updated figures into {CONFIG_FILE}.")
    else:
        print("\n(Read-only run. Add --write to save these medians into config.yaml.)")


# ----------------------------------------------------------------------------
# Demo data + self-test
# ----------------------------------------------------------------------------

def demo_listings() -> list[pricing.Listing]:
    L = pricing.Listing
    us = dict(currency="USD", condition="Open Box")
    demo = [
        L("m11111111111", "mercari", "【新品未開封】MacBook Pro 14インチ M4 Pro 24GB 512GB スペースブラック", 218000, condition="新品、未使用"),
        L("m22222222222", "mercari", "MacBook Pro 16インチ M3 Pro 18GB/512GB 未使用に近い 充放電回数3回 US配列", 195000, condition="未使用に近い"),
        L("x1234567890", "yahoo", "MacBook Pro 14 M5 16GB 512GB 新品 未使用 国内正規品", 248000, is_auction=False, condition="未使用"),
        L("x2345678901", "yahoo", "ジャンク MacBook Pro M3 Max 16インチ", 90000, condition="未使用"),
        L("m33333333333", "mercari", "MacBook Pro M2 Pro 14inch 16GB 512GB 箱のみ", 65000, condition="新品、未使用"),
        L("m44444444444", "mercari", "MacBook Air M2 13インチ 新品", 99000, condition="新品、未使用"),
        L("m55555555555", "mercari", "MacBook Pro M2 Max 16インチ 32GB 1TB 新品未使用 JIS配列", 230000, condition="新品、未使用"),
        L("123456789012", "ebay_us", "Apple MacBook Pro 14\" M4 Pro 24GB RAM 512GB SSD Space Black NEW SEALED", 1449, **us),
        L("234567890123", "ebay_us", "Apple MacBook Pro 16-inch M4 Pro 24GB 512GB Open Box - 2 cycles", 1699, best_offer=True, **us),
        L("LADEMO12345", "swappa", "MacBook Pro 14 M4 Pro 512GB 24GB - Mint (Swappa)", 1379, currency="USD", condition="Mint", grade="personal"),
        L("m66666666666", "mercari", "【極美品】MacBook Pro 14インチ M4 Pro 24GB 512GB 充放電回数45回", 175000, condition="目立った傷や汚れなし", grade="personal"),
        L("345678901234", "ebay_us", "Apple MacBook Pro 16\" M4 Pro 24GB 512GB - Like New, only 21 cycles", 1499, currency="USD", condition="Used (seller: like new)", grade="personal"),
        # the wider 2026 scope: desktops, displays and iPads
        L("m88888888888", "mercari", "【新品未使用】Mac Studio M1 Max 32GB 512GB", 75000, condition="新品、未使用"),
        L("x3456789012", "yahoo", "iMac 24インチ M4 16GB 256GB ブルー 新品未開封", 120000, condition="未使用"),
        L("p1234567890z", "paypay", "iPad Air 13インチ M4 128GB Wi-Fi 未使用", 68000, condition="未使用"),
        L("567890123456", "ebay_de", "Apple Mac mini M4 16GB 256GB - NEU versiegelt", 399, currency="EUR", condition="Brand New"),
        L("cl-demo-1", "craigslist", "New Sealed iPad Pro 13 M4 256GB Space Black", 650, currency="USD", condition="seller says new/sealed"),
        L("1800109157", "gumtree", "Apple Studio Display 27 inch - brand new, still boxed", 600, currency="GBP", condition="seller says new/sealed"),
        L("m99999999999", "mercari", "Mac mini M4 Pro 24GB 512GB 未使用に近い", 138000, condition="未使用に近い"),
    ]
    for l in demo:
        if l.source in ("ebay_us", "swappa", "craigslist"):
            l.keyboard = "US"
        elif l.source in ("ebay_uk", "gumtree"):
            l.keyboard = "UK"
        elif l.source == "ebay_de":
            l.keyboard = "EU"
    return demo


def run_selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    cfg = load_config()
    check("config.yaml loads", isinstance(cfg, dict) and "models" in cfg)
    check("43 models defined", len(cfg["models"]) == 43)

    l = pricing.Listing("m1", "mercari", "MacBook Pro 14インチ M4 Pro 24GB 512GB 新品", 200000)
    pricing.parse_listing_specs(l)
    check("chip M4 PRO parsed", l.chip == "M4 PRO")
    check('size 14 parsed', l.size == 14 and not l.size_guessed)
    check("RAM 24 / SSD 512 parsed", l.ram_gb == 24 and l.storage_gb == 512)

    l2 = pricing.Listing("m2", "mercari", "ＭacBook Ｐro Ｍ3 マックス 16型 1TB 36GB", 300000)
    pricing.parse_listing_specs(l2)
    check("full-width + katakana chip (M3 MAX)", l2.chip == "M3 MAX")
    check("16-inch + 1TB parsed", l2.size == 16 and l2.storage_gb == 1024)

    l3 = pricing.Listing("m3", "mercari", "MacBook Pro M2 13インチ 新品", 120000)
    pricing.parse_listing_specs(l3)
    check("base M2 (13-inch) excluded", l3.chip == "")

    l4 = pricing.Listing("m4", "mercari", "MacBook Air M2 新品", 99000)
    pricing.parse_listing_specs(l4)
    check("MacBook Air excluded", l4.chip == "")

    l5 = pricing.Listing("m5", "mercari", "MacBook Pro 16GB M5 512GB", 210000)
    pricing.parse_listing_specs(l5)
    check('16GB not mistaken for 16-inch (size guessed 14")', l5.size == 14 and l5.size_guessed)

    check("US keyboard detected",
          (lambda x: (pricing.parse_listing_specs(x), x.keyboard)[1])(
              pricing.Listing("m6", "mercari", "MacBook Pro M4 14 US配列", 1)) == "US")

    check("box-only excluded",
          pricing.is_excluded("MacBook Pro M3 箱のみ", cfg["filters"]["exclude_keywords"]) is not None)

    check("cycle count parsed (充放電回数：4回)",
          pricing.find_cycle_count("バッテリー 充放電回数：4回です") == 4)
    check("cycle count parsed (cycle count 7)",
          pricing.find_cycle_count("Battery cycle count 7") == 7)
    check("cycle count parsed (only 2 cycles)",
          pricing.find_cycle_count("Open Box - only 2 cycles") == 2)

    check("financing offer not mistaken for the price ($95/mo)",
          sources._first_usd_price("$95/mo with Affirm ... $1,849.00") == 1849.0)
    check("plain price still parsed", sources._first_usd_price("US $1,046") == 1046.0)

    from bs4 import BeautifulSoup as _BS
    sold_card = _BS('<li class="list"><div class="thumbnail-area soldOut">'
                    '</div><p class="price">215,000 YEN</p></li>', "html.parser")
    live_card = _BS('<li class="list"><div class="thumbnail-area"></div>'
                    '<p class="price">215,000 YEN</p></li>', "html.parser")
    check("Buyee soldOut overlay detected",
          sources._card_is_sold(sold_card, "215,000 YEN") is True)
    check("live card not mistaken for sold",
          sources._card_is_sold(live_card, "215,000 YEN") is False)
    check("reserved listing excluded (予約済み)",
          pricing.is_excluded("予約済み)MacBook Pro M3 1TB 14インチ",
                              cfg["filters"]["exclude_keywords"]) is not None)
    check("rakuma search page not mistaken for an item",
          sources.BUYEE_RAKUMA_HREF_RE.search("/rakuma/search?keyword=x") is None)

    check("personal tier configured", cfg.get("personal", {}).get("enabled") is True)
    check("JP grade: 新品未開封 -> resale",
          sources._jp_grade("新品未開封 MacBook Pro") == "resale")
    check("JP grade: 新品同様 (like-new, used) -> personal",
          sources._jp_grade("新品同様 MacBook Pro 極美品") == "personal")
    check("JP grade: 極美品 -> personal",
          sources._jp_grade("【極美品】MacBook Pro M4") == "personal")
    check("JP grade: plain 中古 -> rejected",
          sources._jp_grade("中古 MacBook Pro M4") is None)
    check("eBay like-new title accepted for personal tier",
          bool(sources.EBAY_LIKE_NEW_RE.search("MacBook Pro M4 - Mint condition, 21 cycles")))
    check("eBay generic used title rejected for personal tier",
          not sources.EBAY_LIKE_NEW_RE.search("Apple MacBook Pro 14 M4 512GB Space Black"))
    check("cycle ceilings: resale 10 / personal 60",
          pricing.max_cycles_for("resale", cfg) == 10
          and pricing.max_cycles_for("personal", cfg) == 60)
    check("regional alert bars: UK 35 / US 35 / JP 50",
          pricing.alert_thresholds(cfg, "ebay_uk")["min"] == 35
          and pricing.alert_thresholds(cfg, "ebay_us")["min"] == 35
          and pricing.alert_thresholds(cfg, "swappa")["min"] == 35
          and pricing.alert_thresholds(cfg, "mercari")["min"] == 50
          and pricing.alert_thresholds(cfg, "yahoo")["min"] == 50)
    check("global minimum alert bar is 35",
          pricing.global_min_alert_pct(cfg) == 35)

    # ---- parts / wrong-model leaks (seen in live output) ----
    check("'top case' part listing excluded",
          pricing.is_excluded("Genuine Apple MacBook Pro 16 A3428 M5 Pro top case UK Keyboard",
                              cfg["filters"]["exclude_keywords"]) is not None)
    check("'LCD Display Assembly' part listing excluded",
          pricing.is_excluded("MacBook Pro 16 A3428 LCD Display Assembly M5 Pro Grade A+",
                              cfg["filters"]["exclude_keywords"]) is not None)
    check("accessory 'for MacBook' excluded",
          pricing.is_excluded("Leather Case for MacBook Pro 14 M4",
                              cfg["filters"]["exclude_keywords"]) is not None)
    l15 = pricing.Listing("e15", "ebay_uk", "macbook pro m3 15 inch 8 gb ram 256gb",
                          414, currency="GBP")
    pricing.parse_listing_specs(l15)
    check("explicit 15-inch (an Air) rejected", l15.chip == "")
    lj = pricing.Listing("ej", "ebay_uk", "MacBook Pro 14 M3 A2918 Japanese Keyboard",
                         700, currency="GBP")
    lj.keyboard = "UK"
    pricing.parse_listing_specs(lj)
    check("'Japanese Keyboard' in English detected as JIS", lj.keyboard == "JIS")
    le = pricing.Listing("ee", "ebay_uk", "MacBook Pro 14 M4 Pro 512GB Swedish Keyboard",
                         1400, currency="GBP")
    le.keyboard = "UK"
    pricing.parse_listing_specs(le)
    check("Swedish keyboard detected as non-UK EU layout", le.keyboard == "EU")

    # ---- spec-aware benchmarks ----
    ls = pricing.Listing("es", "ebay_uk", "MacBook Pro 14 M4 Pro 48GB 2TB", 2100,
                         currency="GBP")
    pricing.parse_listing_specs(ls)
    pricing.match_model(ls, cfg["models"], cfg)
    base = next(m for m in cfg["models"] if m["id"] == "m4pro-14")
    sa = cfg["value"]["spec_adjustments"]
    exp_adj = (48 - 24) / 8 * sa["ram_per_8gb_gbp"] + sa["ssd_gbp"][2048]
    check(f"48GB/2TB benchmark spec-adjusted (+£{ls.spec_adj_gbp:.0f})",
          abs(ls.spec_adj_gbp - exp_adj) < 0.01
          and abs(ls.uk_avg_gbp - (base["uk_avg_gbp"] + exp_adj)) < 0.01)
    lb = pricing.Listing("eb", "ebay_uk", "MacBook Pro 14 M4 Pro 24GB 512GB", 1400,
                         currency="GBP")
    pricing.parse_listing_specs(lb)
    pricing.match_model(lb, cfg["models"], cfg)
    check("base-spec listing unadjusted", lb.spec_adj_gbp == 0
          and lb.uk_avg_gbp == base["uk_avg_gbp"])
    _r = {"JPY": 195.0, "USD": 1.30}
    l9 = pricing.Listing("m9", "mercari", "MacBook Pro 14 M4 Pro 極美品 充放電回数45回",
                         180000, grade="personal")
    pricing.parse_listing_specs(l9)
    pricing.match_model(l9, cfg["models"])
    pricing.score(l9, cfg, _r)
    check("45 cycles OK for personal tier (no flag)",
          not any("battery cycles" in f for f in l9.flags))
    l10 = pricing.Listing("m10", "mercari", "MacBook Pro 14 M4 Pro 未使用 充放電回数45回",
                          180000, grade="resale")
    pricing.parse_listing_specs(l10)
    pricing.match_model(l10, cfg["models"])
    pricing.score(l10, cfg, _r)
    check("45 cycles flagged for resale tier",
          any("battery cycles" in f for f in l10.flags))

    l6 = pricing.Listing("m7", "mercari", "MacBook Pro M4 Pro 12C CPU 16C GPU 24GB 1TB", 1)
    pricing.parse_listing_specs(l6)
    check('core counts not mistaken for 16-inch (size guessed 14")',
          l6.size == 14 and l6.size_guessed)

    l6b = pricing.Listing("e1", "ebay_us",
                          "2024 MacBook M4 Pro, 12‑core CPU, 16‑coreGPU 14.2\"", 1,
                          currency="USD")
    pricing.parse_listing_specs(l6b)
    check("unicode-hyphen core counts ignored, real 14.2 size kept",
          l6b.size == 14 and not l6b.size_guessed)

    # ---- the 2026 wide-scope families ----
    fam_cases = [
        ("Mac Studio M1 Ultra 64GB 1TB 新品未開封", "mac_studio", "M1 ULTRA", "studio-m1ultra"),
        ("Apple Mac Studio M4 Max 36GB 512GB sealed", "mac_studio", "M4 MAX", "studio-m4max"),
        ("Mac mini M4 Pro 24GB 512GB NEU", "mac_mini", "M4 PRO", "mini-m4pro"),
        ("Apple Mac mini M2 8GB 256GB 新品", "mac_mini", "M2", "mini-m2"),
        ("iMac 24インチ M4 16GB 256GB ブルー", "imac", "M4", "imac-m4"),
        ("Apple Mac Pro M2 Ultra Tower 64GB 1TB", "mac_pro", "M2 ULTRA", "macpro-m2ultra"),
        ("Apple Studio Display 27インチ 標準ガラス", "display", "", "studio-display"),
        ("iPad Pro 11インチ 第4世代 128GB Wi-Fi 未使用", "ipad_pro", "M2", "ipadpro-m2-11"),
        ("iPad Pro 12.9 M2 256GB Space Gray", "ipad_pro", "M2", "ipadpro-m2-129"),
        ("iPad Pro 13インチ M4 256GB", "ipad_pro", "M4", "ipadpro-m4-13"),
        ("iPad Air 13-inch M4 128GB Blue NEW", "ipad_air", "M4", "ipadair-m4-13"),
    ]
    for title, fam, chip, mid in fam_cases:
        lf = pricing.Listing("t", "mercari", title, 1)
        pricing.parse_listing_specs(lf)
        pricing.match_model(lf, cfg["models"], cfg)
        check(f"{fam}: '{title[:36]}...' -> {mid}",
              lf.family == fam and lf.chip == chip and lf.model_id == mid)
    for title in ("iPad mini 7 A17 Pro 128GB 新品",     # no tracked family
                  "iPad 第10世代 64GB",                  # base iPad
                  "iMac 24 M1 2021 8GB",                 # pre-2022 chip
                  "Mac Studio 2027 M9 Hyper"):           # unknown chip
        lf = pricing.Listing("t", "mercari", title, 1)
        pricing.parse_listing_specs(lf)
        check(f"out of scope: '{title[:30]}'", lf.family == "" or lf.model_id is None)
    # ---- accessory-vs-bundle detection (titles that leaked in live scans) --
    for title in (
            "Apple 11インチiPad Pro（M5）用Magic Keyboard - 日本語 - ブラック",
            "【新品】Apple 13インチiPad Air(M2)用Magic Keyboard - 日本語",
            "IPORT 非接触充電プロテクトケース 13インチiPad Air (M3 (2025年モデル)) 対応",
            "Bosstab E06 ELITE EVO FLOOR STAND BLACK IPAD PRO 13IN M4",
            "Logitech Combo Touch iPad Air 13-inch (M2)(2024) Keyboard Case"):
        la = pricing.Listing("t", "mercari", title, 60000)
        pricing.parse_listing_specs(la)
        check(f"accessory rejected: '{title[:44]}'", la.family == "")
    for title in (
            "iPad Pro 12.9 第6世代 128GB／Apple Pencil付",
            "iPad Pro 本体 第6世代 M2 ケース ガラスフィルム 付き",
            "iPad Pro 13 M4 256GB + Magic Keyboard セット 新品",
            "iPad Air 13 M2 128GB with Smart Folio included"):
        lb = pricing.Listing("t", "mercari", title, 90000)
        pricing.parse_listing_specs(lb)
        check(f"bundle kept: '{title[:44]}'", lb.family in ("ipad_pro", "ipad_air"))
    lmp = pricing.Listing("t", "ebay_uk", "Apple Mac Pro M2 Ultra", 3000, currency="GBP")
    pricing.parse_listing_specs(lmp)
    check("'Mac Pro' not confused with 'MacBook Pro'", lmp.family == "mac_pro")
    check("keyboardless family gets keyboard n/a", lmp.keyboard == "n/a")
    check("regional bars split by keyboard: mini JP 42 / macbook JP 50",
          pricing.alert_thresholds(cfg, "mercari", "mac_mini")["min"] == 42
          and pricing.alert_thresholds(cfg, "mercari", "macbook")["min"] == 50)
    check("EU bars: macbook 38 / keyboardless 35",
          pricing.alert_thresholds(cfg, "ebay_de", "macbook")["min"] == 38
          and pricing.alert_thresholds(cfg, "ebay_de", "ipad_pro")["min"] == 35)
    check("wanted-ad filter (classifieds)",
          pricing.is_wanted_ad("WANTED MACBOOK PRO 16 CASH TODAY")
          and pricing.is_wanted_ad("We Buy MacBooks & iPads")
          and not pricing.is_wanted_ad("Unwanted gift: sealed Mac mini M4"))
    check("classifieds grading: sealed->resale, like-new->personal, else None",
          sources._en_grade("New Sealed Mac Studio M4 Max") == "resale"
          and sources._en_grade("iPad Pro 13 M4 - like new, boxed") == "personal"
          and sources._en_grade("Mac mini M4 good condition") is None)
    check("eBay.de price format parsed (EUR 1.234,56)",
          sources._first_price_in("EUR 1.234,56", "EUR") == 1234.56)
    nv = len(sources._buyee_variants(cfg))
    check(f"Buyee search count consolidated ({nv} searches/source, was 42)",
          nv <= 34)
    check("classifieds use one broad query per family",
          len(sources._broad_queries(cfg)) == 8)

    rates = {"JPY": 195.0, "USD": 1.30, "GBP": 1.0, "EUR": 1.17}

    # EU landed-cost maths: €999 Mac mini from eBay DE
    cost_eu = pricing.landed_cost_gbp(999, "ebay_de", cfg, rates, "mac_mini")
    exp_eu = (999 + cfg["costs"]["eu_shipping_eur_family"]["mac_mini"]) / 1.17 * 1.20 + 12
    check(f"EU landed cost maths (£{cost_eu:.2f})", abs(cost_eu - exp_eu) < 0.01)
    # family-aware shipping: an iPad from JP ships cheaper than a display
    cost_ipad = pricing.landed_cost_gbp(100000, "mercari", cfg, rates, "ipad_pro")
    cost_disp = pricing.landed_cost_gbp(100000, "mercari", cfg, rates, "display")
    check("family shipping: iPad < display on the same JP price",
          cost_ipad < cost_disp)
    # like-new flip target: personal-grade stock sells at like-new money
    lpf = pricing.Listing("t", "ebay_uk", "Apple Mac mini M4 Pro 24GB 512GB - mint, as new",
                          800, currency="GBP", grade="personal")
    pricing.parse_listing_specs(lpf)
    pricing.match_model(lpf, cfg["models"], cfg)
    pricing.score(lpf, cfg, rates)
    check("personal-grade flip targets like-new price (below UK-new avg)",
          0 < lpf.flip_target_gbp < lpf.uk_avg_gbp and lpf.flip_profit_gbp != 0)

    # landed-cost maths: ¥218,000 mercari at 195 JPY/GBP, defaults
    cost = pricing.landed_cost_gbp(218000, "mercari", cfg, rates)
    expect = (218000 + 800 + 0 + 8000) / 195.0 * 1.20 + 12
    check(f"JP landed cost maths (£{cost:.2f})", abs(cost - expect) < 0.01)

    # landed-cost maths: $1,449 eBay US at 1.30 USD/GBP, defaults
    c = cfg["costs"]
    cost_us = pricing.landed_cost_gbp(1449, "ebay_us", cfg, rates)
    expect_us = ((1449 * (1 + c["us_sales_tax_pct"] / 100.0)
                  + c["us_forwarder_fee_usd"] + c["us_domestic_shipping_usd"]
                  + c["us_intl_shipping_usd"]) / 1.30 * 1.20 + 12)
    check(f"US landed cost maths (£{cost_us:.2f})", abs(cost_us - expect_us) < 0.01)

    # English (eBay US) titles parse with the same spec detector
    l7 = pricing.Listing("123456789012", "ebay_us",
                         "Apple MacBook Pro 14-inch M4 Pro 24GB RAM 512GB SSD NEW",
                         1449, currency="USD")
    l7.keyboard = "US"
    pricing.parse_listing_specs(l7)
    check("eBay US title parsed (M4 PRO 14, 24/512)",
          l7.chip == "M4 PRO" and l7.size == 14
          and l7.ram_gb == 24 and l7.storage_gb == 512)
    check("US keyboard preset survives spec parsing", l7.keyboard == "US")
    pricing.match_model(l7, cfg["models"])
    pricing.score(l7, cfg, rates)
    check("US listing scored without JIS flag",
          not any("JIS" in f for f in l7.flags))
    friction = float(cfg.get("resale", {}).get("sell_friction_pct", 5))
    exp_profit = round(l7.uk_avg_gbp * (1 - friction / 100.0) - l7.landed_gbp, 2)
    check(f"flip profit computed (£{l7.flip_profit_gbp:.0f})",
          abs(l7.flip_profit_gbp - exp_profit) < 0.01)
    msg = report.whatsapp_message(l7, cfg)
    check("whatsapp alert renders (bold model, link, no HTML)",
          "*MacBook Pro" in msg and "ebay.com/itm/" in msg and "<b>" not in msg)

    # Swappa synthetic titles parse with the same detector, same US cost route
    l8 = pricing.Listing("LACW00000", "swappa",
                         "MacBook Pro 16 M3 Max 1TB 36GB - Mint (Swappa)",
                         2499, currency="USD", condition="Mint")
    l8.keyboard = "US"
    pricing.parse_listing_specs(l8)
    check("Swappa title parsed (M3 MAX 16, 36GB/1TB)",
          l8.chip == "M3 MAX" and l8.size == 16
          and l8.ram_gb == 36 and l8.storage_gb == 1024)
    check("Swappa uses the US landed-cost route",
          pricing.landed_cost_gbp(2499, "swappa", cfg, rates)
          == pricing.landed_cost_gbp(2499, "ebay_us", cfg, rates))
    check("Swappa purchase link", l8.market_links[0][0] == "Swappa"
          and "swappa.com/listing/view/LACW00000" in l8.market_links[0][1])

    # ---- best-value engine ----
    check("UK landed cost = price + postage buffer, no VAT",
          pricing.landed_cost_gbp(1000, "ebay_uk", cfg, rates)
          == 1000 + cfg["costs"]["uk_domestic_shipping_gbp"])
    lv = pricing.Listing("456789012345", "ebay_uk",
                         "Apple MacBook Pro 14 M4 Pro 24GB 512GB", 1275,
                         currency="GBP", condition="Used", grade="good")
    pricing.parse_listing_specs(lv)
    pricing.match_model(lv, cfg["models"])
    check("eBay UK listing matched", lv.model_id == "m4pro-14")
    vf = cfg["value"]
    fair_good = pricing.fair_value_gbp(lv, cfg)
    exp_good = (lv.uk_used_gbp if lv.uk_used_gbp
                else round(lv.uk_avg_gbp * vf["good_factor"], 2))
    check(f"fair value for 'good' condition (£{fair_good:.0f})",
          abs(fair_good - exp_good) < 0.01)
    lv.grade = "personal"
    fair_ln = pricing.fair_value_gbp(lv, cfg)
    exp_ln = (round((lv.uk_avg_gbp + lv.uk_used_gbp) / 2, 2) if lv.uk_used_gbp
              else round(lv.uk_avg_gbp * vf["like_new_factor"], 2))
    check(f"fair value for like-new condition (£{fair_ln:.0f})",
          abs(fair_ln - exp_ln) < 0.01)
    lv.grade = "resale"
    check("fair value for new = UK average",
          pricing.fair_value_gbp(lv, cfg) == lv.uk_avg_gbp)
    wear = pricing.battery_wear_gbp(600, cfg)
    exp_wear = round(600 / vf["battery_cycle_rating"]
                     * vf["battery_replacement_gbp"], 2)
    check(f"600-cycle battery wear costed (£{wear:.0f})",
          abs(wear - exp_wear) < 0.01)
    check("battery wear negligible under 60 cycles",
          pricing.battery_wear_gbp(40, cfg) < 15)
    lc = pricing.Listing("x1", "ebay_uk", "MacBook Pro 14 M4 Pro", 1000,
                         currency="GBP", grade="good")
    lc.uk_avg_gbp, lc.uk_used_gbp = 1000.0, 990.0   # spec-mix-inflated data
    check("used median clamped below the new benchmark",
          pricing.fair_value_gbp(lc, cfg) == 920.0)
    lv.grade = "good"
    lv.cycles = 600
    pricing.score(lv, cfg, rates)
    pricing.value_score(lv, cfg)
    check("value score computed (landed + wear vs fair value)",
          lv.value_landed_gbp == round(lv.landed_gbp + wear, 2)
          and lv.fair_gbp == fair_good and lv.value_pct != 0)

    mdl = next(m for m in cfg["models"] if m["id"] == "m4pro-14")
    l.uk_avg_gbp = mdl["uk_avg_gbp"]
    pricing.score(l, cfg, rates)
    check("savings % computed", l.savings_pct != 0)

    print("\nAll good!" if ok else "\nSome checks FAILED - tell Claude the output above.")
    return 0 if ok else 1


# ----------------------------------------------------------------------------

def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="MacBook Pro deal scanner (Japan + US + UK)")
    p.add_argument("command", choices=["scan", "watch", "background", "ukprices",
                                       "test-whatsapp", "selftest"])
    p.add_argument("action", nargs="?", choices=["start", "stop", "status"],
                   help="for the background command: start / stop / status")
    p.add_argument("--no-alert", action="store_true", help="scan without sending WhatsApp alerts")
    p.add_argument("--debug", action="store_true", help="save raw pages for troubleshooting")
    p.add_argument("--demo", action="store_true", help="use built-in fake listings (no internet)")
    p.add_argument("--csv", metavar="FILE", help="also export results to a CSV file")
    p.add_argument("--interval", type=int, help="watch-mode minutes between scans")
    p.add_argument("--write", action="store_true", help="ukprices: save medians into config.yaml")
    p.add_argument("--sources", metavar="LIST",
                   help="scan only these sources this run, e.g. mercari,ebay_us")
    args = p.parse_args()

    if args.command == "selftest":
        return run_selftest()

    if args.command == "background":
        return run_background(args.action or "status")

    cfg = load_config()
    if args.sources:
        cfg["scan"]["sources"] = [x.strip() for x in args.sources.split(",") if x.strip()]

    if args.command == "test-whatsapp":
        ok = store.whatsapp_send(cfg, "✅ MacBook deal bot can reach your WhatsApp. You're all set!")
        print("Sent!" if ok else "Failed - check whatsapp settings in config.yaml "
                                 "(enabled: true, phone, apikey).")
        return 0 if ok else 1

    if args.command == "scan":
        run_scan(cfg, send_alerts=not args.no_alert, debug=args.debug,
                 demo=args.demo, csv_path=args.csv)
        return 0

    if args.command == "watch":
        run_watch(cfg, args.interval, args.debug)
        return 0

    if args.command == "ukprices":
        run_ukprices(cfg, write=args.write, debug=args.debug)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
