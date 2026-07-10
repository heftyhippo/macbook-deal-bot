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

# the two output tiers, in display order
TIERS = [
    ("resale", "RESALE-GRADE - new / unused (arbitrage-safe)"),
    ("personal", "PRACTICALLY NEW - buy-to-use (used, zero visible wear, "
                 "savings vs buying new)"),
]


def console_table(listings: list[Listing], best_value: list[Listing],
                  rates: dict, fx_note: str, cfg: dict) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for l in listings:
            print(f"{l.savings_pct:+6.1f}%  {l.model_label:10s} GBP{l.landed_gbp:8.0f} "
                  f"(UK ~GBP{l.uk_avg_gbp:.0f})  {l.price_str}  {l.title[:60]}")
        return
    con = Console()
    con.print(f"\n[bold]FX:[/bold] 1 GBP = {rates['JPY']:.1f} JPY = "
              f"{rates['USD']:.3f} USD ({fx_note})")
    if not listings and not best_value:
        con.print("[dim]No matching listings this scan.[/dim]\n")
        return
    for grade, tier_title in TIERS:
        group = [l for l in listings if l.grade == grade]
        if not group:
            continue
        tab = Table(title=tier_title, show_lines=False, expand=True)
        tab.add_column("Save", justify="right", style="bold green", no_wrap=True)
        tab.add_column("Model", no_wrap=True)
        tab.add_column("Specs", no_wrap=True)
        tab.add_column("Src", no_wrap=True)
        tab.add_column("Price", justify="right", no_wrap=True)
        tab.add_column("Landed est.", justify="right", no_wrap=True)
        tab.add_column("UK avg", justify="right", no_wrap=True)
        tab.add_column("Cond/cycles", no_wrap=True)
        tab.add_column("Title / flags", overflow="fold", ratio=3)
        for l in group:
            spec = "/".join(x for x in [
                f"{l.ram_gb}GB" if l.ram_gb else "?",
                (f"{l.storage_gb // 1024}TB" if l.storage_gb and l.storage_gb >= 1024
                 else (f"{l.storage_gb}GB" if l.storage_gb else "?")),
            ])
            cyc = f"{l.cycles}cyc" if l.cycles is not None else "cyc?"
            cond = (l.condition or "")[:14] + f" {cyc}"
            flags = ("  [yellow]" + "; ".join(l.flags) + "[/yellow]") if l.flags else ""
            save = f"{l.savings_pct:+.0f}%"
            # highlight rows that clear their own region's alert bar
            hot = l.savings_pct >= pricing.alert_thresholds(cfg, l.source)["min"]
            tab.add_row(save, l.model_label, spec, l.source, l.price_str,
                        f"£{l.landed_gbp:,.0f}", f"£{l.uk_avg_gbp:,.0f}",
                        cond, html_escape_rich(l.title[:90]) + flags,
                        style="bold red" if hot else "")
        con.print(tab)
    if best_value:
        tab = Table(title="BEST VALUE - top deals for price RELATIVE TO "
                          "CONDITION (all markets, all tiers)",
                    show_lines=False, expand=True)
        tab.add_column("Deal", justify="right", style="bold cyan", no_wrap=True)
        tab.add_column("Model", no_wrap=True)
        tab.add_column("Cond", no_wrap=True)
        tab.add_column("Src", no_wrap=True)
        tab.add_column("Price", justify="right", no_wrap=True)
        tab.add_column("All-in cost", justify="right", no_wrap=True)
        tab.add_column("Fair UK", justify="right", no_wrap=True)
        tab.add_column("Title", overflow="fold", ratio=3)
        for l in best_value[:15]:
            cyc = f" {l.cycles}cyc" if l.cycles is not None else ""
            tab.add_row(f"{l.value_pct:+.0f}%", l.model_label,
                        (l.condition or "")[:12] + cyc, l.source, l.price_str,
                        f"£{l.value_landed_gbp:,.0f}", f"£{l.fair_gbp:,.0f}",
                        html_escape_rich(l.title[:80]))
        con.print(tab)
        con.print(f"[dim]...and {max(len(best_value) - 15, 0)} more in "
                  f"deals.html's Buy-for-myself view.[/dim]")
    con.print("[dim]Full clickable links are in deals.html (open it in your browser).[/dim]\n")


