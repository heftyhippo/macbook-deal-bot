#!/usr/bin/env python3
"""
macdeals.py - multi-market Apple product deal scanner. It compares the full
landed cost with a condition/spec/layout-aware expected UK price and ranks all
buyable results with one savings metric.

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
import os
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
LOCAL_CONFIG_FILE = "config.local.yaml"


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Local secrets live in an ignored override file so config.yaml is safe to
    # publish with the scanner. Only the WhatsApp block is accepted here;
    # product/search settings keep one source of truth in config.yaml.
    if os.path.exists(LOCAL_CONFIG_FILE):
        with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        if isinstance(local.get("whatsapp"), dict):
            cfg.setdefault("whatsapp", {}).update(local["whatsapp"])
    # Environment variables override config.yaml. This is how the free cloud
    # runner (GitHub Actions) injects your WhatsApp secrets WITHOUT them ever
    # being written into a file in the repo. Locally, no env vars are set, so
    # your config.yaml values are used as normal.
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
                  "gumtree": sources.fetch_gumtree_cycles,
                  "swappa": sources.fetch_swappa_cycles}


def run_scan(cfg: dict, send_alerts: bool, debug: bool, demo: bool,
             csv_path: str | None) -> None:
    t0 = datetime.now().strftime("%H:%M:%S")
    print(f"[{t0}] scanning...")

    rates, fx_note = pricing.get_fx(cfg["fx"])
    a = cfg["alerts"]
    # One condition/spec/layout-aware savings metric drives every output.
    global_min = pricing.global_min_alert_pct(cfg)
    budget = int(cfg["scan"]["max_detail_fetch"])
    per_source_detail = int(cfg["scan"].get("max_detail_fetch_per_source", 3))
    matched: list[pricing.Listing] = []
    seen_keys: set[tuple[str, str]] = set()
    fresh_sources: set[str] = set()
    tracked_families = {m.get("family", "macbook") for m in cfg["models"]}
    allowed_snapshot_sources = set(cfg["scan"].get(
        "snapshot_sources", cfg["scan"]["sources"]))
    sent = 0

    def process_batch(batch: list[pricing.Listing], source: str) -> list[pricing.Listing]:
        """Filter, score, cycle-enrich and (immediately) alert one source's
        listings, then return its last-good snapshot."""
        nonlocal budget, sent
        out: list[pricing.Listing] = []
        for l in batch:
            searchable = " ".join(x for x in (l.title, l.description) if x)
            if pricing.is_excluded(searchable, cfg["filters"]["exclude_keywords"]):
                continue
            pricing.parse_listing_specs(l)
            if not l.family:
                continue      # not a tracked product (or pre-2022 / an Air)
            if not pricing.is_complete_apple_product(l.title, l.family):
                continue      # accessory, replacement part or component
            pricing.match_model(l, cfg["models"], cfg)
            if not l.model_id:
                continue
            # Distinct sellers frequently reuse catalog titles and the same
            # round price. Only an actual marketplace identity is a duplicate.
            key = (l.source, l.item_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if not demo:
                store.upsert_seen(l.item_id, l.source, l.title, int(l.price))
            pricing.score(l, cfg, rates)
            if any("NOT A WHOLE PRODUCT" in f for f in l.flags):
                continue
            out.append(l)
        out.sort(key=lambda x: x.savings_pct, reverse=True)
        # enrich the most promising MacBooks with battery-cycle info from the
        # listing page (only MacBooks have a battery worth checking) -
        # "promising" = within 8 points of its own region's alert bar
        used_here = 0
        for l in out:
            if budget <= 0 or used_here >= per_source_detail or demo:
                break
            if l.savings_pct < pricing.alert_thresholds(
                    cfg, l.source, l.family)["min"] - 8:
                break         # sorted by savings - the rest are further away
            if l.family != "macbook" or l.cycles is not None:
                continue
            l.cycles = CYCLE_FETCHERS[l.source](l.item_id)
            budget -= 1
            used_here += 1
            time.sleep(1.0)
            # score() is idempotent and preserves parser/source facts.
            pricing.score(l, cfg, rates)
        out.sort(key=lambda x: x.savings_pct, reverse=True)
        # alert NOW - great deals last minutes, not scan-lengths
        if send_alerts:
            for l in out:
                if l.savings_pct < global_min:
                    break
                if l.is_auction:
                    continue  # a current bid is not a price you can pay
                if l.savings_pct < pricing.alert_thresholds(
                        cfg, l.source, l.family)["min"]:
                    continue
                if l.listing_confidence < float(a.get("min_listing_confidence", 0.55)):
                    continue
                if l.benchmark_confidence < float(a.get("min_benchmark_confidence", 0.50)):
                    continue
                if any("SUSPICIOUSLY LOW" in f or "TOO-GOOD" in f
                       for f in l.flags):
                    continue
                if (l.family == "macbook" and l.cycles is not None
                        and l.cycles > pricing.max_cycles_for(l.grade, cfg)):
                    continue
                if not store.should_alert(l.source, l.item_id, int(l.price),
                                          a["realert_drop_pct"]):
                    continue
                if store.whatsapp_send(cfg, report.whatsapp_message(l, cfg)):
                    store.mark_alerted(l.source, l.item_id, int(l.price))
                    sent += 1
        matched.extend(out)
        return out

    if demo:
        process_batch(demo_listings(), "demo")
    else:
        try:
            for src, label, scanner in SCANNERS:
                if src not in cfg["scan"]["sources"]:
                    continue
                try:
                    raw = scanner(cfg, debug)
                    print(f"  {label}: {len(raw)} source candidates")
                    processed = process_batch(raw, src)
                    if processed and store.save_source_snapshot(src, processed):
                        fresh_sources.add(src)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # One marketplace changing markup must never prevent every
                    # later source from being checked.
                    print(f"  [{src}] scanner error - keeping last good results: {e}")
        finally:
            # don't leave the Swappa Chrome open after an error or interruption
            sources.swappa_release()

        allowed = cfg["scan"].get("snapshot_sources", cfg["scan"]["sources"])
        cached = store.load_source_snapshots(
            allowed, fresh_sources,
            max_age_minutes=float(cfg["scan"].get("snapshot_max_age_minutes", 360)))
        for l in cached:
            searchable = " ".join(x for x in (l.title, l.description) if x)
            if (l.source not in allowed_snapshot_sources
                    or pricing.is_excluded(
                        searchable, cfg["filters"]["exclude_keywords"])):
                continue
            # Re-run current identity and pricing rules. Otherwise a result
            # accepted by an older release (an iPad or an SSD enclosure, for
            # example) could remain visible until its snapshot expired.
            pricing.parse_listing_specs(l)
            if (l.family not in tracked_families
                    or not pricing.is_complete_apple_product(l.title, l.family)):
                continue
            pricing.match_model(l, cfg["models"], cfg)
            if not l.model_id:
                continue
            pricing.score(l, cfg, rates)
            key = (l.source, l.item_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            matched.append(l)

    store.prune_stale(90)
    matched.sort(key=lambda x: x.savings_pct, reverse=True)

    max_cycles = int(cfg.get("value", {}).get("max_battery_cycles", 800))
    deals = [l for l in matched
             if not l.is_auction and l.expected_price_gbp > 0
             and not any("NOT A WHOLE PRODUCT" in f for f in l.flags)
             and not (l.family == "macbook" and l.cycles is not None
                      and l.cycles > max_cycles)]
    deals.sort(key=lambda x: (x.savings_pct, x.overall_score), reverse=True)
    deals = deals[:int(cfg.get("value", {}).get("top_n", 250))]
    snapshot_meta = ({} if demo else
                     store.get_snapshot_metadata(cfg["scan"].get(
                         "snapshot_sources", cfg["scan"]["sources"])))
    report.console_table(deals[:40], [], rates, fx_note, cfg)
    report.write_html(deals, [], "deals.html", rates, cfg,
                      source_meta=snapshot_meta)
    counts = {g: sum(1 for l in deals if l.grade == g)
              for g in ("resale", "personal", "good")}
    cached_n = sum(1 for l in deals if l.snapshot_age_minutes)
    print(f"  wrote deals.html ({len(deals)} ranked deals: "
          f"{counts['resale']} new, {counts['personal']} like-new, "
          f"{counts['good']} used; {cached_n} retained from last-good scans)")
    if csv_path:
        report.write_csv(deals, csv_path)
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
    # A quick pass refreshes only these sources, but the report merges their
    # fresh results with every other source's last successful snapshot.
    fast_cfg["scan"]["snapshot_sources"] = list(s["sources"])

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
        cur = float(mdl["uk_avg_gbp"])
        med_new, n1 = sources.ebay_uk_sold_median(
            mdl["ebay_query"], debug=debug,
            plausible_min=max(50, cur * 0.30), plausible_max=cur * 1.80)
        time.sleep(2.5)
        med_used, n2 = sources.ebay_uk_sold_median(
            mdl["ebay_query"], debug=debug, conditions="3000",
            plausible_min=max(50, cur * 0.20), plausible_max=cur * 1.25)
        time.sleep(2.5)
        # medians from fewer than 10 sales are too noisy to overwrite with
        if med_new and n1 < 10:
            med_new = None
        if med_used and n2 < 10:
            med_used = None
        # Do not write a spec-mixed/inverted condition ladder back to config.
        # expected_price_details() also defends at runtime, but preventing bad
        # benchmark data at ingestion keeps the source of truth honest.
        compare_new = float(med_new or cur)
        if med_used and not compare_new * 0.50 <= med_used <= compare_new * 0.95:
            med_used = None
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
        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
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
        L("m22222222222", "mercari", "MacBook Pro 16インチ M3 Pro 18GB/512GB 未使用に近い 充放電回数3回 US配列", 195000, condition="未使用に近い", grade="personal"),
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
        # the wider 2026 scope: desktops and displays
        L("m88888888888", "mercari", "【新品未使用】Mac Studio M1 Max 32GB 512GB", 75000, condition="新品、未使用"),
        L("x3456789012", "yahoo", "iMac 24インチ M4 16GB 256GB ブルー 新品未開封", 120000, condition="未使用"),
        L("567890123456", "ebay_de", "Apple Mac mini M4 16GB 256GB - NEU versiegelt", 399, currency="EUR", condition="Brand New"),
        L("1800109157", "gumtree", "Apple Studio Display 27 inch - brand new, still boxed", 600, currency="GBP", condition="seller says new/sealed"),
        L("m99999999999", "mercari", "Mac mini M4 Pro 24GB 512GB 未使用に近い", 138000, condition="未使用に近い", grade="personal"),
        L("1800229001", "gumtree", "MacBook Pro 14 M3 Pro 18GB 1TB", 825,
          currency="GBP", condition="used / seller description", grade="good",
          description="Excellent working order, 91% battery health, 118 cycles, no scratches or faults.",
          location="Manchester"),
    ]
    for l in demo:
        if l.source in ("ebay_us", "swappa"):
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
    check("33 Mac/display models defined", len(cfg["models"]) == 33)
    check("iPads and Craigslist removed from active scope",
          not any(str(m.get("family", "")).startswith("ipad")
                  for m in cfg["models"])
          and not any(str(q.get("family", "")).startswith("ipad")
                      for q in cfg["scan"]["queries"])
          and "craigslist" not in cfg["scan"]["sources"]
          and not any(src == "craigslist" for src, _, _ in SCANNERS))

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
    check("16GB not mistaken for a screen size (size left unconfirmed)",
          l5.size is None and l5.size_guessed)

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
    check("one condition-aware alert bar across every market",
          pricing.alert_thresholds(cfg, "ebay_uk")["min"] == 35
          and pricing.alert_thresholds(cfg, "ebay_us")["min"] == 35
          and pricing.alert_thresholds(cfg, "swappa")["min"] == 35
          and pricing.alert_thresholds(cfg, "mercari")["min"] == 35
          and pricing.alert_thresholds(cfg, "yahoo")["min"] == 35)
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
    lh = pricing.Listing("eh", "ebay_uk",
                         "Mac Studio M3 Ultra 512GB unified memory 16TB SSD", 5000,
                         currency="GBP", grade="good")
    pricing.parse_listing_specs(lh)
    pricing.match_model(lh, cfg["models"], cfg)
    check("current 512GB-memory / 16TB Studio configuration parsed",
          lh.ram_gb == 512 and lh.storage_gb == 16384)
    check("high-capacity SSD can never create a negative spec adjustment",
          lh.spec_adj_gbp > 0)
    l8tb = pricing.Listing("e8", "ebay_uk",
                           "MacBook Pro 14 M5 Max 48GB RAM 8TB SSD", 4000,
                           currency="GBP")
    pricing.parse_listing_specs(l8tb)
    pricing.match_model(l8tb, cfg["models"], cfg)
    check("8TB MacBook benchmark receives a positive premium",
          l8tb.spec_adj_gbp > 0)
    lu = pricing.Listing("eu", "ebay_uk", "MacBook Pro M2 Pro 16GB 512GB", 700,
                         currency="GBP")
    pricing.parse_listing_specs(lu)
    pricing.match_model(lu, cfg["models"], cfg)
    check("unknown size chooses the lowest compatible benchmark",
          lu.size is None and lu.model_id == "m2pro-16"
          and lu.uk_avg_gbp == 912)
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
    check("core counts not mistaken for a screen size",
          l6.size is None and l6.size_guessed)

    l6b = pricing.Listing("e1", "ebay_us",
                          "2024 MacBook M4 Pro, 12‑core CPU, 16‑coreGPU 14.2\"", 1,
                          currency="USD")
    pricing.parse_listing_specs(l6b)
    check("unicode-hyphen core counts ignored, real 14.2 size kept",
          l6b.size == 14 and not l6b.size_guessed)

    # ---- tracked Mac/display families ----
    fam_cases = [
        ("Mac Studio M1 Ultra 64GB 1TB 新品未開封", "mac_studio", "M1 ULTRA", "studio-m1ultra"),
        ("Apple Mac Studio M4 Max 36GB 512GB sealed", "mac_studio", "M4 MAX", "studio-m4max"),
        ("Mac mini M4 Pro 24GB 512GB NEU", "mac_mini", "M4 PRO", "mini-m4pro"),
        ("Apple Mac mini M2 8GB 256GB 新品", "mac_mini", "M2", "mini-m2"),
        ("iMac 24インチ M4 16GB 256GB ブルー", "imac", "M4", "imac-m4"),
        ("Apple Mac Pro M2 Ultra Tower 64GB 1TB", "mac_pro", "M2 ULTRA", "macpro-m2ultra"),
        ("Apple Studio Display 27インチ 標準ガラス", "display", "", "studio-display"),
    ]
    for title, fam, chip, mid in fam_cases:
        lf = pricing.Listing("t", "mercari", title, 1)
        pricing.parse_listing_specs(lf)
        pricing.match_model(lf, cfg["models"], cfg)
        check(f"{fam}: '{title[:36]}...' -> {mid}",
              lf.family == fam and lf.chip == chip and lf.model_id == mid)
    for title in ("iPad Pro 13 M4 256GB 新品",         # iPads removed
                  "iPad Air 13 M4 128GB 新品",
                  "iPad mini 7 A17 Pro 128GB 新品",
                  "iPad 第10世代 64GB",
                  "iMac 24 M1 2021 8GB",                 # pre-2022 chip
                  "Mac Studio 2027 M9 Hyper"):           # unknown chip
        lf = pricing.Listing("t", "mercari", title, 1)
        pricing.parse_listing_specs(lf)
        check(f"out of scope: '{title[:30]}'", lf.family == "" or lf.model_id is None)
    # ---- accessory-vs-bundle detection (titles that leaked in live scans) --
    for title in (
            "Mac mini M4 aluminium dock enclosure with NVMe SSD slot",
            "MacMini M4 拡張タワー ケース 8TB対応 SSD外付け 10Gbps",
            "M4 Macmini 内蔵SSD拡張カード 2TB",
            "Genuine Apple A2525 27 Studio Display Rear Shell Chassis Case Housing",
            "MacBook Pro 14 M4 Pro display assembly replacement panel"):
        la = pricing.Listing("t", "mercari", title, 60000)
        pricing.parse_listing_specs(la)
        check(f"accessory rejected: '{title[:44]}'", la.family == "")
    too_cheap = pricing.Listing(
        "cheap-part", "ebay_uk", "Genuine Apple Mac Mini M4 Pro 2TB SSD",
        415, currency="GBP", condition="Used", grade="good")
    pricing.parse_listing_specs(too_cheap)
    pricing.match_model(too_cheap, cfg["models"], cfg)
    pricing.score(too_cheap, cfg, {"JPY": 195.0, "USD": 1.30,
                                   "GBP": 1.0, "EUR": 1.17})
    check("implausibly cheap whole-device claim excluded from ranking",
          any("NOT A WHOLE PRODUCT" in f for f in too_cheap.flags))
    bad_capacity = pricing.Listing(
        "bad-cap", "mercari",
        "MacBook Pro 2023 M3 RAM:16GB SSD:5126GB 14.2インチ", 1)
    pricing.parse_listing_specs(bad_capacity)
    check("malformed 5126GB SSD claim ignored",
          bad_capacity.storage_gb is None)
    for title in (
            "Mac mini M4 16GB 256GB with dock included",
            "Apple Studio Display with height-adjustable stand"):
        lb = pricing.Listing("t", "mercari", title, 90000)
        pricing.parse_listing_specs(lb)
        check(f"bundle kept: '{title[:44]}'", bool(lb.family))
    lmp = pricing.Listing("t", "ebay_uk", "Apple Mac Pro M2 Ultra", 3000, currency="GBP")
    pricing.parse_listing_specs(lmp)
    check("'Mac Pro' not confused with 'MacBook Pro'", lmp.family == "mac_pro")
    check("keyboardless family gets keyboard n/a", lmp.keyboard == "n/a")
    check("keyboard differences are priced, not hidden in alert bars",
          pricing.alert_thresholds(cfg, "mercari", "mac_mini")["min"] == 35
          and pricing.alert_thresholds(cfg, "mercari", "macbook")["min"] == 35)
    check("EU products use the same transparent alert bar",
          pricing.alert_thresholds(cfg, "ebay_de", "macbook")["min"] == 35)
    check("wanted-ad filter (classifieds)",
          pricing.is_wanted_ad("WANTED MACBOOK PRO 16 CASH TODAY")
          and pricing.is_wanted_ad("We Buy MacBooks and iMacs")
          and not pricing.is_wanted_ad("Unwanted gift: sealed Mac mini M4"))
    check("classifieds grading: sealed->resale, like-new->personal, else None",
          sources._en_grade("New Sealed Mac Studio M4 Max") == "resale"
          and sources._en_grade("Mac Studio M4 Max - like new, boxed") == "personal"
          and sources._en_grade("Mac mini M4 good condition") is None)
    gumtree_fixture = """
      <html><head><link rel="next" href="/search?page=2"></head><body>
      <a data-q="search-result-anchor"
         href="/p/macs/mac-mini-m4/1800123456?tracking=featured">
        <div data-q="tile-title">Apple Mac mini M4 16GB 256GB</div>
        <div data-q="tile-description">Excellent working order. RRP £599.</div>
        <div data-q="tile-location">Leeds</div>
        <div data-q="tile-price">£225</div>
      </a>
      <a data-q="search-result-anchor" href="/p/macs/broken/1800654321">
        <div data-q="tile-title">Apple Mac mini M4 16GB 256GB</div>
        <div data-q="tile-description">Faulty, MDM locked, for parts.</div>
        <div data-q="tile-price">£200</div>
      </a></body></html>"""
    gx, gn, tiles = sources._parse_gumtree_page(
        gumtree_fixture, cfg, "https://www.gumtree.com/search",
        {"mac_mini"})
    check("Gumtree uses exact price/description and query-string IDs",
          tiles == 2 and len(gx) == 1 and gx[0].item_id == "1800123456"
          and gx[0].price == 225 and gx[0].grade == "good")
    check("Gumtree pagination is captured", bool(gn and "page=2" in gn))
    feeds = sources._gumtree_feed_specs(cfg)
    covered = set().union(*(f["families"] for f in feeds))
    check("two Gumtree feeds cover all six tracked families",
          len(feeds) == 2 and covered == {fam for _, fam in sources.scan_queries(cfg)})
    check("eBay.de price format parsed (EUR 1.234,56)",
          sources._first_price_in("EUR 1.234,56", "EUR") == 1234.56)
    nv = len(sources._buyee_variants(cfg))
    check(f"Buyee search count consolidated ({nv} searches/source, was 42)",
          nv <= 34)
    check("Gumtree uses one broad query per tracked family",
          len(sources._broad_queries(cfg)) == 6)

    rates = {"JPY": 195.0, "USD": 1.30, "GBP": 1.0, "EUR": 1.17}

    # ---- one expected-price / savings / confidence model ----
    variants = []
    for grade, cond in (("resale", "Brand New"), ("personal", "Like New"),
                        ("good", "Used")):
        x = pricing.Listing("v" + grade, "ebay_uk",
                            "MacBook Pro 14 M4 Pro 24GB RAM 512GB SSD",
                            900, currency="GBP", condition=cond, grade=grade)
        x.keyboard = "UK"
        pricing.parse_listing_specs(x)
        pricing.match_model(x, cfg["models"], cfg)
        pricing.score(x, cfg, rates)
        variants.append(x)
    check("expected price descends new > like-new > used",
          variants[0].expected_price_gbp > variants[1].expected_price_gbp
          > variants[2].expected_price_gbp)
    check("headline saving uses expected price for this condition",
          variants[2].savings_gbp == round(
              variants[2].expected_price_gbp - variants[2].landed_gbp, 2)
          and variants[2].value_pct == variants[2].savings_pct)
    check("overall score is bounded and confidence-weighted",
          0 <= variants[0].overall_score <= 100
          and variants[0].benchmark_confidence > 0
          and variants[0].listing_confidence > 0)
    jis = pricing.Listing("vj", "mercari",
                          "MacBook Pro 14 M4 Pro 24GB 512GB JIS", 180000,
                          grade="resale")
    jis.keyboard = "JIS"
    pricing.parse_listing_specs(jis)
    pricing.match_model(jis, cfg["models"], cfg)
    pricing.score(jis, cfg, rates)
    check("JIS keyboard reduces expected UK price explicitly",
          jis.expected_price_gbp
          == variants[0].uk_avg_gbp
          - cfg["expected_price"]["keyboard_penalty_gbp"]["JIS"])
    auction = pricing.Listing("va", "yahoo",
                              "MacBook Pro 14 M4 Pro 24GB 512GB", 100000,
                              is_auction=True)
    pricing.parse_listing_specs(auction)
    pricing.match_model(auction, cfg["models"], cfg)
    pricing.score(auction, cfg, rates)
    check("auction bid has near-zero buyability confidence",
          auction.listing_confidence == 0.05)

    # EU landed-cost maths: €999 Mac mini from eBay DE
    cost_eu = pricing.landed_cost_gbp(999, "ebay_de", cfg, rates, "mac_mini")
    exp_eu = (999 + cfg["costs"]["eu_shipping_eur_family"]["mac_mini"]) / 1.17 * 1.20 + 12
    check(f"EU landed cost maths (£{cost_eu:.2f})", abs(cost_eu - exp_eu) < 0.01)
    # family-aware shipping: a compact Mac mini ships cheaper than a display
    cost_mini = pricing.landed_cost_gbp(100000, "mercari", cfg, rates, "mac_mini")
    cost_disp = pricing.landed_cost_gbp(100000, "mercari", cfg, rates, "display")
    check("family shipping: Mac mini < display on the same JP price",
          cost_mini < cost_disp)
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
    check("contaminated used median rejected in favour of fallback",
          pricing.fair_value_gbp(lc, cfg) == 780.0)
    lv.grade = "good"
    lv.cycles = 600
    pricing.score(lv, cfg, rates)
    pricing.value_score(lv, cfg)
    check("one canonical condition-aware value score computed",
          lv.value_landed_gbp == lv.landed_gbp
          and lv.fair_gbp == lv.expected_price_gbp
          and lv.value_pct == lv.savings_pct != 0)

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

    p = argparse.ArgumentParser(
        description="Apple product deal scanner (Japan + US + UK + EU)")
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
