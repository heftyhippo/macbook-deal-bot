"""
pricing.py - product-family + model detection from listing titles (Japanese
and English), currency conversion, landed-cost maths and deal scoring.

Families covered (all 2022-or-later Apple models):
  macbook     MacBook Pro 14/16 (M2 Pro generation onwards)
  mac_mini    Mac mini (M2 / M2 Pro / M4 / M4 Pro)
  mac_studio  Mac Studio (M1 Max/Ultra, M2 Max/Ultra, M4 Max, M3 Ultra)
  imac        iMac 24" (M3 / M4)
  mac_pro     Mac Pro (M2 Ultra)
  display     Apple Studio Display
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import requests

# ----------------------------------------------------------------------------
# Sources: currency, fee route and alert region
# ----------------------------------------------------------------------------

SOURCE_CURRENCY = {"mercari": "JPY", "yahoo": "JPY", "rakuma": "JPY",
                   "paypay": "JPY", "ebay_us": "USD", "swappa": "USD",
                   "ebay_uk": "GBP", "gumtree": "GBP",
                   "ebay_de": "EUR"}
JP_SOURCES = {"mercari", "yahoo", "rakuma", "paypay"}
US_SOURCES = {"ebay_us", "swappa"}
UK_SOURCES = {"ebay_uk", "gumtree"}
EU_SOURCES = {"ebay_de"}

CURRENCY_SYMBOL = {"JPY": "¥", "USD": "$", "GBP": "£", "EUR": "€"}

# alert thresholds are per REGION (JP prices genuinely run lower, so its bar
# is higher) and split by whether the product carries a keyboard - a JIS
# keyboard hurts UK resale on a MacBook/iMac, but a Mac mini from
# Japan is the same product you'd buy here (see alerts.* in config.yaml)
REGION_OF_SOURCE = {"mercari": "jp", "yahoo": "jp", "rakuma": "jp",
                    "paypay": "jp", "ebay_us": "us", "swappa": "us",
                    "ebay_uk": "uk", "gumtree": "uk",
                    "ebay_de": "eu"}

# families whose box includes a keyboard (iMac ships with a Magic Keyboard)
KEYBOARD_FAMILIES = {"macbook", "imac"}

FAMILY_NAME = {"macbook": "MacBook Pro", "mac_mini": "Mac mini",
               "mac_studio": "Mac Studio", "imac": "iMac 24\"",
               "mac_pro": "Mac Pro", "display": "Studio Display"}

_THRESHOLD_DEFAULTS = {
    True:  {"uk": {"min": 35, "hot": 40, "too_good": 50},    # with keyboard
            "us": {"min": 35, "hot": 40, "too_good": 50},
            "eu": {"min": 38, "hot": 43, "too_good": 53},
            "jp": {"min": 50, "hot": 55, "too_good": 65}},
    False: {"uk": {"min": 35, "hot": 40, "too_good": 50},    # keyboardless
            "us": {"min": 35, "hot": 40, "too_good": 50},
            "eu": {"min": 35, "hot": 40, "too_good": 50},
            "jp": {"min": 42, "hot": 47, "too_good": 57}},
}


def region_of(source: str) -> str:
    return REGION_OF_SOURCE.get(source, "uk")


def alert_thresholds(cfg: dict, source: str, family: str = "macbook") -> dict:
    """{'min', 'hot', 'too_good'} savings-% thresholds for this source's
    region and this product family (keyboarded products carry a higher JP/EU
    bar - a JIS/QWERTZ keyboard is a real resale handicap; keyboardless
    products only pay a small forwarding-hassle premium)."""
    a = cfg.get("alerts", {})
    # The current scoring model already prices keyboard layout, condition and
    # import friction into one expected-price comparison.  When the flat keys
    # are present they are therefore the one alert bar everywhere.  Keep the
    # older regional tables as a backwards-compatible fallback for existing
    # configs that have not moved to the unified model yet.
    flat = (("min_savings_pct", "min"),
            ("hot_savings_pct", "hot"),
            ("too_good_pct", "too_good"))
    if any(old in a for old, _ in flat):
        base = {"min": 35.0, "hot": 40.0, "too_good": 55.0}
        for old, new in flat:
            if old in a:
                base[new] = float(a[old])
        return base
    reg = region_of(source)
    kb = family in KEYBOARD_FAMILIES
    base = dict(_THRESHOLD_DEFAULTS[kb][reg])
    table = a.get("regions" if kb else "regions_no_keyboard") or {}
    # keyboardless table falls back to the keyboarded one for regions it
    # doesn't override (uk/us are usually identical)
    if not kb and reg not in table:
        table = a.get("regions") or {}
    for k, v in (table.get(reg) or {}).items():
        if k in base:
            base[k] = float(v)
    return base


def global_min_alert_pct(cfg: dict) -> float:
    """The lowest alert bar across all regions and both keyboard classes -
    below it nothing alerts."""
    mins = []
    for src in REGION_OF_SOURCE:
        for fam in ("macbook", "mac_mini"):
            mins.append(alert_thresholds(cfg, src, fam)["min"])
    return min(mins)


# ----------------------------------------------------------------------------
# Listing dataclass shared across the project
# ----------------------------------------------------------------------------

@dataclass
class Listing:
    item_id: str               # e.g. "m12345678901" or "x1234567890"
    source: str                # one of SOURCE_CURRENCY's keys
    title: str
    price: float               # in the source's native currency (see currency)
    is_auction: bool = False   # True = auction current bid (price may rise)
    condition: str = ""        # text such as "新品、未使用" or "Open box"
    currency: str = "JPY"      # "JPY" | "USD" | "GBP" | "EUR"
    best_offer: bool = False   # eBay "or Best Offer" - real price may be lower
    grade: str = "resale"      # "resale" (new/unused, arbitrage-safe) |
                               # "personal" (like new, zero visible wear) |
                               # "good" (light wear - only if value tier on)
    url: str = ""              # original marketplace URL
    buyee_path: str = ""       # exact Buyee item URL captured while scraping
    description: str = ""      # search-card/detail description when available
    location: str = ""         # useful for collection-only classifieds
    # filled in during analysis:
    family: str = ""           # product family (see FAMILY_NAME)
    model_id: Optional[str] = None
    model_label: str = ""
    chip: str = ""
    size: Optional[float] = None
    size_guessed: bool = False
    ram_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    keyboard: str = "unknown"  # "US" | "UK" | "JIS" | "EU" | "unknown"
    cycles: Optional[int] = None
    landed_gbp: float = 0.0
    uk_avg_gbp: float = 0.0
    savings_pct: float = 0.0
    expected_price_gbp: float = 0.0  # canonical UK value for exact condition/spec
    expected_price_basis: str = ""
    savings_gbp: float = 0.0
    benchmark_confidence: float = 0.0  # 0..1, quality of the expected-price evidence
    listing_confidence: float = 0.0    # 0..1, match/buyability confidence
    overall_score: float = 0.0         # 0..100 confidence-weighted deal score
    snapshot_age_minutes: int = 0      # >0 when retained from a previous source scan
    # condition-aware analysis:
    uk_used_gbp: float = 0.0        # eBay-UK sold median for USED units
    fair_gbp: float = 0.0           # fair UK value for THIS condition
    value_landed_gbp: float = 0.0   # landed + pro-rated battery wear cost
    value_pct: float = 0.0          # % below fair value
    spec_adj_gbp: float = 0.0       # benchmark shift for above/below-base spec
    flip_profit_gbp: float = 0.0    # est. profit reselling (resale + personal)
    flip_target_gbp: float = 0.0    # the price the flip assumes you sell at
    flags: list = field(default_factory=list)

    @property
    def price_str(self) -> str:
        """Native price formatted for display, e.g. ¥218,000 / $1,499 / £999."""
        return f"{CURRENCY_SYMBOL.get(self.currency, '')}{self.price:,.0f}"

    @property
    def market_links(self) -> list:
        """(label, url) purchase links appropriate to the source's market."""
        if self.source in JP_SOURCES:
            links = [("Buyee", self.buyee_url)]
            if self.source != "paypay":       # ZenMarket doesn't proxy PayPay
                links.append(("ZenMarket", self.zenmarket_url))
            links.append(("Original", self.original_url))
            return links
        label = {"ebay_us": "eBay US", "swappa": "Swappa",
                 "ebay_uk": "eBay UK", "ebay_de": "eBay DE",
                 "gumtree": "Gumtree",
                 }.get(self.source, "Listing")
        return [(label, self.original_url)]

    @property
    def buyee_url(self) -> str:
        if self.source not in JP_SOURCES:
            return ""
        if self.buyee_path:
            if self.buyee_path.startswith("http"):
                return self.buyee_path
            return "https://buyee.jp" + self.buyee_path
        if self.source == "mercari":
            return f"https://buyee.jp/mercari/item/{self.item_id}"
        if self.source == "rakuma":
            return f"https://buyee.jp/item/rakuma/{self.item_id}"
        if self.source == "paypay":
            return f"https://buyee.jp/paypayfleamarket/item/{self.item_id}"
        return f"https://buyee.jp/item/yahoo/auction/{self.item_id}"

    @property
    def zenmarket_url(self) -> str:
        if self.source not in JP_SOURCES:
            return ""
        if self.source == "mercari":
            return f"https://zenmarket.jp/en/mercariproduct.aspx?itemCode={self.item_id}"
        if self.source == "rakuma":
            return f"https://zenmarket.jp/en/rakuma.aspx?itemCode={self.item_id}"
        return f"https://zenmarket.jp/en/auction.aspx?itemCode={self.item_id}"

    @property
    def original_url(self) -> str:
        if self.url:
            return self.url
        if self.source == "ebay_us":
            return f"https://www.ebay.com/itm/{self.item_id}"
        if self.source == "ebay_uk":
            return f"https://www.ebay.co.uk/itm/{self.item_id}"
        if self.source == "ebay_de":
            return f"https://www.ebay.de/itm/{self.item_id}"
        if self.source == "swappa":
            return f"https://swappa.com/listing/view/{self.item_id}"
        if self.source == "mercari":
            return f"https://jp.mercari.com/item/{self.item_id}"
        if self.source == "rakuma":
            return f"https://item.fril.jp/{self.item_id}"
        if self.source == "paypay":
            return f"https://paypayfleamarket.yahoo.co.jp/item/{self.item_id}"
        return f"https://page.auctions.yahoo.co.jp/jp/auction/{self.item_id}"


