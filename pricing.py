"""
pricing.py - model detection from Japanese listing titles, currency conversion,
landed-cost maths and deal scoring.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import requests

# ----------------------------------------------------------------------------
# Listing dataclass shared across the project
# ----------------------------------------------------------------------------

@dataclass
class Listing:
    item_id: str               # e.g. "m12345678901" or "x1234567890"
    source: str                # "mercari" | "yahoo" | "rakuma"
    title: str
    price_jpy: int
    is_auction: bool = False   # True = auction current bid (price may rise)
    condition: str = ""        # text such as "新品、未使用"
    url: str = ""              # original marketplace URL
    buyee_path: str = ""       # exact Buyee item URL captured while scraping
                               # (used for sources where the ID format varies)
    # filled in during analysis:
    model_id: Optional[str] = None
    model_label: str = ""
    chip: str = ""
    size: Optional[int] = None
    size_guessed: bool = False
    ram_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    keyboard: str = "unknown"  # "US" | "JIS" | "unknown"
    cycles: Optional[int] = None
    landed_gbp: float = 0.0
    uk_avg_gbp: float = 0.0
    savings_pct: float = 0.0
    flags: list = field(default_factory=list)

    @property
    def buyee_url(self) -> str:
        if self.buyee_path:
            if self.buyee_path.startswith("http"):
                return self.buyee_path
            return "https://buyee.jp" + self.buyee_path
        if self.source == "mercari":
            return f"https://buyee.jp/mercari/item/{self.item_id}"
        if self.source == "rakuma":
            return f"https://buyee.jp/item/rakuma/{self.item_id}"
        return f"https://buyee.jp/item/yahoo/auction/{self.item_id}"

    @property
    def zenmarket_url(self) -> str:
        if self.source == "mercari":
            return f"https://zenmarket.jp/en/mercariproduct.aspx?itemCode={self.item_id}"
        if self.source == "rakuma":
            # ZenMarket also proxies Rakuma; if the code shape ever drifts this
            # still lands on a valid search for the item.
            return f"https://zenmarket.jp/en/rakuma.aspx?itemCode={self.item_id}"
        return f"https://zenmarket.jp/en/auction.aspx?itemCode={self.item_id}"

    @property
    def original_url(self) -> str:
        if self.url:
            return self.url
        if self.source == "mercari":
            return f"https://jp.mercari.com/item/{self.item_id}"
        if self.source == "rakuma":
            return f"https://item.fril.jp/{self.item_id}"
        return f"https://page.auctions.yahoo.co.jp/jp/auction/{self.item_id}"


# ----------------------------------------------------------------------------
# Title normalisation + model detection
# ----------------------------------------------------------------------------

RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128}
STORAGE_GB_SIZES = {256, 512}

def normalise(text: str) -> str:
    """Full-width -> half-width, uppercase, katakana chip words -> latin."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.upper()
    t = t.replace("プロ", " PRO ").replace("マックス", " MAX ")
    t = t.replace("インチ", "INCH").replace("型", "INCH")
    return t

def parse_listing_specs(listing: Listing) -> None:
    """Fill chip / size / ram / storage / keyboard on the listing in place."""
    t = normalise(listing.title)

    # Must look like a MacBook Pro, not an Air / Neo / accessory
    if "MACBOOK" not in t:
        return
    if "AIR" in t or "NEO" in t:
        return

    # --- chip ---------------------------------------------------------------
    m = re.search(r"\bM([2-5])\s*[-/]?\s*(PRO|MAX)?\b", t)
    if not m:
        return
    gen = int(m.group(1))
    variant = m.group(2) or ""
    # A plain "M2" MacBook Pro (13-inch) is OUT of scope; M2 PRO/MAX are in.
    if gen == 2 and not variant:
        return
    listing.chip = f"M{gen} {variant}".strip()

    # --- RAM / storage (strip them out before looking for screen size) -------
    storage_gb = None
    tb = re.search(r"\b([1248])\s*TB\b", t)
    if tb:
        storage_gb = int(tb.group(1)) * 1024
    gb_values = [int(x) for x in re.findall(r"\b(\d{2,4})\s*GB\b", t)]
    ram = None
    for v in gb_values:
        if v in RAM_SIZES and ram is None:
            ram = v
        elif v in STORAGE_GB_SIZES and storage_gb is None:
            storage_gb = v
    # lone "512GB" with no RAM figure: don't mistake storage for RAM
    listing.ram_gb = ram
    listing.storage_gb = storage_gb

    # --- screen size ----------------------------------------------------------
    t_nosizes = re.sub(r"\b\d{1,4}\s*(?:GB|TB)\b", " ", t)
    s = re.search(r"(?<!\d)(14|16)(?:[.]2)?(?!\d)", t_nosizes)
    if s:
        listing.size = int(s.group(1))
    else:
        listing.size = 14          # conservative default: compare vs the cheaper size
        listing.size_guessed = True

    # --- keyboard layout -------------------------------------------------------
    if re.search(r"US\s*配列|US\s*キー|英語\s*配列|英字\s*配列|US\s*KEYBOARD", t):
        listing.keyboard = "US"
    elif re.search(r"\bJIS\b|日本語\s*配列", t):
        listing.keyboard = "JIS"
    else:
        listing.keyboard = "unknown"

    # sellers often put the cycle count right in the title - grab it for free
    if listing.cycles is None:
        listing.cycles = find_cycle_count(listing.title)


