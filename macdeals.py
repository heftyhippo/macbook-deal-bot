#!/usr/bin/env python3
"""
macdeals.py - MacBook Pro Japan deal scanner (Buyee / ZenMarket marketplaces)

Commands
--------
  python macdeals.py scan                 one-off scan, prints table + writes deals.html
  python macdeals.py watch                scan every N minutes, Telegram-alert great deals
  python macdeals.py ukprices [--write]   refresh UK averages from eBay UK SOLD listings
  python macdeals.py test-telegram        send a test message to your Telegram
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
    # runner (GitHub Actions) injects your Telegram secrets WITHOUT them ever
    # being written into a file in the repo. Locally, no env vars are set, so
    # your config.yaml values are used as normal.
    import os
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok or cid:
        tg = cfg.setdefault("telegram", {})
        if tok:
            tg["bot_token"] = tok
        if cid:
            tg["chat_id"] = cid
        tg["enabled"] = True
    # Optional: override the sources list from an env var, e.g. on a server IP
    # where Buyee blocks the browser you might set SOURCES=mercari.
    src = os.environ.get("SOURCES", "").strip()
    if src:
        cfg.setdefault("scan", {})["sources"] = [x.strip() for x in src.split(",") if x.strip()]
    return cfg


# ----------------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------------

def run_scan(cfg: dict, send_alerts: bool, debug: bool, demo: bool,
             csv_path: str | None) -> None:
    t0 = datetime.now().strftime("%H:%M:%S")
    print(f"[{t0}] scanning...")

    jpy_per_gbp, fx_note = pricing.get_jpy_per_gbp(cfg["fx"]["fallback_jpy_per_gbp"])

    raw: list[pricing.Listing] = []
    if demo:
        raw = demo_listings()
    else:
        if "mercari" in cfg["scan"]["sources"]:
            r = sources.scan_mercari(cfg)
            print(f"  mercari: {len(r)} raw listings")
            raw += r
        if "yahoo" in cfg["scan"]["sources"]:
            r = sources.scan_yahoo(cfg, debug=debug)
            print(f"  yahoo auctions: {len(r)} raw listings")
            raw += r
        if "rakuma" in cfg["scan"]["sources"]:
            r = sources.scan_rakuma(cfg, debug=debug)
            print(f"  rakuma: {len(r)} raw listings")
            raw += r

    matched: list[pricing.Listing] = []
    for l in raw:
        kw = pricing.is_excluded(l.title, cfg["filters"]["exclude_keywords"])
        if kw:
            continue
        pricing.parse_listing_specs(l)
        if not l.chip:
            continue
        pricing.match_model(l, cfg["models"])
        if not l.model_id:
            continue
        matched.append(l)

    # remember everything we saw
    for l in matched:
        store.upsert_seen(l.item_id, l.source, l.title, l.price_jpy)

    # provisional score, then enrich the most promising with battery-cycle info
    for l in matched:
        pricing.score(l, cfg, jpy_per_gbp)
    matched.sort(key=lambda x: x.savings_pct, reverse=True)

    near = cfg["alerts"]["min_savings_pct"] - 8
    budget = int(cfg["scan"]["max_detail_fetch"])
    for l in matched:
        if budget <= 0:
            break
        if l.savings_pct < near or demo:
            break
        if l.source == "mercari":
            l.cycles = sources.fetch_mercari_cycles(l.item_id)
        elif l.source == "rakuma":
            l.cycles = sources.fetch_rakuma_cycles(l.item_id)
        else:
            l.cycles = sources.fetch_yahoo_cycles(l.item_id)
        budget -= 1
        time.sleep(1.0)
        l.flags = []          # rescore with cycle info
        pricing.score(l, cfg, jpy_per_gbp)

    # output
    top = matched[:40]
    report.console_table(top, jpy_per_gbp, fx_note)
    report.write_html(top, "deals.html", jpy_per_gbp)
    print(f"  wrote deals.html ({len(top)} rows shown of {len(matched)} matched)")
    if csv_path:
        report.write_csv(matched, csv_path)
        print(f"  wrote {csv_path}")

    # alerts
    if send_alerts:
        a = cfg["alerts"]
        sent = 0
        for l in matched:
            if l.savings_pct < a["min_savings_pct"]:
                break
            if l.cycles is not None and l.cycles > a["max_battery_cycles"]:
                continue
            # never alert a suspected part/accessory (price-floor backstop)
            if any("PRICE TOO LOW" in f for f in l.flags):
                continue
            if not store.should_alert(l.item_id, l.price_jpy, a["realert_drop_pct"]):
                continue
            if store.telegram_send(cfg, report.telegram_message(l)):
                store.mark_alerted(l.item_id, l.price_jpy)
                sent += 1
        print(f"  telegram alerts sent: {sent}")


def run_watch(cfg: dict, interval: int | None, debug: bool) -> None:
    minutes = interval or int(cfg["scan"]["watch_interval_minutes"])
    minutes = max(minutes, 8)
    print(f"Watch mode: scanning every {minutes} min. Leave this window open. Ctrl+C to stop.")
    srcs = ", ".join(cfg["scan"]["sources"])
    store.telegram_send(cfg, f"👀 MacBook deal bot is now watching {srcs} "
                             f"every {minutes} min (alerting at "
                             f"{cfg['alerts']['min_savings_pct']}%+ savings).")
    while True:
        try:
            run_scan(cfg, send_alerts=True, debug=debug, demo=False, csv_path=None)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  scan cycle error (will retry): {e}")
        sleep_s = minutes * 60 + random.randint(-45, 45)
        nxt = time.strftime("%H:%M:%S", time.localtime(time.time() + sleep_s))
        print(f"  next scan ~{nxt}")
        try:
            time.sleep(max(sleep_s, 60))
        except KeyboardInterrupt:
            print("\nStopped.")
            return


# ----------------------------------------------------------------------------
# UK price refresh (eBay UK sold listings)
# ----------------------------------------------------------------------------

def run_ukprices(cfg: dict, write: bool, debug: bool) -> None:
    print("Fetching recent eBay UK SOLD prices (condition: New + Open box, UK located)...")
    print("This takes a couple of minutes - one polite request per model.\n")
    results = []
    for mdl in cfg["models"]:
        med, n = sources.ebay_uk_sold_median(mdl["ebay_query"], debug=debug)
        cur = mdl["uk_avg_gbp"]
        if med:
            print(f"  {mdl['id']:<10} current £{cur:>5}   eBay sold median £{med:>6.0f}  ({n} samples)")
        else:
            print(f"  {mdl['id']:<10} current £{cur:>5}   not enough sold data ({n} samples) - keeping current")
        results.append((mdl["id"], med))
        time.sleep(2.5)
    if write:
        updated = 0
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        import re as _re
        for mid, med in results:
            if not med:
                continue
            pat = _re.compile(r"(\{id:\s*" + _re.escape(mid) + r".*?uk_avg_gbp:\s*)(\d+)")
            text, c = pat.subn(lambda m: m.group(1) + str(int(med)), text, count=1)
            updated += c
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\nWrote {updated} updated averages into {CONFIG_FILE}.")
    else:
        print("\n(Read-only run. Add --write to save these medians into config.yaml.)")


# ----------------------------------------------------------------------------
# Demo data + self-test
# ----------------------------------------------------------------------------

def demo_listings() -> list[pricing.Listing]:
    L = pricing.Listing
    return [
        L("m11111111111", "mercari", "【新品未開封】MacBook Pro 14インチ M4 Pro 24GB 512GB スペースブラック", 218000, condition="新品、未使用"),
        L("m22222222222", "mercari", "MacBook Pro 16インチ M3 Pro 18GB/512GB 未使用に近い 充放電回数3回 US配列", 195000, condition="未使用に近い"),
        L("x1234567890", "yahoo", "MacBook Pro 14 M5 16GB 512GB 新品 未使用 国内正規品", 248000, is_auction=False, condition="未使用"),
        L("x2345678901", "yahoo", "ジャンク MacBook Pro M3 Max 16インチ", 90000, condition="未使用"),
        L("m33333333333", "mercari", "MacBook Pro M2 Pro 14inch 16GB 512GB 箱のみ", 65000, condition="新品、未使用"),
        L("m44444444444", "mercari", "MacBook Air M2 13インチ 新品", 99000, condition="新品、未使用"),
        L("m55555555555", "mercari", "MacBook Pro M2 Max 16インチ 32GB 1TB 新品未使用 JIS配列", 230000, condition="新品、未使用"),
    ]


def run_selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    cfg = load_config()
    check("config.yaml loads", isinstance(cfg, dict) and "models" in cfg)
    check("19 models defined", len(cfg["models"]) == 19)

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

    # landed-cost maths: ¥218,000 mercari at 195 JPY/GBP, defaults
    cost = pricing.landed_cost_gbp(218000, "mercari", cfg, 195.0)
    expect = (218000 + 800 + 0 + 8000) / 195.0 * 1.20 + 12
    check(f"landed cost maths (£{cost:.2f})", abs(cost - expect) < 0.01)

    mdl = next(m for m in cfg["models"] if m["id"] == "m4pro-14")
    l.uk_avg_gbp = mdl["uk_avg_gbp"]
    pricing.score(l, cfg, 195.0)
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

    p = argparse.ArgumentParser(description="MacBook Pro Japan deal scanner")
    p.add_argument("command", choices=["scan", "watch", "ukprices", "test-telegram", "selftest"])
    p.add_argument("--no-alert", action="store_true", help="scan without sending Telegram alerts")
    p.add_argument("--debug", action="store_true", help="save raw pages for troubleshooting")
    p.add_argument("--demo", action="store_true", help="use built-in fake listings (no internet)")
    p.add_argument("--csv", metavar="FILE", help="also export results to a CSV file")
    p.add_argument("--interval", type=int, help="watch-mode minutes between scans")
    p.add_argument("--write", action="store_true", help="ukprices: save medians into config.yaml")
    args = p.parse_args()

    if args.command == "selftest":
        return run_selftest()

    cfg = load_config()

    if args.command == "test-telegram":
        ok = store.telegram_send(cfg, "✅ MacBook deal bot can reach your Telegram. You're all set!")
        print("Sent!" if ok else "Failed - check telegram settings in config.yaml "
                                 "(enabled: true, bot_token, chat_id).")
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