# ----------------------------------------------------------------------------
# Title normalisation + family / model detection
# ----------------------------------------------------------------------------

# Bare 256/512GB values still mean storage, but explicitly labelled
# "512GB unified memory" is valid on a current Mac Studio.  The parser below
# handles those high-memory values only when RAM/memory context is present.
RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 512}
MAC_STORAGE_SIZES = {256, 512, 1024, 2048, 4096, 8192, 16384}

def normalise(text: str) -> str:
    """Full-width -> half-width, uppercase, katakana product words -> latin."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.upper()
    t = (t.replace("プロ", " PRO ").replace("マックス", " MAX ")
          .replace("ウルトラ", " ULTRA ").replace("スタジオ", " STUDIO ")
          .replace("ミニ", " MINI ").replace("アイパッド", " IPAD ")
          .replace("ディスプレイ", " DISPLAY "))
    t = t.replace("インチ", "INCH").replace("型", "INCH")
    return re.sub(r"\s+", " ", t)


# chips valid per family (2022-or-later models only)
FAMILY_CHIPS = {
    "macbook":    {"M2 PRO", "M2 MAX", "M3", "M3 PRO", "M3 MAX",
                   "M4", "M4 PRO", "M4 MAX", "M5", "M5 PRO", "M5 MAX"},
    "mac_mini":   {"M2", "M2 PRO", "M4", "M4 PRO"},
    "mac_studio": {"M1 MAX", "M1 ULTRA", "M2 MAX", "M2 ULTRA",
                   "M4 MAX", "M3 ULTRA"},
    "imac":       {"M3", "M4"},
    "mac_pro":    {"M2 ULTRA"},
    "display":    set(),           # Studio Display has no M chip
}

CHIP_RE = re.compile(r"\bM([1-5])\s*[-/]?\s*(PRO|MAX|ULTRA)?\b")


# A title that starts with an accessory/part name is selling that item, not a
# complete Mac or Studio Display.  This is deliberately multilingual because
# the same parser protects every marketplace.
_ACCESSORY_LEAD_RE = re.compile(
    r"^\W*(?:APPLE\s+)?(?:MAGIC\s+)?(?:KEYBOARD|TASTATUR|CLAVIER|PENCIL|"
    r"SMART\s+FOLIO|FOLIO|CASE|COVER|HÜLLE|SLEEVE|SKIN|STAND|DOCK|HUB|"
    r"ENCLOSURE|HOUSING|MOUNT|ADAPTER|MOUSE|TRACKPAD|CHARGER|NETZTEIL|"
    r"LADEGERÄT|CHASSIS|SHELL|LOGIC\s*BOARD|MOTHERBOARD|DISPLAY\s+ASSEMBLY|"
    r"SCREEN\s+ASSEMBLY|SSD\s+(?:EXPANSION\s+)?(?:CARD|MODULE|BOARD))\b")

# Product-name-first titles can still be parts: "Studio Display rear shell"
# and "Mac mini SSD expansion card" were both observed in live top results.
_ACC_NOUN_DISPLAY_RE = re.compile(
    r"FLOOR\s*STAND|DESK\s*STAND|WALL\s*MOUNT|VESA|MOUNT\s*ADAPTER|"
    r"REAR\s*(?:SHELL|CASE|HOUSING)|CHASSIS|HOUSING|SHELL|CASE|COVER|"
    r"DISPLAY\s*ASSEMBLY|SCREEN\s*ASSEMBLY|LCD\s*PANEL|GLASS\s*PANEL|"
    r"LOGIC\s*BOARD|CAMERA\s*MODULE|POWER\s*SUPPLY|CABLE|"
    r"スタンドのみ|マウント|筐体|外装|部品")
_ACC_NOUN_DESKTOP_RE = re.compile(
    r"\b(?:DOCK(?:ING\s+STATION)?|HUB|ENCLOSURE|HOUSING|CASE|SHELL|"
    r"CHASSIS|DESK\s*STAND|WALL\s*MOUNT|VESA\s*MOUNT|MOUNTING\s*BRACKET|"
    r"EXPANSION\s+(?:CARD|MODULE|BOARD|TOWER)|SSD\s+(?:EXPANSION\s+)?"
    r"(?:CARD|MODULE|BOARD|ENCLOSURE|ADAPTER)|NVME\s+(?:ENCLOSURE|DOCK)|"
    r"LOGIC\s*BOARD|MOTHERBOARD|POWER\s*SUPPLY)\b|"
    r"内蔵SSD拡張カード|SSD拡張|拡張タワー|外付け|筐体|ケース|"
    r"ドック(?:ステーション)?|エンクロージャー|冷却ファン|ハブ")
_ACC_NOUN_MACBOOK_RE = re.compile(
    r"\b(?:TOP\s*CASE|BOTTOM\s*CASE|PALM\s*REST|DISPLAY\s*ASSEMBLY|"
    r"SCREEN\s*ASSEMBLY|LCD\s*PANEL|LOGIC\s*BOARD|MOTHERBOARD|"
    r"REPLACEMENT\s+(?:SCREEN|BATTERY|KEYBOARD)|BATTERY\s+ONLY|"
    r"KEYBOARD\s+ONLY|TRACKPAD\s+ONLY|CHASSIS|HOUSING|SHELL)\b|"
    r"液晶パネル|ロジックボード|マザーボード|部品|交換用|修理用")
_BUNDLE_MARK_RE = re.compile(
    r"セット|付き|付属|付|同梱|おまけ|本体|INCLUDED|INCLUDES|\bWITH\b|"
    r"[＋+＆&]")
_LEGACY_MAC_RE = re.compile(
    r"\bINTEL\b|\bCORE\s*I[3579]\b|\b(?:EARLY|MID|LATE)\s*20(?:0\d|1\d|2[01])\b")
_CURRENT_DISPLAY_RE = re.compile(
    r"\bAPPLE\s+STUDIO\s+DISPLAY\b|\bA2525\b|\b5K\b|"
    r"(?:\b27(?:[.]0)?\s*(?:INCH|[\"”])?.{0,24}\bSTUDIO\s+DISPLAY\b|"
    r"\bSTUDIO\s+DISPLAY\b.{0,24}\b27(?:[.]0)?\s*(?:INCH|[\"”])?)")
_LEGACY_DISPLAY_RE = re.compile(
    r"\bVINTAGE\b|\bPOWER\s+MAC\b|\bG4\b|\bM4551\b|\bM7768\b|"
    r"\b(?:15|17|20|21|23)\s*(?:INCH|[\"”])")


def _is_accessory_only(t: str, fam: str) -> bool:
    """True when a title is selling a part/accessory rather than a device.

    A bundle marker keeps a real computer sold *with* an accessory only when
    the marker appears before the accessory noun.  If the accessory is named
    first ("dock with Mac mini compatibility"), the accessory is the item.
    """
    if fam == "display":
        rx = _ACC_NOUN_DISPLAY_RE
    elif fam in ("mac_mini", "mac_studio", "mac_pro", "imac"):
        rx = _ACC_NOUN_DESKTOP_RE
    elif fam == "macbook":
        rx = _ACC_NOUN_MACBOOK_RE
    else:
        return False
    match = rx.search(t)
    if not match:
        return False
    bundle = _BUNDLE_MARK_RE.search(t)
    return not bundle or bundle.start() > match.start()


def is_complete_apple_product(title: str, family: str = "") -> bool:
    """Return whether a title describes a complete tracked Apple device.

    This public gate is used for both fresh and cached listings so a stale
    accessory can never survive merely because an earlier parser accepted it.
    """
    t = normalise(title)
    fam = family or _detect_family(t)
    if not fam or fam not in FAMILY_CHIPS:
        return False
    if _ACCESSORY_LEAD_RE.search(t) or _is_accessory_only(t, fam):
        return False
    if fam != "display" and _LEGACY_MAC_RE.search(t):
        return False
    if fam == "display":
        return bool(_CURRENT_DISPLAY_RE.search(t)
                    and not _LEGACY_DISPLAY_RE.search(t))
    return True


def _detect_family(t: str) -> str:
    """Product family from a normalised title; '' = not a product we track."""
    if _ACCESSORY_LEAD_RE.search(t):
        return ""
    if "MACBOOK" in t:
        # MacBook Air is out of scope (and 13/15-inch sizes are Airs)
        return "" if "AIR" in t else "macbook"
    if "STUDIO DISPLAY" in t:
        return "display"
    if "MAC STUDIO" in t:
        return "mac_studio"
    if "MAC MINI" in t or "MACMINI" in t:
        return "mac_mini"
    if "IMAC" in t:
        return "imac"
    if re.search(r"\bMAC\s*PRO\b", t):
        return "mac_pro"
    return ""


def parse_listing_specs(listing: Listing) -> None:
    """Fill family / chip / size / ram / storage / keyboard in place.
    A listing that doesn't parse to a tracked family keeps family='' and is
    dropped by the caller."""
    # A last-good snapshot may have been parsed by an older release. Reset the
    # derived identity fields so revalidation cannot retain a stale match.
    listing.family = ""
    listing.model_id = None
    listing.model_label = ""
    listing.chip = ""
    listing.size = None
    listing.size_guessed = False
    listing.ram_gb = None
    listing.storage_gb = None
    listing.spec_adj_gbp = 0.0
    title_t = normalise(listing.title)
    # Search-result descriptions are especially useful on classifieds, where
    # sellers often put the exact memory/storage/condition below a terse title.
    # Family/accessory detection remains title-only so a comparison phrase in
    # the description cannot turn one product into another.
    t = normalise(" ".join(x for x in (listing.title, listing.description) if x))

    fam = _detect_family(title_t)
    if not fam:
        return
    if not is_complete_apple_product(listing.title, fam):
        return    # a keyboard/case/pencil/stand named after the device

    # --- chip --------------------------------------------------------------
    chip = ""
    m = CHIP_RE.search(t)
    if m:
        chip = f"M{m.group(1)} {m.group(2) or ''}".strip()
    if fam == "display":
        chip = ""                   # no chip - and none required
    elif chip not in FAMILY_CHIPS[fam]:
        return                      # pre-2022, MacBook Air-class, or unknown
    listing.family = fam
    listing.chip = chip

    # --- RAM / storage (strip them out before looking for sizes) ------------
    storage_gb = None
    tb = re.search(r"\b(1|2|4|8|16)\s*TB\b", t)
    if tb:
        storage_gb = int(tb.group(1)) * 1024
    gb_values = [int(x) for x in re.findall(r"\b(\d{2,4})\s*GB\b", t)]
    ram = None
    # Context wins for unusually large unified-memory configurations.
    rm = (re.search(r"\b(\d{1,3})\s*GB\s+(?:UNIFIED\s+)?(?:RAM|MEMORY)\b", t)
          or re.search(r"\b(?:RAM|MEMORY)\s*[:=-]?\s*(\d{1,3})\s*GB\b", t))
    if rm and int(rm.group(1)) in RAM_SIZES:
        ram = int(rm.group(1))
    sm = (re.search(r"\b(\d{2,5})\s*GB\s+(?:SSD|STORAGE|DRIVE)\b", t)
          or re.search(r"\b(?:SSD|STORAGE)\s*[:=-]?\s*(\d{2,5})\s*GB\b", t))
    if sm and int(sm.group(1)) in MAC_STORAGE_SIZES:
        storage_gb = int(sm.group(1))
    for v in gb_values:
        if fam == "display":
            continue
        if v in RAM_SIZES and v not in (256, 512) and ram is None:
            ram = v
        elif v in MAC_STORAGE_SIZES and storage_gb is None:
            storage_gb = v
    listing.ram_gb = ram
    listing.storage_gb = storage_gb

    # --- size / variant ------------------------------------------------------
    t_nosizes = re.sub(r"\b\d{1,4}\s*(?:GB|TB)\b", " ", t)
    # core counts ("12C CPU", "16-core GPU", "24コア") are not sizes
    t_nosizes = re.sub(r"\b\d{1,2}\s*[-‐–—]?\s*(?:C\b|CORES?|コア)", " ", t_nosizes)

    if fam == "macbook":
        s = re.search(r"(?<!\d)(14|16)(?:[.]2)?(?!\d)", t_nosizes)
        if s:
            listing.size = int(s.group(1))
        else:
            # an explicit 13"/15" with no 14/16 anywhere is a MacBook Air (or
            # an old 13" Pro) whose title just skips the word "Air"
            if re.search(r"(?<!\d)(13|15)(?:[.]\d)?\s*(?:INCH|[\"”])", t_nosizes):
                listing.family = listing.chip = ""
                return
            listing.size = None    # choose the lowest compatible benchmark later
            listing.size_guessed = True
    elif fam == "imac":
        listing.size = 24          # every Apple-silicon iMac is 24"
    # mac_mini / mac_studio / mac_pro / display have no size dimension

    # --- keyboard layout (only meaningful when a keyboard is in the box) -----
    if fam in KEYBOARD_FAMILIES:
        if re.search(r"US\s*配列|US\s*キー|英語\s*配列|英字\s*配列|US\s*KEYBOARD", t):
            listing.keyboard = "US"
        elif re.search(r"\bJIS\b|日本語\s*配列|JAPANESE\s*KEY", t):
            listing.keyboard = "JIS"
        elif re.search(r"\b(?:SWEDISH|GERMAN|FRENCH|ITALIAN|SPANISH|DANISH|"
                       r"NORWEGIAN|NORDIC|BELGIAN|PORTUGUESE|QWERTZ|AZERTY)\b", t):
            listing.keyboard = "EU"    # non-UK ISO layout sold cross-border
    else:
        listing.keyboard = "n/a"       # no keyboard in the box

    # sellers often put the battery cycle count right in the title
    if listing.cycles is None and fam == "macbook":
        listing.cycles = find_cycle_count(t)


_BASE_SPEC_RE = re.compile(r"(\d+)\s*GB.*?/\s*(\d+)\s*(GB|TB)")


def _capacity_value(capacity: int, table: dict[int, float]) -> float:
    """Return a monotonic value for an SSD capacity map.

    Exact configured values win.  Missing capacities are linearly
    interpolated (or extrapolated from the nearest two points) instead of
    silently becoming zero, which used to make an 8TB machine worth *less*
    than its base configuration.
    """
    if capacity in table:
        return table[capacity]
    points = sorted(table)
    if not points:
        return 0.0
    if len(points) == 1:
        return table[points[0]]
    if capacity < points[0]:
        lo, hi = points[0], points[1]
    elif capacity > points[-1]:
        lo, hi = points[-2], points[-1]
    else:
        hi = next(p for p in points if p > capacity)
        lo = points[points.index(hi) - 1]
    span = hi - lo
    return table[lo] + (capacity - lo) / span * (table[hi] - table[lo])


def _spec_adjust_gbp(listing: Listing, mdl: dict, cfg: Optional[dict]) -> float:
    """Benchmark shift for a listing specced above/below the model's
    base_spec - a 48GB/2TB machine should not be scored against base money.
    Additive GBP steps from config (value.spec_adjustments), capped so a
    parse mishap can't distort the benchmark by more than -15%/+60%."""
    if not cfg:
        return 0.0
    sa = cfg.get("value", {}).get("spec_adjustments", {})
    if not sa:
        return 0.0
    m = _BASE_SPEC_RE.search(str(mdl.get("base_spec", "")))
    if not m:
        return 0.0
    base_ram = int(m.group(1))
    base_ssd = int(m.group(2)) * (1024 if m.group(3) == "TB" else 1)
    adj = 0.0
    per8 = float(sa.get("ram_per_8gb_gbp", 60))
    if listing.ram_gb and listing.ram_gb > base_ram:
        adj += (listing.ram_gb - base_ram) / 8.0 * per8
    ssd_map = {int(k): float(v) for k, v in (sa.get("ssd_gbp") or {}).items()}
    if listing.storage_gb and ssd_map:
        adj += (_capacity_value(listing.storage_gb, ssd_map)
                - _capacity_value(base_ssd, ssd_map))
    new = float(mdl["uk_avg_gbp"])
    return round(min(max(adj, -0.15 * new), 0.60 * new), 2)


