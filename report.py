"""
report.py - console table, the dashboard website (deals.html), CSV export
and the WhatsApp alert message format.
"""
from __future__ import annotations

import csv
import json
import time as _time
from datetime import datetime

import pricing
from pricing import Listing

GRADE_LABEL = {
    "resale": "New / unused",
    "personal": "Like new",
    "good": "Used - good",
}


def _expected_price(l: Listing) -> float:
    """Canonical expected price with fallbacks for old saved snapshots."""
    return float(getattr(l, "expected_price_gbp", 0) or
                 getattr(l, "fair_gbp", 0) or l.uk_avg_gbp or 0)


def _saving_gbp(l: Listing) -> float:
    if hasattr(l, "savings_gbp") and getattr(l, "expected_price_gbp", 0):
        return float(l.savings_gbp)
    return _expected_price(l) - float(l.landed_gbp or 0)


def _saving_pct(l: Listing) -> float:
    if getattr(l, "expected_price_gbp", 0):
        return float(l.savings_pct or 0)
    if getattr(l, "fair_gbp", 0):
        return float(getattr(l, "value_pct", 0) or 0)
    return float(l.savings_pct or 0)


def _optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _merge_deals(listings: list[Listing], legacy_best: list[Listing]) -> list[Listing]:
    """One result set, while accepting the historical two-list API."""
    unique: dict[str, Listing] = {}
    for l in list(listings) + list(legacy_best):
        key = f"{l.source}:{l.item_id}"
        unique[key] = l
    return sorted(unique.values(),
                  key=lambda x: (_saving_pct(x),
                                 float(getattr(x, "overall_score", 0) or 0)),
                  reverse=True)


def _spec_text(l: Listing) -> str:
    parts: list[str] = []
    if l.family != "display" and l.ram_gb:
        parts.append(f"{l.ram_gb}GB RAM")
    if l.storage_gb:
        parts.append(f"{l.storage_gb // 1024}TB" if l.storage_gb >= 1024
                     else f"{l.storage_gb}GB")
    if l.family in pricing.KEYBOARD_FAMILIES and l.keyboard not in ("n/a", "unknown", ""):
        parts.append(f"{l.keyboard} keyboard")
    return " / ".join(parts) or "spec not stated"


def _confidence_label(l: Listing) -> str:
    vals = [_optional_float(getattr(l, "benchmark_confidence", None)),
            _optional_float(getattr(l, "listing_confidence", None))]
    vals = [v for v in vals if v is not None]
    if not vals:
        return "unknown"
    conf = min(vals)
    return "high" if conf >= .80 else "medium" if conf >= .60 else "low"


def _alert_eligible(l: Listing, cfg: dict) -> bool:
    """Mirror the scanner's safety gates for a genuinely alertable deal."""
    if l.is_auction:
        return False
    if _saving_pct(l) < pricing.alert_thresholds(cfg, l.source, l.family)["min"]:
        return False
    a = cfg.get("alerts", {})
    if float(getattr(l, "listing_confidence", 0) or 0) < float(
            a.get("min_listing_confidence", 0.55)):
        return False
    if float(getattr(l, "benchmark_confidence", 0) or 0) < float(
            a.get("min_benchmark_confidence", 0.50)):
        return False
    risk_text = " ".join(str(flag).upper() for flag in l.flags)
    if any(marker in risk_text for marker in
           ("SUSPICIOUSLY LOW", "TOO-GOOD", "NOT A WHOLE PRODUCT")):
        return False
    if (l.family == "macbook" and l.cycles is not None
            and l.cycles > pricing.max_cycles_for(l.grade, cfg)):
        return False
    return True


def console_table(listings: list[Listing], best_value: list[Listing],
                  rates: dict, fx_note: str, cfg: dict) -> None:
    """Print the same single condition-aware ranking shown by the dashboard."""
    deals = _merge_deals(listings, best_value)
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for l in deals:
            print(f"{_saving_pct(l):+6.1f}%  {l.model_label:22s} "
                  f"GBP{l.landed_gbp:8.0f} vs GBP{_expected_price(l):8.0f}  "
                  f"{l.source:12s}  {l.title[:60]}")
        return

    con = Console()
    con.print(f"\n[bold]FX:[/bold] 1 GBP = {rates['JPY']:.1f} JPY = "
              f"{rates['USD']:.3f} USD = {rates.get('EUR', 0):.3f} EUR ({fx_note})")
    if not deals:
        con.print("[dim]No matching listings this scan.[/dim]\n")
        return

    tab = Table(title="BEST APPLE DEALS - saving against the expected UK price",
                show_lines=False, expand=True)
    tab.add_column("Deal", justify="right", style="bold green", no_wrap=True)
    tab.add_column("Product", ratio=3)
    tab.add_column("Where", no_wrap=True)
    tab.add_column("Price", justify="right", no_wrap=True)
    tab.add_column("Score", justify="right", no_wrap=True)
    for l in deals:
        overall = _optional_float(getattr(l, "overall_score", None))
        caution = (f"\n[yellow]⚠ {l.flags[0]}[/yellow]" if l.flags else "")
        cash_saving = _saving_gbp(l)
        cash_text = (f"+£{cash_saving:,.0f}" if cash_saving >= 0
                     else f"−£{abs(cash_saving):,.0f}")
        tab.add_row(f"{_saving_pct(l):+.0f}%\n{cash_text}",
                    f"{l.model_label}\n[dim]{_spec_text(l)}[/dim]" + caution,
                    f"{l.source}\n[dim]{GRADE_LABEL.get(l.grade, l.grade)}[/dim]",
                    f"£{l.landed_gbp:,.0f}\n[dim]vs £{_expected_price(l):,.0f}[/dim]",
                    ((f"{overall:.0f}/100\n" if overall is not None else "")
                     + f"[dim]{_confidence_label(l)} conf.[/dim]"))
    con.print(tab)
    con.print("[dim]Open deals.html for filters, price methodology and clickable links.[/dim]\n")