def html_escape_rich(s: str) -> str:
    return s.replace("[", "(").replace("]", ")")


# ----------------------------------------------------------------------------
# The dashboard website (written to deals.html locally; the same file is
# published to GitHub Pages by the cloud workflow). Self-contained: inline
# CSS + JS + data, works from file:// and from a web server alike.
# ----------------------------------------------------------------------------

def _dash_record(l: Listing, cfg: dict) -> dict:
    t = pricing.alert_thresholds(cfg, l.source)
    return {
        "key": f"{l.source}:{l.item_id}",
        "src": l.source,
        "region": pricing.region_of(l.source),
        "grade": l.grade,
        "title": l.title,
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
        "ukavg": round(l.uk_avg_gbp),
        "save": l.savings_pct,
        "fair": round(l.fair_gbp) if l.fair_gbp else None,
        "allin": round(l.value_landed_gbp) if l.value_landed_gbp else None,
        "val": l.value_pct if l.fair_gbp else None,
        "profit": round(l.flip_profit_gbp) if l.grade == "resale" else None,
        "alert": bool(l.savings_pct >= t["min"] and l.grade in ("resale", "personal")),
        "flags": list(l.flags),
        "links": [[label, url] for label, url in l.market_links],
    }


def write_html(listings: list[Listing], best_value: list[Listing], path: str,
               rates: dict, cfg: dict) -> None:
    recs: dict[str, dict] = {}
    order: list[str] = []
    for l in list(listings) + list(best_value):
        k = f"{l.source}:{l.item_id}"
        if k not in recs:
            recs[k] = _dash_record(l, cfg)
            order.append(k)
    # rank within the buy-for-myself (best value) view; None = not ranked
    for i, l in enumerate(best_value):
        recs[f"{l.source}:{l.item_id}"]["me"] = i
    for r in recs.values():
        r.setdefault("me", None)
    data = {
        "generated": int(_time.time()),
        "generated_str": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fx": {"JPY": round(rates["JPY"], 1), "USD": round(rates["USD"], 3)},
        "bars": {reg: pricing.alert_thresholds(cfg, src)["min"]
                 for reg, src in (("uk", "ebay_uk"), ("us", "ebay_us"),
                                  ("jp", "mercari"))},
        "friction": float(cfg.get("resale", {}).get("sell_friction_pct", 5)),
        "items": [recs[k] for k in order],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_DASH_TEMPLATE.replace("%%DATA%%", payload))


_DASH_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MacBook Deal Bot</title>
<style>
:root{
  --bg:#f5f6f8; --card:#ffffff; --text:#191c20; --muted:#69707a;
  --line:#e3e6ea; --accent:#0a58ca; --amber-bg:#fdf3e0; --amber:#8a5a00;
  --red-bg:#fdeaea; --red:#a11a1a; --blue-bg:#e5efff; --blue:#0a58ca;
  --g1:#0f7b3d; --g2:#3d9a63; --g3:#7c8b57; --grey:#8a919b;
  --rowhover:#f0f3f7;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#12151a; --card:#1a1f26; --text:#e8eaed; --muted:#98a1ab;
    --line:#2a313a; --accent:#7ab0ff; --amber-bg:#3a2f14; --amber:#e8c268;
    --red-bg:#42201f; --red:#ff9d97; --blue-bg:#1d3050; --blue:#9cc3ff;
    --g1:#5fd08b; --g2:#4fae74; --g3:#9dae6d; --grey:#7f8792;
    --rowhover:#20262e;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--bg);
  border-bottom:1px solid var(--line);padding:10px 18px;
  display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px}
.brand{font-size:18px;font-weight:700;white-space:nowrap}
nav{display:flex;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:3px;gap:3px}
nav button{border:0;background:none;color:var(--muted);font:inherit;
  font-weight:600;padding:7px 16px;border-radius:8px;cursor:pointer}