def match_model(listing: Listing, models: list[dict],
                cfg: Optional[dict] = None) -> None:
    """Attach the matching config model (family + chip + size) to the listing,
    with the benchmark adjusted for the listing's RAM/SSD spec when cfg given."""
    candidates = []
    for mdl in models:
        if mdl.get("family", "macbook") != (listing.family or "macbook"):
            continue
        if str(mdl.get("chip", "")).upper() != listing.chip:
            continue
        if ("size" in mdl and listing.size is not None
                and float(mdl["size"]) != float(listing.size)):
            continue
        candidates.append(mdl)
    if listing.size is None:
        # An unknown size must never inherit whichever model happened to be
        # first in config.  Use the lowest compatible benchmark so uncertainty
        # cannot manufacture a bargain.
        candidates.sort(key=lambda m: float(m.get("uk_avg_gbp", 0)))
    for mdl in candidates:
        listing.model_id = mdl["id"]
        fam_name = FAMILY_NAME.get(listing.family, "")
        size_str = ""
        if listing.size is None and "size" in mdl:
            size_str = " (size unconfirmed)"
        elif "size" in mdl and listing.family == "macbook":
            size_str = f' {mdl["size"]}"'
        listing.model_label = (mdl.get("label")
                               or f"{fam_name} {listing.chip}{size_str}".strip())
        adj = _spec_adjust_gbp(listing, mdl, cfg)
        listing.spec_adj_gbp = adj
        new = float(mdl["uk_avg_gbp"])
        used = float(mdl.get("uk_used_gbp", 0) or 0)
        listing.uk_avg_gbp = round(new + adj, 2)
        # scale the used benchmark proportionally with the spec shift
        listing.uk_used_gbp = (round(used * listing.uk_avg_gbp / new, 2)
                               if used and new else used)
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