# ----------------------------------------------------------------------------
# The dashboard website (written to deals.html locally; the same file is
# published to GitHub Pages by the cloud workflow). Self-contained: inline
# CSS + JS + data, works from file:// and from a web server alike.
# ----------------------------------------------------------------------------

def _dash_record(l: Listing, cfg: dict) -> dict:
    benchmark_conf = _optional_float(getattr(l, "benchmark_confidence", None))
    listing_conf = _optional_float(getattr(l, "listing_confidence", None))
    overall = _optional_float(getattr(l, "overall_score", None))
    return {
        "key": f"{l.source}:{l.item_id}",
        "src": l.source,
        "region": pricing.region_of(l.source),
        "fam": l.family or "macbook",
        "grade": l.grade,
        "title": l.title,
        "description": str(getattr(l, "description", "") or "")[:1200],
        "location": str(getattr(l, "location", "") or "")[:200],
        "model": l.model_label,
        "ram": l.ram_gb,
        "ssd": l.storage_gb,
        "kbd": l.keyboard,
        "cond": l.condition,
        "cycles": l.cycles,
        "price": l.price_str,
        "auction": l.is_auction,
        "offer": l.best_offer,
        "landed": round(l.landed_gbp),
        "expected": round(_expected_price(l)),
        "expected_basis": str(getattr(l, "expected_price_basis", "") or ""),
        "save_gbp": round(_saving_gbp(l)),
        "save_pct": round(_saving_pct(l), 1),
        "score": round(overall, 1) if overall is not None else None,
        "benchmark_conf": benchmark_conf,
        "listing_conf": listing_conf,
        "snapshot_age": int(getattr(l, "snapshot_age_minutes", 0) or 0),
        "alert": _alert_eligible(l, cfg),
        "flags": list(l.flags),
        "links": [[label, url] for label, url in l.market_links],
    }


def write_html(listings: list[Listing], best_value: list[Listing], path: str,
               rates: dict, cfg: dict,
               source_meta: dict | None = None) -> None:
    deals = _merge_deals(listings, best_value)
    data = {
        "generated": int(_time.time()),
        "generated_str": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fx": {"JPY": round(rates["JPY"], 1), "USD": round(rates["USD"], 3),
               "EUR": round(rates.get("EUR", 0), 3)},
        "source_meta": source_meta or {},
        "items": [_dash_record(l, cfg) for l in deals],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_DASH_TEMPLATE.replace("%%DATA%%", payload))


