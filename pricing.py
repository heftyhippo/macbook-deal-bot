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
  ipad_pro    iPad Pro (M2 / M4 / M5)
  ipad_air    iPad Air (M2 / M3 / M4 - only the models averaging over ~£500)
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
                   "craigslist": "USD", "ebay_uk": "GBP", "gumtree": "GBP",
                   "ebay_de": "EUR"}
JP_SOURCES = {"mercari", "yahoo", "rakuma", "paypay"}
US_SOURCES = {"ebay_us", "swappa", "craigslist"}
UK_SOURCES = {"ebay_uk", "gumtree"}
EU_SOURCES = {"ebay_de"}

CURRENCY_SYMBOL = {"JPY": "¥", "USD": "$", "GBP": "£", "EUR": "€"}

# alert thresholds are per REGION (JP prices genuinely run lower, so its bar
# is higher) and split by whether the product carries a keyboard - a JIS
# keyboard hurts UK resale on a MacBook/iMac, but a Mac mini or iPad from
# Japan is the same product you'd buy here (see alerts.* in config.yaml)
REGION_OF_SOURCE = {"mercari": "jp", "yahoo": "jp", "rakuma": "jp",
                    "paypay": "jp", "ebay_us": "us", "swappa": "us",
                    "craigslist": "us", "ebay_uk": "uk", "gumtree": "uk",
                    "ebay_de": "eu"}

# families whose box includes a keyboard (iMac ships with a Magic Keyboard)
KEYBOARD_FAMILIES = {"macbook", "imac"}

FAMILY_NAME = {"macbook": "MacBook Pro", "mac_mini": "Mac mini",
               "mac_studio": "Mac Studio", "imac": "iMac 24\"",
               "mac_pro": "Mac Pro", "display": "Studio Display",
               "ipad_pro": "iPad Pro", "ipad_air": "iPad Air"}

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
    reg = region_of(source)
    kb = family in KEYBOARD_FAMILIES
    base = dict(_THRESHOLD_DEFAULTS[kb][reg])
    # legacy flat keys (pre-regional configs) apply to every region
    for old, new in (("min_savings_pct", "min"), ("hot_savings_pct", "hot"),
                     ("too_good_pct", "too_good")):
        if old in a:
            base[new] = float(a[old])
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
                 "craigslist": "Craigslist", "gumtree": "Gumtree",
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

# 256/512 are deliberately NOT here even though huge Mac Studio/Pro RAM
# configs exist - a bare "512GB" in a title is storage 99% of the time
RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192}
MAC_STORAGE_SIZES = {256, 512}
IPAD_STORAGE_SIZES = {128, 256, 512}