# Classifieds sites are full of "wanted"/"we buy" ads -
# they look like listings but there's nothing to buy
CLASSIFIED_AD_RE = re.compile(
    r"\bwanted\b|\bwtb\b|we\s*buy|i\s*buy|buying\b|sell\s*your|cash\s*for|"
    r"\blooking\s*for\b|trade\s*in|recycl", re.I)


def is_wanted_ad(title: str) -> bool:
    return bool(CLASSIFIED_AD_RE.search(title))


# ----------------------------------------------------------------------------
# FX
# ----------------------------------------------------------------------------

def get_fx(fx_cfg: dict) -> tuple[dict, str]:
    """Live JPY/USD/EUR per GBP in one request (frankfurter, ECB data)."""
    fallback = {"JPY": float(fx_cfg.get("fallback_jpy_per_gbp", 216)),
                "USD": float(fx_cfg.get("fallback_usd_per_gbp", 1.33)),
                "EUR": float(fx_cfg.get("fallback_eur_per_gbp", 1.17)),
                "GBP": 1.0}
    try:
        r = requests.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": "GBP", "symbols": "JPY,USD,EUR"},
            timeout=10,
        )
        r.raise_for_status()
        rates = r.json()["rates"]
        return {"JPY": float(rates["JPY"]), "USD": float(rates["USD"]),
                "EUR": float(rates["EUR"]), "GBP": 1.0}, "live"
    except Exception:
        return fallback, "fallback (edit fx.fallback_* in config.yaml)"