_DASH_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Best Apple deals</title>
<style>
:root{
  color-scheme:light;
  --bg:#f3f6f9;--surface:#fff;--surface-2:#f8fafc;--text:#172033;
  --muted:#536277;--line:#d6dee8;--accent:#175cd3;--accent-hover:#124ba9;
  --accent-soft:#eaf2ff;--good:#087443;--good-soft:#e8f7ef;
  --warn:#8a4b00;--warn-soft:#fff2d8;--danger:#b42318;--danger-soft:#ffebe9;
  --shadow:0 8px 26px rgba(23,32,51,.07);--focus:#7c3aed;
}
@media(prefers-color-scheme:dark){
  :root{color-scheme:dark;--bg:#10141b;--surface:#171d26;--surface-2:#1d2530;
    --text:#f1f5f9;--muted:#b1bdca;--line:#35404e;--accent:#9cc2ff;
    --accent-hover:#bad5ff;--accent-soft:#203656;--good:#75dda7;
    --good-soft:#173b2b;--warn:#ffd083;--warn-soft:#463515;--danger:#ffaaa3;
    --danger-soft:#4b2625;--shadow:0 10px 30px rgba(0,0,0,.22);--focus:#c4b5fd}
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select{font:inherit}
button,a,summary,input,select{outline-offset:3px}
:focus-visible{outline:3px solid var(--focus)}
.skip{position:absolute;left:12px;top:-70px;background:var(--text);color:var(--surface);
  padding:10px 14px;border-radius:8px;z-index:20}
.skip:focus{top:12px}
.site-header{border-bottom:1px solid var(--line);background:var(--surface)}
.topbar{max-width:1180px;margin:auto;padding:18px 22px;display:flex;
  align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.brand-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;
  color:white;background:linear-gradient(145deg,#2776e6,#154aa0);font-size:21px}
h1{font-size:20px;line-height:1.2;margin:0;letter-spacing:-.02em}
.strap{display:block;color:var(--muted);font-size:12px;font-weight:500;margin-top:2px}
.meta{text-align:right;color:var(--muted);font-size:12.5px}
.meta strong{color:var(--text)}
main{max-width:1180px;margin:auto;padding:30px 22px 58px}
.intro{display:flex;justify-content:space-between;align-items:end;gap:28px;margin-bottom:18px}
.intro h2{font-size:30px;line-height:1.15;letter-spacing:-.035em;margin:0 0 7px}
.intro p{color:var(--muted);max-width:710px;margin:0;font-size:15.5px}
.method{white-space:nowrap;color:var(--good);background:var(--good-soft);
  border:1px solid color-mix(in srgb,var(--good) 26%,transparent);
  padding:7px 11px;border-radius:999px;font-size:12px;font-weight:700}
.filter-panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:16px;box-shadow:var(--shadow);margin-bottom:18px}
.filter-grid{display:grid;grid-template-columns:minmax(220px,1.7fr) repeat(4,minmax(130px,1fr));gap:12px}
.control{display:grid;gap:5px;color:var(--muted);font-size:12px;font-weight:650}
.control input,.control select{width:100%;min-height:43px;color:var(--text);
  background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.control input::placeholder{color:var(--muted);opacity:.8}
.advanced{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.advanced>summary,.source-health>summary,.deal-more>summary{cursor:pointer;color:var(--accent);
  font-weight:700;min-height:34px;display:flex;align-items:center;width:max-content}
.advanced-grid{display:flex;flex-wrap:wrap;gap:10px 22px;padding:8px 0 2px}
.check{display:flex;align-items:center;gap:8px;color:var(--text);cursor:pointer}
.check input{width:18px;height:18px;accent-color:var(--accent)}
.mini-select{display:flex;align-items:center;gap:8px;color:var(--text)}
.mini-select select{min-height:36px;border:1px solid var(--line);border-radius:8px;
  background:var(--surface-2);color:var(--text);padding:5px 9px}
.resultbar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  margin:12px 2px}
.resultbar p{margin:0;color:var(--muted)}
.resultbar strong{color:var(--text)}
.link-button{border:0;background:none;color:var(--accent);font-weight:700;cursor:pointer;padding:8px}
.deal-list{display:grid;gap:12px}
.deal-card{position:relative;display:grid;grid-template-columns:48px minmax(230px,1fr) minmax(320px,410px) 155px;
  gap:18px;align-items:center;background:var(--surface);border:1px solid var(--line);
  border-radius:17px;padding:20px;box-shadow:0 2px 8px rgba(23,32,51,.035)}
.deal-card.top{border:2px solid var(--accent);box-shadow:var(--shadow);padding:19px}
.top-label{position:absolute;top:-11px;left:18px;background:var(--accent);color:#071326;
  border-radius:999px;padding:3px 10px;font-size:11px;font-weight:800;letter-spacing:.03em}
@media(prefers-color-scheme:light){.top-label{color:#fff}}
.rank{width:42px;height:42px;border-radius:13px;background:var(--surface-2);border:1px solid var(--line);
  display:grid;place-items:center;font-size:14px;font-weight:800;color:var(--muted)}
.deal-card.top .rank{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
.eyebrow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:5px}
.tag{display:inline-flex;align-items:center;min-height:24px;border-radius:999px;padding:2px 8px;
  font-size:11px;font-weight:750;background:var(--surface-2);border:1px solid var(--line);color:var(--muted)}
.tag.stale,.tag.risk{color:var(--warn);background:var(--warn-soft);border-color:transparent}
.tag.alert{color:var(--good);background:var(--good-soft);border-color:transparent}
.deal-main h3{font-size:18px;line-height:1.25;letter-spacing:-.015em;margin:0 0 3px}
.spec{color:var(--text);font-size:13px;margin:0 0 3px}
.listing-title{color:var(--muted);font-size:12.5px;margin:0;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.flag-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.flag{font-size:11px;padding:3px 7px;border-radius:7px;background:var(--warn-soft);color:var(--warn)}
.flag.danger{background:var(--danger-soft);color:var(--danger);font-weight:700}
.deal-summary{display:grid;grid-template-columns:135px 1fr;gap:14px;align-items:center}
.deal-metric{border-right:1px solid var(--line);padding-right:14px}
.deal-metric strong{display:block;color:var(--good);font-size:34px;line-height:1;
  letter-spacing:-.04em;font-variant-numeric:tabular-nums}
.deal-metric.negative strong{color:var(--danger)}
.deal-metric span{display:block;font-size:12px;font-weight:750;margin-top:4px}
.deal-metric small{display:block;color:var(--muted);font-size:11.5px;margin-top:2px}
.prices{display:grid;grid-template-columns:1fr 1fr;gap:9px 15px;margin:0}
.prices div{min-width:0}
.prices dt{color:var(--muted);font-size:11px;margin:0}
.prices dd{font-weight:750;font-size:16px;margin:0;font-variant-numeric:tabular-nums;white-space:nowrap}
.scoreline{grid-column:1/-1;color:var(--muted);font-size:11.5px}
.scoreline strong{color:var(--text)}
.conf-high{color:var(--good)}.conf-medium{color:var(--warn)}.conf-low{color:var(--danger)}
.actions{display:grid;gap:7px;justify-items:stretch}
.primary-link,.show-more{display:flex;align-items:center;justify-content:center;min-height:44px;
  border:1px solid var(--accent);border-radius:10px;text-decoration:none;font-weight:750;
  color:white;background:#1756aa;padding:9px 12px;text-align:center}
.primary-link:hover,.show-more:hover{background:#12488f}
.secondary-note{text-align:center;color:var(--muted);font-size:11px}
.deal-more{grid-column:2/-1;border-top:1px solid var(--line);padding-top:8px;margin-top:-4px}
.detail-body{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,1fr);gap:22px;
  padding:8px 0 3px}
.detail-copy p{margin:0 0 9px;color:var(--muted)}
.detail-copy strong{color:var(--text)}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:0}
.detail-grid div{border-bottom:1px solid var(--line);padding-bottom:6px}
.detail-grid dt{color:var(--muted);font-size:11px}.detail-grid dd{margin:1px 0 0;font-size:12.5px}
.all-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.text-link{color:var(--accent);font-weight:700;text-decoration-thickness:1px;text-underline-offset:3px}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:16px;
  color:var(--muted);text-align:center;padding:48px 20px}
.empty strong{display:block;color:var(--text);font-size:18px;margin-bottom:4px}
.show-wrap{display:flex;justify-content:center;margin:22px 0}
.show-more{cursor:pointer;min-width:180px;background:var(--surface);color:var(--accent)}
.show-more:hover{background:var(--accent-soft)}
.source-health{margin-top:24px;color:var(--muted)}
.source-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:7px}
.source-item{border:1px solid var(--line);background:var(--surface);border-radius:10px;padding:9px 11px;font-size:12px}
.source-item strong{color:var(--text);display:block}
footer{border-top:1px solid var(--line);background:var(--surface)}
.footer-inner{max-width:1180px;margin:auto;padding:22px;color:var(--muted);font-size:12.5px}
.footer-inner strong{color:var(--text)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
[hidden]{display:none!important}
@media(max-width:980px){
  .filter-grid{grid-template-columns:2fr repeat(2,1fr)}
  .deal-card{grid-template-columns:48px minmax(0,1fr) minmax(320px,400px)}
  .actions{grid-column:2/-1;grid-template-columns:minmax(180px,260px) 1fr;align-items:center}
  .deal-more{grid-column:2/-1}
}
@media(max-width:760px){
  .topbar{align-items:flex-start;padding:15px 16px}.meta{max-width:190px}
  main{padding:22px 12px 44px}.intro{align-items:flex-start}.intro h2{font-size:26px}.method{display:none}
  .filter-grid{grid-template-columns:1fr 1fr}.filter-grid .search{grid-column:1/-1}
  .deal-card,.deal-card.top{grid-template-columns:42px minmax(0,1fr);gap:12px;padding:17px 14px}
  .rank{width:38px;height:38px}.deal-summary,.actions,.deal-more{grid-column:2/-1}
  .actions{grid-template-columns:1fr}.detail-body{grid-template-columns:1fr}
}
@media(max-width:520px){
  .topbar{display:block}.meta{text-align:left;max-width:none;margin:10px 0 0 49px}
  .strap{display:none}.intro{display:block}.filter-panel{padding:12px}
  .filter-grid{grid-template-columns:1fr}.filter-grid .search{grid-column:auto}
  .deal-card,.deal-card.top{display:block;padding:18px 14px}.deal-card.top{padding-top:22px}
  .rank{float:left;margin:0 10px 8px 0}.deal-main{min-height:42px}
  .deal-summary{clear:both;grid-template-columns:120px 1fr;margin-top:15px;background:var(--surface-2);
    border-radius:12px;padding:12px}.deal-metric strong{font-size:30px}
  .actions{margin-top:12px}.deal-more{margin-top:12px}.detail-grid{grid-template-columns:1fr}
  .advanced-grid{display:grid}.resultbar{align-items:flex-start}.source-grid{grid-template-columns:1fr}
}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
</style></head>
<body>
<a class="skip" href="#deals">Skip to deals</a>
<header class="site-header">
  <div class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true">◆</span><h1>Apple Deal Finder
      <span class="strap">The strongest Apple prices found across every market</span></h1></div>
    <div class="meta" id="meta" aria-live="polite"></div>
  </div>
</header>
<main id="main">
  <section class="intro" aria-labelledby="deals-heading">
    <div><h2 id="deals-heading">Best Apple deals</h2>
      <p id="method-copy">One ranking, based on how far the all-in cost sits below the expected UK price for the exact model, specification and condition.</p></div>
    <span class="method">Condition-aware pricing</span>
  </section>

  <form class="filter-panel" id="filters" role="search" aria-label="Filter Apple deals">
    <div class="filter-grid">
      <label class="control search" for="q">Search
        <input id="q" type="search" autocomplete="off" placeholder="Model, specification, title or location">
      </label>
      <label class="control" for="ffam">Product
        <select id="ffam"><option value="">All products</option><option value="macbook">MacBooks</option>
          <option value="desktop">Mac desktops</option><option value="display">Displays</option></select>
      </label>
      <label class="control" for="fmarket">Marketplace
        <select id="fmarket"><option value="">All marketplaces</option></select>
      </label>
      <label class="control" for="fgrade">Condition
        <select id="fgrade"><option value="">All conditions</option><option value="resale">New / unused</option>
          <option value="personal">Like new</option><option value="good">Used - good</option></select>
      </label>
      <label class="control" for="sort">Sort by
        <select id="sort"><option value="save_pct">Best saving %</option><option value="score">Overall score</option>
          <option value="save_gbp">Biggest £ saving</option><option value="price">Lowest all-in price</option>
          <option value="confidence">Highest confidence</option></select>
      </label>
    </div>
    <details class="advanced" id="advanced">
      <summary>Advanced filters</summary>
      <fieldset class="advanced-grid"><legend class="sr-only">Advanced filters</legend>
        <label class="check"><input type="checkbox" id="fpositive" checked> Deals below expected price only</label>
        <label class="check"><input type="checkbox" id="fauction"> Fixed-price listings only</label>
        <label class="check"><input type="checkbox" id="fprotected"> Buyer protection only</label>
        <label class="check"><input type="checkbox" id="fjis"> Hide JIS keyboards</label>
        <label class="mini-select" for="fconfidence">Minimum confidence
          <select id="fconfidence"><option value="0">Any</option><option value="0.6">60%</option>
            <option value="0.75">75%</option><option value="0.85">85%</option></select></label>
      </fieldset>
    </details>
  </form>

  <div class="resultbar"><p id="result-count" aria-live="polite"></p>
    <button class="link-button" type="button" id="clear" hidden>Clear filters</button></div>
  <section class="deal-list" id="deals" aria-label="Ranked Apple deals"></section>
  <div class="show-wrap"><button class="show-more" type="button" id="show-more" hidden>Show more deals</button></div>
  <details class="source-health" id="source-health" hidden><summary>Scan details</summary><div class="source-grid" id="source-grid"></div></details>
</main>
<footer><div class="footer-inner"><strong>How the ranking works.</strong> All-in cost includes the listing price, estimated shipping,
  proxy or forwarding fees, UK import VAT and handling where applicable. Expected price is adjusted for the exact model, specification,
  condition, keyboard and known battery wear. The percentage saving is the primary ranking; overall score also reduces uncertain matches
  and weaker price evidence. Estimates are deliberately cautious. Always verify the exact product, seller, photos, lock status and final
  delivered cost before buying. Gumtree listings are leads without normal buyer protection.</div></footer>
<noscript><p class="empty">JavaScript is needed to display and filter the deal list.</p></noscript>
<script>const DATA = %%DATA%%;</script>
<script>
(function(){
"use strict";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=n=>n==null||!Number.isFinite(Number(n))?"–":"£"+Math.round(Number(n)).toLocaleString("en-GB");
const MARKET={mercari:"🇯🇵 Mercari",yahoo:"🇯🇵 Yahoo Auctions",rakuma:"🇯🇵 Rakuma",
  paypay:"🇯🇵 PayPay Flea",ebay_us:"🇺🇸 eBay US",swappa:"🇺🇸 Swappa",
  ebay_uk:"🇬🇧 eBay UK",gumtree:"🇬🇧 Gumtree",ebay_de:"🇩🇪 eBay DE"};
const GRADE={resale:"New / unused",personal:"Like new",good:"Used - good"};
const FAMGROUP={macbook:"macbook",mac_mini:"desktop",mac_studio:"desktop",imac:"desktop",
  mac_pro:"desktop",display:"display"};
const PAGE=30;
let limit=PAGE;

function safeUrl(value){
  try{const u=new URL(String(value));return (u.protocol==="https:"||u.protocol==="http:")?u.href:"";}catch(e){return "";}
}
function ageText(minutes){
  const n=Math.max(0,Math.round(Number(minutes)||0));
  if(n<1)return "just now";if(n<120)return n+" min old";if(n<2880)return Math.round(n/60)+" h old";
  return Math.round(n/1440)+" days old";
}
function specText(r){
  const parts=[];
  if(r.fam!=="display"&&r.ram)parts.push(r.ram+"GB RAM");
  if(r.ssd)parts.push(r.ssd>=1024?(r.ssd/1024)+"TB":r.ssd+"GB");
  if(r.kbd&&!['unknown','n/a',''].includes(r.kbd))parts.push(r.kbd+" keyboard");
  return parts.join(" · ")||"Specification not stated";
}
function confidenceValue(r){
  const vals=[r.benchmark_conf,r.listing_conf].filter(v=>v!=null&&Number.isFinite(Number(v))).map(Number);
  return vals.length?Math.min(...vals):0;
}
function confidenceMeta(r){
  const v=confidenceValue(r);return v>=.8?["High","high",v]:v>=.6?["Medium","medium",v]:["Low","low",v];
}
function isClassified(r){return r.src==="gumtree";}
function dangerousFlag(f){return /TOO-GOOD|SUSPICIOUS|NOT A WHOLE|no buyer protection|pickup\/cash/i.test(f);}
function dealMetric(r){
  const p=Number(r.save_pct)||0;
  if(p>0)return {number:Math.round(p)+"%",label:"below expected",money:"Save "+money(r.save_gbp),negative:false};
  if(p===0)return {number:"0%",label:"at expected price",money:"No estimated saving",negative:false};
  return {number:Math.abs(Math.round(p))+"%",label:"above expected",money:money(Math.abs(r.save_gbp))+" over",negative:true};
}
function primaryLink(r){
  for(const item of (r.links||[])){const url=safeUrl(item[1]);if(url)return [item[0],url];}
  return null;
}
function linkHtml(item){
  const url=safeUrl(item[1]);return url?'<a class="text-link" target="_blank" rel="noopener noreferrer" href="'+
    esc(url)+'">'+esc(item[0])+' <span aria-hidden="true">↗</span></a>':"";
}
function card(r,index){
  const metric=dealMetric(r),conf=confidenceMeta(r),primary=primaryLink(r),id="deal-"+index;
  const market=MARKET[r.src]||r.src;
  const visibleFlags=(r.flags||[]).slice(0,2).map(f=>'<span class="flag '+(dangerousFlag(f)?"danger":"")+'">'+esc(f)+'</span>').join("");
  const moreFlags=(r.flags||[]).length>2?'<span class="flag">+'+((r.flags||[]).length-2)+' more checks</span>':"";
  const stale=r.snapshot_age?'<span class="tag stale">Saved scan · '+ageText(r.snapshot_age)+'</span>':"";
  const alert=r.alert?'<span class="tag alert">Strong deal</span>':"";
  const score=r.score==null?"–":Math.round(r.score)+"/100";
  const details=[];
  details.push('<div><dt>Original price</dt><dd>'+esc(r.price)+(r.auction?" (current bid)":"")+(r.offer?" · offers accepted":"")+'</dd></div>');
  details.push('<div><dt>Expected-price method</dt><dd>'+esc(r.expected_basis||"Configured UK benchmark")+'</dd></div>');
  details.push('<div><dt>Benchmark confidence</dt><dd>'+Math.round((Number(r.benchmark_conf)||0)*100)+'%</dd></div>');
  details.push('<div><dt>Listing confidence</dt><dd>'+Math.round((Number(r.listing_conf)||0)*100)+'%</dd></div>');
  if(r.location)details.push('<div><dt>Location</dt><dd>'+esc(r.location)+'</dd></div>');
  if(r.cycles!=null)details.push('<div><dt>Battery cycles</dt><dd>'+esc(r.cycles)+'</dd></div>');
  if(r.snapshot_age)details.push('<div><dt>Source snapshot</dt><dd>'+ageText(r.snapshot_age)+'</dd></div>');
  const allFlags=(r.flags||[]).length?'<p><strong>Checks before buying:</strong> '+esc((r.flags||[]).join(" · "))+'</p>':"";
  const links=(r.links||[]).map(linkHtml).filter(Boolean).join("");
  return '<article class="deal-card '+(index===0?"top":"")+'" aria-labelledby="'+id+'">'+
    (index===0?'<span class="top-label">TOP DEAL IN THIS VIEW</span>':"")+
    '<div class="rank" aria-label="Rank '+(index+1)+'">#'+(index+1)+'</div>'+
    '<div class="deal-main"><div class="eyebrow"><span class="tag">'+esc(market)+'</span><span class="tag">'+
      esc(GRADE[r.grade]||r.grade)+'</span>'+stale+alert+'</div><h3 id="'+id+'">'+esc(r.model||"Apple product")+'</h3>'+
      '<p class="spec">'+esc(specText(r))+'</p><p class="listing-title">'+esc(r.title)+'</p>'+
      ((visibleFlags||moreFlags)?'<div class="flag-row">'+visibleFlags+moreFlags+'</div>':"")+'</div>'+
    '<div class="deal-summary"><div class="deal-metric '+(metric.negative?"negative":"")+'"><strong>'+metric.number+
      '</strong><span>'+metric.label+'</span><small>'+metric.money+'</small></div><dl class="prices"><div><dt>All-in cost</dt><dd>'+
      money(r.landed)+'</dd></div><div><dt>Expected price</dt><dd>'+money(r.expected)+'</dd></div><div class="scoreline">'+
      'Overall <strong>'+score+'</strong> · <span class="conf-'+conf[1]+'"><strong>'+conf[0]+' confidence</strong></span></div></dl></div>'+
    '<div class="actions">'+(primary?'<a class="primary-link" target="_blank" rel="noopener noreferrer" href="'+esc(primary[1])+
      '">View on '+esc(primary[0])+' <span aria-hidden="true">↗</span></a>':'<span class="secondary-note">Listing link unavailable</span>')+
      '<span class="secondary-note">Verify details before buying</span></div>'+
    '<details class="deal-more"><summary>Price method and listing details</summary><div class="detail-body"><div class="detail-copy">'+
      '<p><strong>Listing title:</strong> '+esc(r.title)+'</p>'+(r.description?'<p><strong>Description:</strong> '+esc(r.description)+'</p>':"")+
      allFlags+(links?'<div class="all-links">'+links+'</div>':"")+'</div><dl class="detail-grid">'+details.join("")+'</dl></div></details></article>';
}

function filteredRows(){
  const q=$("#q").value.trim().toLowerCase(),fam=$("#ffam").value,market=$("#fmarket").value;
  const grade=$("#fgrade").value,minConf=Number($("#fconfidence").value)||0;
  const positive=$("#fpositive").checked,fixed=$("#fauction").checked;
  const protectedOnly=$("#fprotected").checked,hideJis=$("#fjis").checked;
  let rows=DATA.items.filter(r=>{
    const hay=[r.model,r.title,r.description,r.location,r.src,(r.flags||[]).join(" ")].join(" ").toLowerCase();
    return (!q||hay.includes(q))&&(!fam||FAMGROUP[r.fam]===fam)&&(!market||r.src===market)&&
      (!grade||r.grade===grade)&&(!positive||r.save_pct>0)&&(!fixed||!r.auction)&&
      (!protectedOnly||!isClassified(r))&&(!hideJis||r.kbd!=="JIS")&&confidenceValue(r)>=minConf;
  });
  const sort=$("#sort").value;
  rows.sort((a,b)=>sort==="save_gbp"?b.save_gbp-a.save_gbp:
    sort==="score"?(b.score??-1)-(a.score??-1):sort==="price"?a.landed-b.landed:
    sort==="confidence"?confidenceValue(b)-confidenceValue(a):b.save_pct-a.save_pct);
  return rows;
}
function nonDefaultFilters(){
  return Boolean($("#q").value||$("#ffam").value||$("#fmarket").value||$("#fgrade").value||
    $("#sort").value!=="save_pct"||!$("#fpositive").checked||$("#fauction").checked||
    $("#fprotected").checked||$("#fjis").checked||$("#fconfidence").value!=="0");
}
function render(){
  const rows=filteredRows(),shown=rows.slice(0,limit);
  $("#deals").innerHTML=shown.length?shown.map(card).join(""):
    '<div class="empty"><strong>No deals match these filters</strong>Try clearing a filter or check again after the next scan.</div>';
  $("#result-count").innerHTML='<strong>'+rows.length+'</strong> '+(rows.length===1?"deal":"deals")+
    (rows.length&&shown.length<rows.length?' · showing '+shown.length:"");
  $("#show-more").hidden=shown.length>=rows.length;
  $("#show-more").textContent="Show "+Math.min(PAGE,rows.length-shown.length)+" more deals";
  $("#clear").hidden=!nonDefaultFilters();
}
function updateMeta(){
  const age=Math.max(0,Math.round((Date.now()/1000-DATA.generated)/60));
  const updated=age<1?"just now":ageText(age).replace(" old","")+" ago";
  $("#meta").innerHTML='<strong>'+DATA.items.length+' ranked deals</strong><br>Updated '+updated;
}
function sourceHealth(){
  const entries=Object.entries(DATA.source_meta||{});if(!entries.length)return;
  $("#source-grid").innerHTML=entries.sort((a,b)=>(MARKET[a[0]]||a[0]).localeCompare(MARKET[b[0]]||b[0])).map(([src,m])=>
    '<div class="source-item"><strong>'+esc(MARKET[src]||src)+'</strong>'+esc(m.listing_count??0)+' saved results · '+
    (m.age_minutes==null?"age unknown":ageText(m.age_minutes))+'</div>').join("");
  $("#source-health").hidden=false;
}
function populateMarkets(){
  [...new Set(DATA.items.map(r=>r.src))].sort((a,b)=>(MARKET[a]||a).localeCompare(MARKET[b]||b)).forEach(src=>{
    const o=document.createElement("option");o.value=src;o.textContent=MARKET[src]||src;$("#fmarket").appendChild(o);
  });
}

$("#filters").addEventListener("submit",e=>e.preventDefault());
$("#filters").addEventListener("input",()=>{limit=PAGE;render();});
$("#clear").addEventListener("click",()=>{$("#filters").reset();limit=PAGE;render();$("#q").focus();});
$("#show-more").addEventListener("click",()=>{limit+=PAGE;render();$("#show-more").scrollIntoView({block:"nearest"});});
populateMarkets();sourceHealth();updateMeta();render();setInterval(updateMeta,60000);
if(location.protocol.indexOf("http")===0)setInterval(()=>location.reload(),10*60*1000);
})();
</script>
</body></html>
"""


def write_csv(listings: list[Listing], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rank", "overall_score", "savings_pct", "savings_gbp",
                    "landed_gbp", "expected_price_gbp", "expected_price_basis",
                    "benchmark_confidence", "listing_confidence", "family",
                    "grade", "model", "source", "ram_gb", "storage_gb",
                    "keyboard", "listing_price", "currency", "condition",
                    "cycles", "auction", "best_offer", "snapshot_age_minutes",
                    "location", "flags", "title", "description", "links"])
        for rank, l in enumerate(_merge_deals(listings, []), 1):
            w.writerow([
                rank, getattr(l, "overall_score", ""), _saving_pct(l),
                _saving_gbp(l), l.landed_gbp, _expected_price(l),
                getattr(l, "expected_price_basis", ""),
                getattr(l, "benchmark_confidence", ""),
                getattr(l, "listing_confidence", ""), l.family, l.grade,
                l.model_label, l.source, l.ram_gb, l.storage_gb, l.keyboard,
                l.price, l.currency, l.condition, l.cycles, l.is_auction,
                l.best_offer, getattr(l, "snapshot_age_minutes", 0),
                getattr(l, "location", ""), "; ".join(l.flags), l.title,
                getattr(l, "description", ""),
                " | ".join(url for _, url in l.market_links),
            ])


def whatsapp_message(l: Listing, cfg: dict) -> str:
    """Plain-text alert for WhatsApp (CallMeBot). *asterisks* render bold."""
    t = pricing.alert_thresholds(cfg, l.source, l.family)
    saving_pct = _saving_pct(l)
    saving_gbp = _saving_gbp(l)
    expected = _expected_price(l)
    fire = ("🔥 EXCEPTIONAL DEAL" if saving_pct >= t["hot"] else "✅ Strong deal")
    tier = {"resale": "new / unused", "personal": "like new",
            "good": "used - good"}.get(l.grade, l.grade)
    src = {"ebay_us": "eBay US 🇺🇸", "swappa": "Swappa 🇺🇸",
           "mercari": "Mercari 🇯🇵",
           "yahoo": "Yahoo 🇯🇵", "rakuma": "Rakuma 🇯🇵",
           "paypay": "PayPay 🇯🇵", "ebay_uk": "eBay UK 🇬🇧",
           "gumtree": "Gumtree 🇬🇧", "ebay_de": "eBay DE 🇩🇪"}.get(l.source, l.source)
    relation = "below" if saving_pct >= 0 else "above"
    amount = f"save £{abs(saving_gbp):,.0f}" if saving_gbp >= 0 else f"£{abs(saving_gbp):,.0f} over"
    overall = _optional_float(getattr(l, "overall_score", None))
    benchmark_conf = _optional_float(getattr(l, "benchmark_confidence", None))
    listing_conf = _optional_float(getattr(l, "listing_confidence", None))
    lines = [
        f"{fire} ({tier})",
        f"*{l.model_label}* — {src}",
        f"*{abs(saving_pct):.0f}% {relation} expected* ({amount})",
        f"All-in *£{l.landed_gbp:,.0f}* vs expected *£{expected:,.0f}*",
    ]
    confidence_bits = []
    if overall is not None:
        confidence_bits.append(f"overall {overall:.0f}/100")
    if benchmark_conf is not None:
        confidence_bits.append(f"price evidence {benchmark_conf:.0%}")
    if listing_conf is not None:
        confidence_bits.append(f"listing confidence {listing_conf:.0%}")
    if confidence_bits:
        lines.append(" · ".join(confidence_bits))
    basis = str(getattr(l, "expected_price_basis", "") or "")
    if basis:
        lines.append(f"Expected price: {basis}")
    lines.append(f"Listing {l.price_str}{' (auction bid)' if l.is_auction else ''}"
                 f" · {_spec_text(l)}")
    cond = l.condition
    if l.family == "macbook":
        cond += " · " + (f"{l.cycles} battery cycles" if l.cycles is not None
                         else "battery cycles unknown - check listing")
    lines.append(cond)
    location = str(getattr(l, "location", "") or "")
    if location:
        lines.append(f"Location: {location}")
    snapshot_age = int(getattr(l, "snapshot_age_minutes", 0) or 0)
    if snapshot_age:
        lines.append(f"Retained from last successful scan ({snapshot_age} min old)")
    if l.flags:
        lines.append("⚠️ " + "; ".join(l.flags))
    lines.append(l.title[:120])
    lines.append("")
    for label, url in l.market_links:
        lines.append(f"{label}: {url}")
    return "\n".join(lines)