def normalise(text: str) -> str:
    """Full-width -> half-width, uppercase, katakana product words -> latin."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.upper()
    t = (t.replace("プロ", " PRO ").replace("マックス", " MAX ")
          .replace("ウルトラ", " ULTRA ").replace("スタジオ", " STUDIO ")
          .replace("ミニ", " MINI ").replace("アイパッド", " IPAD ")
          .replace("エアー", " AIR ").replace("エア", " AIR ")
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
    "ipad_pro":   {"M2", "M4", "M5"},
    "ipad_air":   {"M2", "M3", "M4"},
}

CHIP_RE = re.compile(r"\bM([1-5])\s*[-/]?\s*(PRO|MAX|ULTRA)?\b")


# a title that STARTS with an accessory name is selling the accessory, not
# the device ("Apple Magic Keyboard 13 Zoll iPad Air (M2)...", "Hülle für
# iPad Pro", "Smart Folio..."). Titles that merely mention one ("iPad Pro M4
# + Magic Keyboard") are bundles and stay in.
_ACCESSORY_LEAD_RE = re.compile(
    r"^\W*(?:APPLE\s+)?(?:MAGIC\s+)?(?:KEYBOARD|TASTATUR|CLAVIER|PENCIL|"
    r"SMART\s+FOLIO|FOLIO|CASE|COVER|HÜLLE|SLEEVE|SKIN|STAND|DOCK|MOUSE|"
    r"TRACKPAD|CHARGER|NETZTEIL|LADEGERÄT)\b")

# iPad/display listings that are really an ACCESSORY named after the device
# ("11インチiPad Pro（M5）用Magic Keyboard", "Logitech Combo Touch iPad Air
# 13 Keyboard Case", "Bosstab floor stand iPad Pro 13") - an accessory noun
# with NO bundle marker means the accessory IS the item. Bundles ("iPad Pro
# + Magic Keyboard", "128GB／Apple Pencil付", "ケース付き") stay in.
_ACC_NOUN_IPAD_RE = re.compile(
    r"MAGIC\s*KEYBOARD|SMART\s*KEYBOARD|KEYBOARD\s*(?:CASE|COVER|FOLIO)|"
    r"COMBO\s*TOUCH|APPLE\s*PENCIL|SMART\s*(?:FOLIO|COVER)|SCREEN\s*PROTECT|"
    r"\bSTAND\b|TRACKPAD|キーボード|ペンシル|ケース|カバー|フィルム|"
    r"スタンド|フォリオ|充電器")
_ACC_NOUN_DISPLAY_RE = re.compile(
    r"FLOOR\s*STAND|DESK\s*STAND|WALL\s*MOUNT|VESA|MOUNT\s*ADAPTER|"
    r"スタンドのみ|マウント")
_BUNDLE_MARK_RE = re.compile(
    r"セット|付き|付属|付|同梱|おまけ|本体|INCLUDED|INCLUDES|\bWITH\b|"
    r"[＋+＆&]")


def _is_accessory_only(t: str, fam: str) -> bool:
    """True when an iPad/display-family title is selling an accessory rather
    than the device (accessory noun present, no bundle marker)."""
    if fam in ("ipad_pro", "ipad_air"):
        rx = _ACC_NOUN_IPAD_RE
    elif fam == "display":
        rx = _ACC_NOUN_DISPLAY_RE
    else:
        return False    # MacBook/iMac titles legitimately mention keyboards
    return bool(rx.search(t)) and not _BUNDLE_MARK_RE.search(t)


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
    if "IPAD PRO" in t:
        return "ipad_pro"
    if "IPAD AIR" in t:
        return "ipad_air"
    return ""


# JP sellers state iPad generations instead of chips: map the 2022 M2 gens
_IPAD_GEN_M2 = re.compile(r"第\s*([46])\s*世代")


def parse_listing_specs(listing: Listing) -> None:
    """Fill family / chip / size / ram / storage / keyboard in place.
    A listing that doesn't parse to a tracked family keeps family='' and is
    dropped by the caller."""
    t = normalise(listing.title)

    fam = _detect_family(t)
    if not fam:
        return
    if _is_accessory_only(t, fam):
        return    # a keyboard/case/pencil/stand named after the device

    # --- chip --------------------------------------------------------------
    chip = ""
    m = CHIP_RE.search(t)
    if m:
        chip = f"M{m.group(1)} {m.group(2) or ''}".strip()
    if fam == "ipad_pro" and not m:
        g = _IPAD_GEN_M2.search(listing.title)
        if g:                       # 11" 第4世代 / 12.9" 第6世代 = M2 (2022)
            chip = "M2"
    if fam == "display":
        chip = ""                   # no chip - and none required
    elif chip not in FAMILY_CHIPS[fam]:
        return                      # pre-2022, MacBook Air-class, or unknown
    listing.family = fam
    listing.chip = chip

    # --- RAM / storage (strip them out before looking for sizes) ------------
    storage_gb = None
    tb = re.search(r"\b([1248])\s*TB\b", t)
    if tb:
        storage_gb = int(tb.group(1)) * 1024
    gb_values = [int(x) for x in re.findall(r"\b(\d{2,4})\s*GB\b", t)]
    ram = None
    stor_set = IPAD_STORAGE_SIZES if fam.startswith("ipad") else MAC_STORAGE_SIZES
    for v in gb_values:
        if fam.startswith("ipad") or fam == "display":
            # iPad titles never state RAM - every GB figure is storage
            if v in IPAD_STORAGE_SIZES and storage_gb is None:
                storage_gb = v
        elif v in RAM_SIZES and ram is None:
            ram = v
        elif v in stor_set and storage_gb is None:
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
            listing.size = 14      # conservative default: the cheaper size
            listing.size_guessed = True
    elif fam == "imac":
        listing.size = 24          # every Apple-silicon iMac is 24"
    elif fam in ("ipad_pro", "ipad_air"):
        if re.search(r"(?<!\d)12[.,]9(?!\d)", t_nosizes):
            listing.size = 12.9
        else:
            s = re.search(r"(?<!\d)(11|13)(?!\d)", t_nosizes)
            if s:
                listing.size = float(s.group(1))
            else:
                listing.size = 11.0    # conservative default: the smaller iPad
                listing.size_guessed = True
        # generation naming: the M2 big iPad Pro is "12.9", the M4/M5 one "13"
        if fam == "ipad_pro":
            if listing.chip == "M2" and listing.size == 13:
                listing.size = 12.9
            elif listing.chip in ("M4", "M5") and listing.size == 12.9:
                listing.size = 13.0
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

    # cellular iPads are worth a little more - surface it
    if fam.startswith("ipad") and re.search(r"CELLULAR|セルラー|WI-?FI\s*\+", t):
        listing.flags.append("cellular model (worth a little more)")

    # sellers often put the battery cycle count right in the title
    if listing.cycles is None and fam == "macbook":
        listing.cycles = find_cycle_count(listing.title)


_BASE_SPEC_RE = re.compile(r"(\d+)\s*GB.*?/\s*(\d+)\s*(GB|TB)")


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
        adj += (ssd_map.get(listing.storage_gb, 0.0)
                - ssd_map.get(base_ssd, 0.0))
    new = float(mdl["uk_avg_gbp"])
    return round(min(max(adj, -0.15 * new), 0.60 * new), 2)


def match_model(listing: Listing, models: list[dict],
                cfg: Optional[dict] = None) -> None:
    """Attach the matching config model (family + chip + size) to the listing,
    with the benchmark adjusted for the listing's RAM/SSD spec when cfg given."""
    for mdl in models:
        if mdl.get("family", "macbook") != (listing.family or "macbook"):
            continue
        if str(mdl.get("chip", "")).upper() != listing.chip:
            continue
        if "size" in mdl and (listing.size is None
                              or float(mdl["size"]) != float(listing.size)):
            continue
        listing.model_id = mdl["id"]
        fam_name = FAMILY_NAME.get(listing.family, "")
        size_str = ""
        if "size" in mdl and listing.family in ("macbook", "ipad_pro", "ipad_air"):
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