def match_model(listing: Listing, models: list[dict]) -> None:
    """Attach the matching config model (chip + size) to the listing."""
    for mdl in models:
        if mdl["chip"].upper() == listing.chip and int(mdl["size"]) == listing.size:
            listing.model_id = mdl["id"]
            listing.model_label = f'{mdl["chip"]} {mdl["size"]}"'
            listing.uk_avg_gbp = float(mdl["uk_avg_gbp"])
            return


# ----------------------------------------------------------------------------
# Exclusion filter
# ----------------------------------------------------------------------------

def is_excluded(title: str, exclude_keywords: list[str]) -> Optional[str]:
    t = normalise(title)
    for kw in exclude_keywords:
        if normalise(kw) in t:
            return kw
    return None


# ----------------------------------------------------------------------------
# FX
# ----------------------------------------------------------------------------

def get_jpy_per_gbp(fallback: float) -> tuple[float, str]:
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "GBP", "to": "JPY"},
            timeout=10,
        )
        r.raise_for_status()
        rate = float(r.json()["rates"]["JPY"])
        return rate, "live"
    except Exception:
        return float(fallback), "fallback (edit fx.fallback_jpy_per_gbp in config.yaml)"


# ----------------------------------------------------------------------------
# Landed cost + scoring
# ----------------------------------------------------------------------------

def landed_cost_gbp(price_jpy: int, source: str, cfg: dict, jpy_per_gbp: float) -> float:
    c = cfg["costs"]
    dom = c["domestic_shipping_jpy"].get(source, 0)
    total_jpy = price_jpy + c["proxy_fee_jpy"] + dom + c["intl_shipping_jpy"]
    gbp = total_jpy / jpy_per_gbp
    if c.get("apply_uk_import", True):
        gbp = gbp * (1 + c["uk_vat_pct"] / 100.0) + c["courier_handling_gbp"]
    return round(gbp, 2)


def score(listing: Listing, cfg: dict, jpy_per_gbp: float) -> None:
    listing.landed_gbp = landed_cost_gbp(listing.price_jpy, listing.source, cfg, jpy_per_gbp)
    if listing.uk_avg_gbp:
        listing.savings_pct = round(
            (listing.uk_avg_gbp - listing.landed_gbp) / listing.uk_avg_gbp * 100, 1
        )
    a = cfg["alerts"]
    # Price-sanity backstop: a whole MacBook never sells for a small fraction
    # of its UK value - that's a part (screen/board/etc.) or an accessory the
    # keyword list didn't catch. Flag (and below, suppress alerts) when the
    # JP price is implausibly low for the model. Default 0.30 = 30% of UK avg;
    # genuine bargains (even -50% landed) sit far above this floor.
    floor_ratio = a.get("implausible_price_ratio", 0.30)
    if listing.uk_avg_gbp and jpy_per_gbp:
        jp_gbp = listing.price_jpy / jpy_per_gbp   # bare item price, no fees
        if jp_gbp < listing.uk_avg_gbp * floor_ratio:
            listing.flags.append(
                "PRICE TOO LOW for a whole unit - likely a part/accessory, not the Mac")
    if listing.savings_pct >= a["too_good_pct"]:
        listing.flags.append("TOO-GOOD? verify carefully (box-only/scam/mislabel risk)")
    if listing.is_auction:
        listing.flags.append("auction - current bid, price can rise")
    if listing.size_guessed:
        listing.flags.append('size not stated - assumed 14"')
    if listing.keyboard == "JIS":
        listing.flags.append("JIS (Japanese) keyboard")
    elif listing.keyboard == "unknown":
        listing.flags.append("keyboard layout unknown (likely JIS)")
    if listing.cycles is not None and listing.cycles > a["max_battery_cycles"]:
        listing.flags.append(f"battery cycles {listing.cycles} > your max {a['max_battery_cycles']}")


CYCLE_RE = re.compile(
    r"(?:充放電回数|充放電|サイクル数|サイクルカウント|サイクル|CYCLE\s*COUNTS?|CYCLES?)\D{0,8}?(\d{1,4})\s*回?",
    re.IGNORECASE,
)

def find_cycle_count(text: str) -> Optional[int]:
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text).upper()
    m = CYCLE_RE.search(t)
    if m:
        try:
            n = int(m.group(1))
            if 0 <= n <= 3000:
                return n
        except ValueError:
            pass
    return None