nav button.on{background:var(--accent);color:#fff}
.meta{color:var(--muted);font-size:13px;margin-left:auto;text-align:right}
.wrap{max-width:1280px;margin:0 auto;padding:14px 18px 40px}
.explain{color:var(--muted);margin:2px 0 12px;font-size:14px}
.filters{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;
  margin-bottom:12px}
.filters input[type=search],.filters select{font:inherit;color:var(--text);
  background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:7px 10px}
.filters input[type=search]{min-width:220px;flex:1;max-width:340px}
.filters label{color:var(--muted);font-size:14px;display:flex;
  align-items:center;gap:5px;white-space:nowrap;cursor:pointer}
.tablewrap{background:var(--card);border:1px solid var(--line);
  border-radius:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:900px}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--muted);text-align:left;padding:10px 9px;cursor:pointer;
  border-bottom:1px solid var(--line);white-space:nowrap;user-select:none}
th .dir{opacity:.8}
td{padding:10px 9px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--rowhover)}
.big{font-size:17px;font-weight:700;white-space:nowrap}
.sub{font-size:12px;color:var(--muted);white-space:nowrap}
.deal-hi{color:var(--g1)} .deal-mid{color:var(--g2)}
.deal-lo{color:var(--g3)} .deal-none{color:var(--grey)} .deal-neg{color:var(--red)}
.m1{font-weight:650}
.m2{font-size:12.5px;color:var(--muted);margin-top:1px}
.m3{font-size:12px;color:var(--muted);opacity:.85;max-width:330px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.chips{margin-top:4px;display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:11px;padding:1px 7px;border-radius:99px;white-space:nowrap;
  background:var(--amber-bg);color:var(--amber)}
.chip.red{background:var(--red-bg);color:var(--red);font-weight:600}
.chip.blue{background:var(--blue-bg);color:var(--blue);font-weight:700}
.chip.grey{background:var(--line);color:var(--muted)}
.star{color:#e3a008;font-size:14px}
.mkt{white-space:nowrap}
.grade{font-size:12px;font-weight:600;white-space:nowrap}
.grade.resale{color:var(--g1)} .grade.personal{color:var(--accent)}
.grade.good{color:var(--muted)}
.cond-t{font-size:12px;color:var(--muted);max-width:150px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.btn{display:inline-block;font-size:12px;font-weight:600;padding:3px 9px;
  margin:0 4px 4px 0;border-radius:7px;background:var(--blue-bg);
  color:var(--blue);text-decoration:none;white-space:nowrap}
.btn:hover{filter:brightness(1.06)}
.money{white-space:nowrap;font-variant-numeric:tabular-nums}
.empty{padding:40px;text-align:center;color:var(--muted)}
footer{max-width:1280px;margin:18px auto 0;padding:0 18px 30px;
  color:var(--muted);font-size:12.5px}
@media (max-width:760px){
  header{padding:10px 12px}
  .meta{margin-left:0;text-align:left;flex-basis:100%}
  .wrap{padding:10px 8px 30px}
  table{min-width:0}
  thead{display:none}
  tbody tr{display:block;border-bottom:6px solid var(--bg);padding:6px 4px}
  td{display:flex;gap:10px;border-bottom:0;padding:4px 10px}
  td::before{content:attr(data-l);flex:0 0 84px;font-size:11px;
    text-transform:uppercase;color:var(--muted);padding-top:3px}
  td .cell{flex:1;min-width:0}
  .m3{white-space:normal;max-width:none}
}
</style></head>
<body>
<header>
  <div class="brand">💻 MacBook Deal Bot</div>
  <nav id="tabs">
    <button data-tab="me">🎯 Buy for myself</button>
    <button data-tab="flip">💰 Resell for profit</button>
    <button data-tab="sav">💸 Biggest savings</button>
  </nav>
  <div class="meta" id="meta"></div>
</header>
<div class="wrap">
  <p class="explain" id="explain"></p>
  <div class="filters">
    <input id="q" type="search" placeholder="Search model or title…">
    <select id="fmodel"><option value="">All models</option></select>
    <select id="fmarket"><option value="">All markets</option></select>
    <label><input type="checkbox" id="fjis"> hide JIS keyboards</label>
    <label><input type="checkbox" id="fauction"> hide auctions</label>
    <label><input type="checkbox" id="falert"> alert-worthy only</label>
  </div>
  <div class="tablewrap"><table>
    <thead id="thead"></thead><tbody id="tbody"></tbody>
  </table></div>
</div>
<footer>
  <b>How the numbers work.</b> “Landed” = item price + proxy/forwarder fees +
  shipping + 20% UK import VAT + courier handling (UK listings: just postage).
  “All-in” additionally prices battery wear pro-rata (cycles ÷ 1000 × £249).
  “Fair UK value” is what that model costs in the UK <i>in that condition</i>:
  new = UK average, good = eBay UK sold median, like-new = midpoint.
  Flip profit assumes reselling at the UK average minus selling friction.
  Estimates are deliberately slightly pessimistic — always sanity-check the
  listing (photos, cycle count, seller) before buying. ★ = clears your
  WhatsApp alert bar.
</footer>
<script>const DATA = %%DATA%%;</script>
<script>
(function(){
"use strict";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",
  ">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>n==null?"–":"£"+Math.round(n).toLocaleString("en-GB");
const MARKET={mercari:"🇯🇵 Mercari",yahoo:"🇯🇵 Yahoo Auctions",
  rakuma:"🇯🇵 Rakuma",ebay_us:"🇺🇸 eBay US",swappa:"🇺🇸 Swappa",
  ebay_uk:"🇬🇧 eBay UK"};
const GRADE={resale:"New / unused",personal:"Like new",good:"Good"};

let tab=(location.hash||"").replace("#","");
if(tab!=="me"&&tab!=="flip"&&tab!=="sav")tab="me";
let sortKey=null,sortDir=-1;

/* ---- NEW-since-last-visit badges (localStorage) ---- */
let seen;
try{seen=new Set(JSON.parse(localStorage.getItem("mdb_seen")||"[]"));}
catch(e){seen=new Set();}
const firstVisit=seen.size===0;
const isNew=k=>!firstVisit&&!seen.has(k);
try{
  const all=new Set(seen);
  DATA.items.forEach(r=>all.add(r.key));
  localStorage.setItem("mdb_seen",JSON.stringify([...all].slice(-4000)));
}catch(e){}

/* ---- cells ---- */
function dealCls(p,hi,mid){return p==null?"deal-none":
  p<=0?"deal-neg":p>=hi?"deal-hi":p>=mid?"deal-mid":"deal-lo";}
function star(r){return r.alert?' <span class="star" title="clears your WhatsApp alert bar">★</span>':"";}
function machineCell(r){
  const spec=[r.ram?r.ram+"GB":null,
    r.ssd?(r.ssd>=1024?(r.ssd/1024)+"TB":r.ssd+"GB"):null,
    r.kbd&&r.kbd!=="unknown"?r.kbd+" keyboard":null].filter(Boolean).join(" · ");
  const chips=(r.flags||[]).map(f=>'<span class="chip'+
    (/TOO-GOOD/.test(f)?" red":"")+'">'+esc(f)+"</span>").join("");
  return '<div class="m1">'+esc(r.model)+star(r)+
    (isNew(r.key)?' <span class="chip blue">NEW</span>':"")+'</div>'+
    '<div class="m2">'+esc(spec||"spec not stated")+'</div>'+
    '<div class="m3" title="'+esc(r.title)+'">'+esc(r.title)+'</div>'+
    (chips?'<div class="chips">'+chips+'</div>':"");
}
function condCell(r){
  const cyc=r.cycles!=null?r.cycles+" cycles":"cycles ?";
  return '<div class="grade '+r.grade+'">'+GRADE[r.grade]+'</div>'+
    '<div class="sub">'+esc(cyc)+'</div>'+
    '<div class="cond-t" title="'+esc(r.cond)+'">'+esc(r.cond)+'</div>';
}
function priceCell(r){
  return '<div class="money">'+esc(r.price)+(r.auction?" 🔨":"")+'</div>'+
    (r.auction?'<div class="sub">auction bid</div>':"")+
    (r.offer?'<div class="sub">or Best Offer</div>':"");
}
function valCell(r){
  return '<span class="big '+dealCls(r.val,25,12)+'">'+
    (r.val==null?"–":Math.round(r.val)+"% off")+'</span>'+
    '<div class="sub">fair value '+fmt(r.fair)+'</div>';
}
function profitCell(r){
  const roi=r.profit!=null&&r.landed?Math.round(r.profit/r.landed*100):null;
  return '<span class="big '+dealCls(r.profit,300,120)+'">'+
    (r.profit==null?"–":(r.profit<0?"−£"+Math.abs(r.profit).toLocaleString("en-GB")
      :"£"+r.profit.toLocaleString("en-GB")))+'</span>'+
    (roi!=null?'<div class="sub">'+roi+'% ROI · save '+Math.round(r.save)+'%</div>':"");
}
function saveCell(r){
  return '<span class="big '+dealCls(r.save,35,20)+'">'+
    (r.save>0?Math.round(r.save)+"% off":"+"+Math.abs(Math.round(r.save))+"% over")
    +'</span><div class="sub">vs UK avg '+fmt(r.ukavg)+'</div>';
}
function linksCell(r){
  return (r.links||[]).map(x=>'<a class="btn" target="_blank" rel="noopener" href="'
    +esc(x[1])+'">'+esc(x[0])+"</a>").join("");
}

/* ---- column sets ---- */
const COLS={
 me:[
  {l:"Deal",s:r=>r.val??-999,c:valCell},
  {l:"Machine",s:r=>r.model,c:machineCell},
  {l:"Condition",s:r=>r.cycles??9999,c:condCell},
  {l:"Market",s:r=>r.src,c:r=>'<span class="mkt">'+(MARKET[r.src]||r.src)+'</span>'},
  {l:"Price",s:r=>r.allin??r.landed,c:priceCell},
  {l:"All-in cost",s:r=>r.allin??r.landed,
   c:r=>'<span class="big money">'+fmt(r.allin??r.landed)+'</span><div class="sub">to your door</div>'},
  {l:"Buy",s:null,c:linksCell},
 ],
 flip:[
  {l:"Est. profit",s:r=>r.profit??-9999,c:profitCell},
  {l:"Machine",s:r=>r.model,c:machineCell},
  {l:"Condition",s:r=>r.cycles??9999,c:condCell},
  {l:"Market",s:r=>r.src,c:r=>'<span class="mkt">'+(MARKET[r.src]||r.src)+'</span>'},
  {l:"Price",s:r=>r.landed,c:priceCell},
  {l:"Landed cost",s:r=>r.landed,
   c:r=>'<span class="big money">'+fmt(r.landed)+'</span><div class="sub">to your door</div>'},
  {l:"UK resale value",s:r=>r.ukavg,
   c:r=>'<span class="money">'+fmt(r.ukavg)+'</span><div class="sub">avg. for new</div>'},
  {l:"Buy",s:null,c:linksCell},
 ],
 sav:[
  {l:"Saving",s:r=>r.save,c:saveCell},
  {l:"Machine",s:r=>r.model,c:machineCell},
  {l:"Condition",s:r=>r.cycles??9999,c:condCell},
  {l:"Market",s:r=>r.src,c:r=>'<span class="mkt">'+(MARKET[r.src]||r.src)+'</span>'},
  {l:"Price",s:r=>r.landed,c:priceCell},
  {l:"Landed cost",s:r=>r.landed,
   c:r=>'<span class="big money">'+fmt(r.landed)+'</span><div class="sub">to your door</div>'},
  {l:"UK avg (new)",s:r=>r.ukavg,
   c:r=>'<span class="money">'+fmt(r.ukavg)+'</span>'},
  {l:"Buy",s:null,c:linksCell},
 ]
};
const EXPLAIN={
 me:"Every buyable listing — any market, any condition down to “good” — ranked "+
    "by how far its all-in UK cost (shipping, VAT and battery wear included) "+
    "sits below the fair UK price for that model in that condition.",
 flip:"New / unused stock only: what you'd make buying at the landed cost and "+
    "reselling at the UK average (minus "+DATA.friction+"% selling friction). "+
    "Check the flags — an auction bid or a too-good price needs extra care.",
 sav:"New / unused and like-new machines ranked by the raw saving: landed "+
    "cost vs the UK average price of a NEW unit. This is the exact number "+
    "your WhatsApp alerts fire on (★ = clears the bar: UK/US "+DATA.bars.uk+
    "%+, JP "+DATA.bars.jp+"%+)."
};

/* ---- filtering + rendering ---- */
function baseRows(){
  if(tab==="me")
    return DATA.items.filter(r=>r.me!=null).sort((a,b)=>a.me-b.me);
  if(tab==="flip")
    return DATA.items.filter(r=>r.grade==="resale").sort((a,b)=>(b.profit??-9e9)-(a.profit??-9e9));
  return DATA.items.filter(r=>r.grade==="resale"||r.grade==="personal")
    .sort((a,b)=>b.save-a.save);
}
function rows(){
  const q=$("#q").value.trim().toLowerCase();
  const fm=$("#fmodel").value,fk=$("#fmarket").value;
  let it=baseRows().filter(r=>
    (!q||(r.model+" "+r.title).toLowerCase().includes(q))&&
    (!fm||r.model===fm)&&(!fk||r.src===fk)&&
    (!$("#fjis").checked||r.kbd!=="JIS")&&
    (!$("#fauction").checked||!r.auction)&&
    (!$("#falert").checked||r.alert));
  if(sortKey!=null){
    const s=COLS[tab][sortKey].s;
    it=it.slice().sort((a,b)=>{
      const x=s(a),y=s(b);
      return (typeof x==="string"?x.localeCompare(y):x-y)*sortDir;
    });
  }
  return it;
}
function render(){
  document.querySelectorAll("nav button").forEach(b=>
    b.classList.toggle("on",b.dataset.tab===tab));
  $("#explain").textContent=EXPLAIN[tab];
  const cols=COLS[tab];
  $("#thead").innerHTML="<tr>"+cols.map((c,i)=>
    "<th data-i='"+i+"'>"+c.l+(sortKey===i?
    " <span class='dir'>"+(sortDir<0?"↓":"↑")+"</span>":"")+"</th>").join("")+"</tr>";
  const it=rows();
  $("#tbody").innerHTML=it.length?it.map(r=>"<tr>"+cols.map(c=>
    "<td data-l='"+c.l+"'><div class='cell'>"+c.c(r)+"</div></td>").join("")+"</tr>").join(""):
    "<tr><td colspan='"+cols.length+"'><div class='empty'>Nothing matches — "+
    "relax a filter, or wait for the next scan.</div></td></tr>";
  document.querySelectorAll("th").forEach(th=>th.onclick=()=>{
    const i=+th.dataset.i;
    if(!COLS[tab][i].s)return;
    sortDir=sortKey===i?-sortDir:-1;sortKey=i;render();
  });
}

/* ---- header meta ---- */
function meta(){
  const age=Math.max(0,Math.round((Date.now()/1000-DATA.generated)/60));
  const nMe=DATA.items.filter(r=>r.me!=null).length;
  const nFlip=DATA.items.filter(r=>r.grade==="resale").length;
  $("#meta").innerHTML="Updated "+esc(DATA.generated_str)+" ("+
    (age<1?"just now":age<120?age+" min ago":Math.round(age/60)+" h ago")+")<br>"+
    "£1 = ¥"+DATA.fx.JPY+" · $"+DATA.fx.USD+
    " · alerts: UK/US ≥"+DATA.bars.uk+"%, JP ≥"+DATA.bars.jp+"%"+
    " · "+nMe+" buyable · "+nFlip+" flippable";
}

/* ---- wiring ---- */
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  tab=b.dataset.tab;location.hash=tab;sortKey=null;sortDir=-1;render();
});
["q","fmodel","fmarket","fjis","fauction","falert"].forEach(id=>
  $("#"+id).addEventListener("input",render));
const models=[...new Set(DATA.items.map(r=>r.model))].sort();
$("#fmodel").innerHTML+=models.map(m=>"<option>"+esc(m)+"</option>").join("");
const mkts=[...new Set(DATA.items.map(r=>r.src))];
$("#fmarket").innerHTML+=mkts.map(s=>"<option value='"+s+"'>"+
  (MARKET[s]||s)+"</option>").join("");
meta();render();
/* served over http(s) (e.g. GitHub Pages): pick up fresh scans automatically */
if(location.protocol.indexOf("http")===0)
  setInterval(()=>location.reload(),10*60*1000);
})();
</script>
</body></html>
"""


def write_csv(listings: list[Listing], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["grade", "savings_pct", "value_pct", "flip_profit_gbp",
                    "model", "source", "ram_gb", "storage_gb", "keyboard",
                    "price", "currency", "landed_gbp", "value_landed_gbp",
                    "fair_gbp", "uk_avg_gbp", "condition", "cycles", "auction",
                    "flags", "title", "links"])
        for l in listings:
            w.writerow([l.grade, l.savings_pct, l.value_pct, l.flip_profit_gbp,
                        l.model_label, l.source, l.ram_gb, l.storage_gb,
                        l.keyboard, l.price, l.currency, l.landed_gbp,
                        l.value_landed_gbp, l.fair_gbp, l.uk_avg_gbp,
                        l.condition, l.cycles, l.is_auction,
                        "; ".join(l.flags), l.title,
                        " | ".join(url for _, url in l.market_links)])


def whatsapp_message(l: Listing, cfg: dict) -> str:
    """Plain-text alert for WhatsApp (CallMeBot). *asterisks* render bold."""
    t = pricing.alert_thresholds(cfg, l.source)
    fire = ("🔥 INCREDIBLE DEAL" if l.savings_pct >= t["hot"] else "✅ Good deal")
    tier = ("🏪 resale-grade" if l.grade == "resale"
            else "🎯 practically new, buy-to-use")
    src = {"ebay_us": "eBay US 🇺🇸", "swappa": "Swappa 🇺🇸",
           "mercari": "Mercari 🇯🇵", "yahoo": "Yahoo 🇯🇵",
           "rakuma": "Rakuma 🇯🇵", "ebay_uk": "eBay UK 🇬🇧"}.get(l.source, l.source)
    spec = " / ".join(x for x in [
        f"{l.ram_gb}GB" if l.ram_gb else "RAM?",
        (f"{l.storage_gb // 1024}TB" if l.storage_gb and l.storage_gb >= 1024
         else (f"{l.storage_gb}GB" if l.storage_gb else "SSD?")),
        f"kbd {l.keyboard}",
    ])
    cyc = (f"{l.cycles} battery cycles" if l.cycles is not None
           else "battery cycles unknown - check listing")
    lines = [
        f"{fire} ({tier})",
        f"*MacBook Pro {l.model_label}* — {src}",
        f"Save ~*{l.savings_pct:.0f}%* — landed est. *£{l.landed_gbp:,.0f}* "
        f"vs UK avg £{l.uk_avg_gbp:,.0f}",
    ]
    if l.grade == "resale" and l.flip_profit_gbp > 0:
        lines.append(f"Est. flip profit ~£{l.flip_profit_gbp:,.0f} "
                     f"if resold at UK avg")
    lines.append(f"{l.price_str}{' (auction bid)' if l.is_auction else ''}"
                 f" · {spec}")
    lines.append(f"{l.condition} · {cyc}")
    if l.flags:
        lines.append("⚠️ " + "; ".join(l.flags))
    lines.append(l.title[:120])
    lines.append("")
    for label, url in l.market_links:
        lines.append(f"{label}: {url}")
    return "\n".join(lines)
