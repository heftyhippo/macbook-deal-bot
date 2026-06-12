"""
report.py - console table, clickable HTML report, CSV export and the
Telegram alert message format.
"""
from __future__ import annotations

import csv
import html
from datetime import datetime

from pricing import Listing


def console_table(listings: list[Listing], jpy_per_gbp: float, fx_note: str) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for l in listings:
            print(f"{l.savings_pct:+6.1f}%  {l.model_label:10s} GBP{l.landed_gbp:8.0f} "
                  f"(UK ~GBP{l.uk_avg_gbp:.0f})  YEN{l.price_jpy:,}  {l.title[:60]}")
        return
    con = Console()
    con.print(f"\n[bold]FX:[/bold] 1 GBP = {jpy_per_gbp:.1f} JPY ({fx_note})")
    tab = Table(title="Best matching listings (sorted by saving vs UK average)",
                show_lines=False, expand=True)
    tab.add_column("Save", justify="right", style="bold green", no_wrap=True)
    tab.add_column("Model", no_wrap=True)
    tab.add_column("Specs", no_wrap=True)
    tab.add_column("JP price", justify="right", no_wrap=True)
    tab.add_column("Landed est.", justify="right", no_wrap=True)
    tab.add_column("UK avg", justify="right", no_wrap=True)
    tab.add_column("Cond/cycles", no_wrap=True)
    tab.add_column("Title / flags", overflow="fold", ratio=3)
    for l in listings:
        spec = "/".join(x for x in [
            f"{l.ram_gb}GB" if l.ram_gb else "?",
            (f"{l.storage_gb // 1024}TB" if l.storage_gb and l.storage_gb >= 1024
             else (f"{l.storage_gb}GB" if l.storage_gb else "?")),
        ])
        cyc = f"{l.cycles}cyc" if l.cycles is not None else "cyc?"
        cond = (l.condition or "")[:14] + f" {cyc}"
        flags = ("  [yellow]" + "; ".join(l.flags) + "[/yellow]") if l.flags else ""
        save = f"{l.savings_pct:+.0f}%"
        style = "bold red" if l.savings_pct >= 30 else ""
        tab.add_row(save, l.model_label, spec, f"¥{l.price_jpy:,}",
                    f"£{l.landed_gbp:,.0f}", f"£{l.uk_avg_gbp:,.0f}",
                    cond, html_escape_rich(l.title[:90]) + flags, style=style)
    con.print(tab)
    con.print("[dim]Full clickable links are in deals.html (open it in your browser).[/dim]\n")


def html_escape_rich(s: str) -> str:
    return s.replace("[", "(").replace("]", ")")


def write_html(listings: list[Listing], path: str, jpy_per_gbp: float) -> None:
    rows = []
    for l in listings:
        spec = " / ".join(x for x in [
            f"{l.ram_gb}GB RAM" if l.ram_gb else "RAM ?",
            (f"{l.storage_gb // 1024}TB" if l.storage_gb and l.storage_gb >= 1024
             else (f"{l.storage_gb}GB" if l.storage_gb else "storage ?")),
            f"kbd: {l.keyboard}",
        ])
        cyc = f"{l.cycles} cycles" if l.cycles is not None else "cycles unknown"
        flags = "; ".join(l.flags)
        hot = ' style="background:#ffe8e8"' if l.savings_pct >= 30 else ""
        rows.append(f"""<tr{hot}>
  <td><b>{l.savings_pct:+.0f}%</b></td>
  <td>{html.escape(l.model_label)}</td>
  <td>{html.escape(spec)}</td>
  <td>¥{l.price_jpy:,}{' (auction bid)' if l.is_auction else ''}</td>
  <td>£{l.landed_gbp:,.0f}</td>
  <td>£{l.uk_avg_gbp:,.0f}</td>
  <td>{html.escape(l.condition)} · {cyc}</td>
  <td>{html.escape(l.title)}<br><span class="flags">{html.escape(flags)}</span></td>
  <td><a href="{l.buyee_url}" target="_blank">Buyee</a> ·
      <a href="{l.zenmarket_url}" target="_blank">ZenMarket</a> ·
      <a href="{l.original_url}" target="_blank">Original</a></td>
</tr>""")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>MacBook deals from Japan</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial;margin:20px;background:#fafafa}}
 table{{border-collapse:collapse;width:100%;background:#fff}}
 th,td{{border:1px solid #ddd;padding:7px 9px;font-size:14px;vertical-align:top}}
 th{{background:#222;color:#fff;position:sticky;top:0}}
 tr:nth-child(even){{background:#f4f6f8}}
 .flags{{color:#a05a00;font-size:12px}}
 a{{color:#0a58ca}}
</style></head><body>
<h2>MacBook Pro deals - Japan vs UK</h2>
<p>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
1 GBP = {jpy_per_gbp:.1f} JPY ·
"Landed est." = item + proxy fee + JP &amp; international shipping + 20% UK import VAT + courier handling.</p>
<table>
<tr><th>Saving</th><th>Model</th><th>Specs</th><th>JP price</th><th>Landed est.</th>
<th>UK avg</th><th>Condition</th><th>Listing</th><th>Buy via</th></tr>
{''.join(rows) if rows else '<tr><td colspan=9>No matching listings this scan.</td></tr>'}
</table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def write_csv(listings: list[Listing], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["savings_pct", "model", "ram_gb", "storage_gb", "keyboard",
                    "price_jpy", "landed_gbp", "uk_avg_gbp", "condition", "cycles",
                    "auction", "flags", "title", "buyee", "zenmarket", "original"])
        for l in listings:
            w.writerow([l.savings_pct, l.model_label, l.ram_gb, l.storage_gb, l.keyboard,
                        l.price_jpy, l.landed_gbp, l.uk_avg_gbp, l.condition, l.cycles,
                        l.is_auction, "; ".join(l.flags), l.title,
                        l.buyee_url, l.zenmarket_url, l.original_url])


def telegram_message(l: Listing) -> str:
    fire = "🔥 INCREDIBLE DEAL" if l.savings_pct >= 45 else "✅ Good deal"
    spec = " / ".join(x for x in [
        f"{l.ram_gb}GB" if l.ram_gb else "RAM?",
        (f"{l.storage_gb // 1024}TB" if l.storage_gb and l.storage_gb >= 1024
         else (f"{l.storage_gb}GB" if l.storage_gb else "SSD?")),
        f"kbd {l.keyboard}",
    ])
    cyc = f"{l.cycles} battery cycles" if l.cycles is not None else "battery cycles unknown - check listing"
    flags = ("\n⚠️ " + "; ".join(l.flags)) if l.flags else ""
    return (
        f"{fire}: <b>MacBook Pro {html.escape(l.model_label)}</b>\n"
        f"Save ~<b>{l.savings_pct:.0f}%</b> - landed est. <b>£{l.landed_gbp:,.0f}</b> "
        f"vs UK avg £{l.uk_avg_gbp:,.0f}\n"
        f"JP price ¥{l.price_jpy:,}{' (auction bid)' if l.is_auction else ''} · {spec}\n"
        f"{html.escape(l.condition)} · {cyc}{flags}\n"
        f"{html.escape(l.title[:120])}\n\n"
        f"<a href='{l.buyee_url}'>Open in Buyee</a> | "
        f"<a href='{l.zenmarket_url}'>Open in ZenMarket</a> | "
        f"<a href='{l.original_url}'>Original listing</a>"
    )