# classifieds sites (gumtree/craigslist) are full of "wanted"/"we buy" ads -
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

# default international shipping by family - a boxed iPad posts for a
# fraction of what a 27" glass display costs to courier safely
_INTL_JPY_DEFAULT = {"macbook": 8000, "mac_mini": 6000, "mac_studio": 12000,
                     "imac": 20000, "mac_pro": 30000, "display": 25000,
                     "ipad_pro": 4500, "ipad_air": 4500}
_INTL_USD_DEFAULT = {"macbook": 85, "mac_mini": 60, "mac_studio": 110,
                     "imac": 180, "mac_pro": 250, "display": 200,
                     "ipad_pro": 45, "ipad_air": 45}
_EU_EUR_DEFAULT = {"macbook": 30, "mac_mini": 25, "mac_studio": 45,
                   "imac": 70, "mac_pro": 90, "display": 80,
                   "ipad_pro": 20, "ipad_air": 20}


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
    Shipping scales with the product: an iPad posts cheap, a 27" display
    doesn't (override per family under costs: in config.yaml).
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


def fair_value_gbp(listing: Listing, cfg: dict) -> float:
    """What a UK buyer typically pays for THIS model in THIS condition.

    new       -> the model's UK average for new/unused (uk_avg_gbp)
    like_new  -> midpoint of new and the used median (or x0.88 fallback)
    good      -> the eBay-UK SOLD median for used units (or x0.78 fallback)
    """
    v = cfg.get("value", {})
    new = float(listing.uk_avg_gbp)
    used = float(listing.uk_used_gbp or 0)
    if used and new:
        used = min(max(used, new * 0.55), new * 0.92)
    bucket = {"resale": "new", "personal": "like_new"}.get(listing.grade, "good")
    if bucket == "new":
        return new
    if bucket == "like_new":
        if used:
            return round((new + used) / 2, 2)
        return round(new * float(v.get("like_new_factor", 0.88)), 2)
    if used:
        return used
    return round(new * float(v.get("good_factor", 0.78)), 2)


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
    """Deal quality for the price RELATIVE TO CONDITION: % below the fair
    UK value for this model in this condition, after adding the pro-rated
    battery wear cost to the landed price."""
    listing.fair_gbp = fair_value_gbp(listing, cfg)
    wear = battery_wear_gbp(listing.cycles, cfg)
    listing.value_landed_gbp = round(listing.landed_gbp + wear, 2)
    if wear >= 25:
        flag = f"battery wear costed in (+£{wear:.0f})"
        if flag not in listing.flags:
            listing.flags.append(flag)
    if listing.fair_gbp:
        listing.value_pct = round(
            (listing.fair_gbp - listing.value_landed_gbp)
            / listing.fair_gbp * 100, 1)