# ----------------------------------------------------------------------------
# Landed cost + scoring
# ----------------------------------------------------------------------------

# Default international shipping by family. A compact desktop posts for a
# fraction of what a 27" glass display costs to courier safely.
_INTL_JPY_DEFAULT = {"macbook": 8000, "mac_mini": 6000, "mac_studio": 12000,
                     "imac": 20000, "mac_pro": 30000, "display": 25000}
_INTL_USD_DEFAULT = {"macbook": 85, "mac_mini": 60, "mac_studio": 110,
                     "imac": 180, "mac_pro": 250, "display": 200}
_EU_EUR_DEFAULT = {"macbook": 30, "mac_mini": 25, "mac_studio": 45,
                   "imac": 70, "mac_pro": 90, "display": 80}


def _family_cost(cfg_map: Optional[dict], defaults: dict, family: str,
                 fallback: float) -> float:
    if cfg_map and family in cfg_map:
        return float(cfg_map[family])
    return float(defaults.get(family, fallback))


def landed_cost_gbp(price: float, source: str, cfg: dict, rates: dict,
                    family: str = "macbook") -> float:
    """Full estimated cost delivered to a UK doorstep, in GBP.

    Japan route:  item + proxy fee + JP domestic + international shipping (JPY)
    US route:     item (+ sales tax) + forwarder fee + US domestic
                  + international shipping (USD)
    EU route:     item + shipping to the UK (EUR)
    All three:    x 1.20 UK import VAT (0% duty on computers/tablets)
                  + courier handling.  UK listings just add postage.
    Shipping scales with the product: a Mac mini posts cheaply, a 27" display
    does not (override per family under costs: in config.yaml).
    """
    c = cfg["costs"]
    fam = family or "macbook"
    if source in UK_SOURCES:
        return round(price + float(c.get("uk_domestic_shipping_gbp", 8)), 2)
    if source in US_SOURCES:
        tax = 1 + float(c.get("us_sales_tax_pct", 0)) / 100.0
        intl = _family_cost(c.get("us_intl_shipping_usd_family"),
                            _INTL_USD_DEFAULT, fam,
                            c.get("us_intl_shipping_usd", 85))
        total_usd = (price * tax
                     + c.get("us_forwarder_fee_usd", 12)
                     + c.get("us_domestic_shipping_usd", 10)
                     + intl)
        gbp = total_usd / rates["USD"]
    elif source in EU_SOURCES:
        ship = _family_cost(c.get("eu_shipping_eur_family"),
                            _EU_EUR_DEFAULT, fam,
                            c.get("eu_shipping_eur", 30))
        gbp = (price + ship) / rates["EUR"]
    else:
        dom = c["domestic_shipping_jpy"].get(source, 0)
        intl = _family_cost(c.get("intl_shipping_jpy_family"),
                            _INTL_JPY_DEFAULT, fam,
                            c.get("intl_shipping_jpy", 8000))
        total_jpy = price + c["proxy_fee_jpy"] + dom + intl
        gbp = total_jpy / rates["JPY"]
    if c.get("apply_uk_import", True):
        gbp = gbp * (1 + c["uk_vat_pct"] / 100.0) + c["courier_handling_gbp"]
    return round(gbp, 2)


def _valid_used_benchmark(new: float, used: float) -> bool:
    """Reject obviously spec-mixed/contaminated sold medians.

    Several legacy values were higher than the corresponding new benchmark.
    Silently clamping them hid the data problem; treating them as sparse data
    and using the documented fallback is both safer and more transparent.
    """
    return bool(new and used and new * 0.50 <= used <= new * 0.95)


def expected_price_details(listing: Listing, cfg: dict) -> tuple[float, str, float]:
    """Return (expected UK price, human-readable basis, confidence 0..1).

    The configured benchmark is already spec-adjusted by ``match_model``.
    This function then prices the exact condition, keyboard/layout and
    unusually worn battery.  It is the single benchmark used by the UI,
    alerts, CSV and WhatsApp.
    """
    v = cfg.get("value", {})
    ep = cfg.get("expected_price", {})
    new = float(listing.uk_avg_gbp or 0)
    used = float(listing.uk_used_gbp or 0)
    if not new:
        return 0.0, "no benchmark", 0.0
    valid_used = _valid_used_benchmark(new, used)
    blob = normalise(" ".join((listing.title, listing.condition,
                               listing.description)))
    conf = float(ep.get("configured_benchmark_confidence", 0.72))

    if listing.grade == "resale":
        # A result card that combines New/Open box is not proof of sealed
        # stock.  Price it conservatively until the listing itself says new.
        open_box = bool(re.search(
            r"OPEN\s*BOX|NEW\s*OTHER|OPENED|開封|未使用に近い", blob))
        ambiguous = "NEW / OPEN BOX" in blob
        factor = float(ep.get("open_box_factor", 0.94)) if open_box else 1.0
        if ambiguous and not open_box:
            factor = float(ep.get("ambiguous_new_factor", 0.97))
            conf -= 0.05
        price = new * factor
        basis = "open-box estimate" if factor < 1 else "new/unused benchmark"
    elif listing.grade == "personal":
        if valid_used:
            weight = float(ep.get("like_new_new_weight", 0.50))
            price = new * weight + used * (1 - weight)
            basis = "like-new estimate from new + used UK benchmarks"
            conf += 0.06
        else:
            price = new * float(v.get("like_new_factor", 0.88))
            basis = "like-new estimate (limited sold data)"
            conf -= 0.12
    else:
        if valid_used:
            price = used
            basis = "used UK sold benchmark"
            conf += 0.08
        else:
            price = new * float(v.get("good_factor", 0.78))
            basis = "used estimate (limited sold data)"
            conf -= 0.15

    # The UK resale value must reflect what is actually in the box.  These
    # explicit deductions replace the old arbitrary regional alert bars.
    if listing.family == "macbook":
        penalties = ep.get("keyboard_penalty_gbp", {})
        if listing.keyboard == "JIS":
            price -= float(penalties.get("JIS", 120))
            basis += "; JIS keyboard adjusted"
        elif listing.keyboard == "EU":
            price -= float(penalties.get("EU", 80))
            basis += "; EU keyboard adjusted"
        elif listing.keyboard == "unknown" and listing.source in JP_SOURCES:
            price -= float(penalties.get("unknown_jp", 100))
            basis += "; likely-JIS keyboard adjusted"
            conf -= 0.06
    elif listing.family == "imac":
        if listing.source in JP_SOURCES | EU_SOURCES:
            price -= float(ep.get("imac_keyboard_penalty_gbp", 80))
            basis += "; non-UK keyboard adjusted"

    # Common display variants whose value is stated clearly enough to model
    # without pretending the base benchmark already includes them.
    if listing.family == "display":
        if "NANO" in blob or "NANO-TEXTURE" in blob:
            price += float(ep.get("studio_display_nano_premium_gbp", 150))
            basis += "; nano-texture adjusted"
        if re.search(r"HEIGHT.?ADJUST|高さ調整", blob):
            price += float(ep.get("studio_display_height_premium_gbp", 150))
            basis += "; height stand adjusted"

    # Sold prices for each condition already contain typical battery wear.
    # Only charge for cycles *above* that condition's baseline to avoid
    # double-counting normal wear.
    if listing.family == "macbook" and listing.cycles is not None:
        baseline = {"resale": 10, "personal": 60, "good": 300}.get(
            listing.grade, 300)
        extra_cycles = max(0, listing.cycles - baseline)
        if extra_cycles:
            rating = float(v.get("battery_cycle_rating", 1000))
            replacement = float(v.get("battery_replacement_gbp", 249))
            wear = min(extra_cycles, rating) / rating * replacement
            price -= wear
            basis += f"; excess battery wear -£{wear:.0f}"

    if listing.size_guessed:
        conf -= 0.14
    if listing.spec_adj_gbp:
        conf -= 0.04
    if listing.family != "display":
        if listing.ram_gb is None:
            conf -= 0.04
        if listing.storage_gb is None:
            conf -= 0.06
    return round(max(price, new * 0.35), 2), basis, round(
        min(max(conf, 0.30), 0.92), 2)