def max_cycles_for(grade: str, cfg: dict) -> int:
    """Battery-cycle ceiling per tier (MacBooks only - desktops have no
    battery and iPad sellers essentially never state a count)."""
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
    target = (listing.uk_avg_gbp if listing.grade == "resale"
              else fair_value_gbp(listing, cfg))
    return (round(target * (1 - friction / 100.0) - listing.landed_gbp, 2),
            round(target, 2))


def score(listing: Listing, cfg: dict, rates: dict) -> None:
    listing.landed_gbp = landed_cost_gbp(listing.price, listing.source, cfg,
                                         rates, listing.family)
    if listing.uk_avg_gbp:
        listing.savings_pct = round(
            (listing.uk_avg_gbp - listing.landed_gbp) / listing.uk_avg_gbp * 100, 1
        )
    if listing.grade in ("resale", "personal"):
        listing.flip_profit_gbp, listing.flip_target_gbp = \
            flip_profit_gbp(listing, cfg)
    a = cfg["alerts"]
    # Price-sanity backstop: a whole unit never sells for a small fraction
    # of its UK value - that's a part or an accessory (iPad keyboard, display
    # stand, ...) the keyword list didn't catch.
    floor_ratio = a.get("implausible_price_ratio", 0.30)
    rate = rates.get(listing.currency)
    if listing.uk_avg_gbp and rate:
        bare_gbp = listing.price / rate   # bare item price, no fees
        if bare_gbp < listing.uk_avg_gbp * floor_ratio:
            listing.flags.append(
                "PRICE TOO LOW for a whole unit - likely a part/accessory, not the device")
    if listing.spec_adj_gbp:
        listing.flags.append(
            f"benchmark spec-adjusted {listing.spec_adj_gbp:+,.0f} GBP")
    t = alert_thresholds(cfg, listing.source, listing.family)
    if listing.savings_pct >= t["too_good"]:
        listing.flags.append("TOO-GOOD? verify carefully (box-only/scam/mislabel risk)")
    if listing.is_auction:
        listing.flags.append("auction - current bid, price can rise")
    if listing.best_offer:
        listing.flags.append("accepts Best Offer - real price may be lower")
    if listing.size_guessed:
        base = '14"' if listing.family == "macbook" else '11"'
        listing.flags.append(f"size not stated - assumed {base}")
    # keyboard flags only where a keyboard is actually in the box
    if listing.family == "macbook":
        if listing.keyboard == "JIS":
            listing.flags.append("JIS (Japanese) keyboard")
        elif listing.keyboard == "EU":
            listing.flags.append("non-UK European keyboard layout")
        elif listing.keyboard == "unknown" and listing.source in JP_SOURCES:
            listing.flags.append("keyboard layout unknown (likely JIS)")
        max_cyc = max_cycles_for(listing.grade, cfg)
        if listing.cycles is not None and listing.cycles > max_cyc:
            listing.flags.append(f"battery cycles {listing.cycles} > your max {max_cyc}")
    elif listing.family == "imac":
        if listing.source in JP_SOURCES:
            listing.flags.append("bundled JIS keyboard/mouse (UK swap ~£80)")
        elif listing.source in EU_SOURCES:
            listing.flags.append("bundled EU-layout keyboard (UK swap ~£80)")
    if listing.source == "craigslist":
        listing.flags.append("local pickup/cash - needs a US contact, no buyer protection")
    elif listing.source == "gumtree":
        listing.flags.append("classifieds - often collection-only, no buyer protection")


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