def fair_value_gbp(listing: Listing, cfg: dict) -> float:
    """Backward-compatible name for the canonical expected UK price."""
    return expected_price_details(listing, cfg)[0]


def battery_wear_gbp(cycles: Optional[int], cfg: dict) -> float:
    """Battery life already consumed, priced pro-rata: each cycle uses
    1/1000th of the battery's rated life; 600 cycles on a £249 battery =
    £149 of value gone. Negligible (<£15) below 60 cycles."""
    if not cycles:
        return 0.0
    v = cfg.get("value", {})
    rating = float(v.get("battery_cycle_rating", 1000))
    cost = float(v.get("battery_replacement_gbp", 249))
    return round(min(cycles, rating) / rating * cost, 2)


def value_score(listing: Listing, cfg: dict) -> None:
    """Populate legacy value fields from the one canonical deal metric."""
    listing.fair_gbp = listing.expected_price_gbp or fair_value_gbp(listing, cfg)
    listing.value_landed_gbp = listing.landed_gbp
    listing.value_pct = listing.savings_pct


def max_cycles_for(grade: str, cfg: dict) -> int:
    """Battery-cycle ceiling per tier (MacBooks only - desktops have no
    battery)."""
    if grade == "personal":
        return int(cfg.get("personal", {}).get("max_battery_cycles", 60))
    if grade == "good":
        return int(cfg.get("value", {}).get("max_battery_cycles", 800))
    return int(cfg["alerts"]["max_battery_cycles"])


def flip_profit_gbp(listing: Listing, cfg: dict) -> tuple[float, float]:
    """(profit, sell-at price) reselling this unit on the UK market:
    new/unused stock sells at the UK average for new; like-new stock at the
    like-new fair value. Friction covers postage/packaging/pricing-to-sell."""
    friction = float(cfg.get("resale", {}).get("sell_friction_pct", 5))
    if not listing.uk_avg_gbp or not listing.landed_gbp:
        return 0.0, 0.0
    target = listing.expected_price_gbp or fair_value_gbp(listing, cfg)
    return (round(target * (1 - friction / 100.0) - listing.landed_gbp, 2),
            round(target, 2))


_SOURCE_CONFIDENCE = {
    "ebay_uk": 0.92, "ebay_us": 0.86, "ebay_de": 0.84,
    "swappa": 0.90, "mercari": 0.84, "rakuma": 0.80,
    "paypay": 0.80, "yahoo": 0.70, "gumtree": 0.60,
}


def listing_confidence(listing: Listing) -> float:
    """How trustworthy/buyable this particular result appears (0..1)."""
    conf = _SOURCE_CONFIDENCE.get(listing.source, 0.65)
    if listing.is_auction:
        return 0.05
    if listing.size_guessed:
        conf *= 0.78
    if listing.family not in ("display",) and listing.storage_gb is None:
        conf *= 0.90
    if listing.source == "gumtree" and not listing.description:
        conf *= 0.80
    joined = " ".join(listing.flags).upper()
    if "TOO-GOOD" in joined or "SUSPICIOUSLY LOW" in joined:
        conf *= 0.55
    if "NOT A WHOLE PRODUCT" in joined:
        conf *= 0.10
    if "BATTERY CYCLES" in joined and "> YOUR MAX" in joined:
        conf *= 0.45
    return round(min(max(conf, 0.05), 0.98), 2)


def score(listing: Listing, cfg: dict, rates: dict) -> None:
    listing.landed_gbp = landed_cost_gbp(listing.price, listing.source, cfg,
                                         rates, listing.family)
    (listing.expected_price_gbp, listing.expected_price_basis,
     listing.benchmark_confidence) = expected_price_details(listing, cfg)
    if listing.expected_price_gbp:
        listing.savings_gbp = round(
            listing.expected_price_gbp - listing.landed_gbp, 2)
        listing.savings_pct = round(
            listing.savings_gbp / listing.expected_price_gbp * 100, 1
        )
    listing.fair_gbp = listing.expected_price_gbp
    listing.value_landed_gbp = listing.landed_gbp
    listing.value_pct = listing.savings_pct
    listing.flip_profit_gbp, listing.flip_target_gbp = flip_profit_gbp(listing, cfg)
    a = cfg["alerts"]
    # Price-sanity backstop: a whole unit never sells for a small fraction
    # of its UK value - that's usually a part, accessory or scam the title
    # checks did not catch.
    floor_ratio = float(a.get("implausible_price_ratio", 0.30))
    hard_floor = float(a.get("hard_exclude_price_ratio", 0.18))
    rate = rates.get(listing.currency)
    if listing.expected_price_gbp and rate:
        bare_gbp = listing.price / rate   # bare item price, no fees
        ratio = bare_gbp / listing.expected_price_gbp
        if ratio < hard_floor:
            flag = "NOT A WHOLE PRODUCT? price is implausible for the device"
            if flag not in listing.flags:
                listing.flags.append(flag)
        elif ratio < floor_ratio:
            flag = "SUSPICIOUSLY LOW - verify the exact product, lock status and seller"
            if flag not in listing.flags:
                listing.flags.append(flag)
    if listing.spec_adj_gbp:
        flag = f"benchmark spec-adjusted {listing.spec_adj_gbp:+,.0f} GBP"
        if flag not in listing.flags:
            listing.flags.append(flag)
    t = alert_thresholds(cfg, listing.source, listing.family)
    if listing.savings_pct >= t["too_good"]:
        flag = "TOO-GOOD? verify carefully (box-only/scam/mislabel risk)"
        if flag not in listing.flags:
            listing.flags.append(flag)
    if listing.is_auction:
        flag = "auction - current bid, price can rise"
        if flag not in listing.flags:
            listing.flags.append(flag)
    if listing.best_offer:
        flag = "accepts Best Offer - real price may be lower"
        if flag not in listing.flags:
            listing.flags.append(flag)
    if listing.size_guessed:
        flag = "size not stated - conservative lower benchmark used"
        if flag not in listing.flags:
            listing.flags.append(flag)
    # keyboard flags only where a keyboard is actually in the box
    if listing.family == "macbook":
        if listing.keyboard == "JIS":
            flag = "JIS (Japanese) keyboard - expected price adjusted"
            if flag not in listing.flags:
                listing.flags.append(flag)
        elif listing.keyboard == "EU":
            flag = "non-UK European keyboard - expected price adjusted"
            if flag not in listing.flags:
                listing.flags.append(flag)
        elif listing.keyboard == "unknown" and listing.source in JP_SOURCES:
            flag = "keyboard unknown (likely JIS) - expected price adjusted"
            if flag not in listing.flags:
                listing.flags.append(flag)
        max_cyc = max_cycles_for(listing.grade, cfg)
        if listing.cycles is not None and listing.cycles > max_cyc:
            flag = f"battery cycles {listing.cycles} > your max {max_cyc}"
            if flag not in listing.flags:
                listing.flags.append(flag)
    elif listing.family == "imac":
        if listing.source in JP_SOURCES:
            flag = "bundled JIS keyboard/mouse - expected price adjusted"
            if flag not in listing.flags:
                listing.flags.append(flag)
        elif listing.source in EU_SOURCES:
            flag = "bundled EU-layout keyboard - expected price adjusted"
            if flag not in listing.flags:
                listing.flags.append(flag)
    if listing.source == "gumtree":
        flag = "classifieds - often collection-only, no buyer protection"
        if flag not in listing.flags:
            listing.flags.append(flag)
    listing.listing_confidence = listing_confidence(listing)
    saving_component = min(max(listing.savings_pct, 0.0) / 50.0, 1.0)
    listing.overall_score = round(
        saving_component * listing.benchmark_confidence
        * listing.listing_confidence * 100, 1)


CYCLE_RE = re.compile(
    r"(?:充放電回数|充放電|サイクル数|サイクルカウント|サイクル|CYCLE\s*COUNTS?|CYCLES?)\D{0,8}?(\d{1,4})\s*回?",
    re.IGNORECASE,
)
# English word order with the number first: "only 3 cycles", "12 battery cycles"
CYCLE_EN_RE = re.compile(r"\b(\d{1,4})\s*(?:BATTERY\s+)?(?:CHARGE\s+)?CYCLES?\b")

def find_cycle_count(text: str) -> Optional[int]:
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text).upper()
    for rx in (CYCLE_RE, CYCLE_EN_RE):
        m = rx.search(t)
        if m:
            try:
                n = int(m.group(1))
                if 0 <= n <= 3000:
                    return n
            except ValueError:
                pass
    return None
