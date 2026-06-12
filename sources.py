"""
sources.py - fetches listings from Mercari JP and Yahoo! Auctions JP
(the two marketplaces that supply the bulk of Buyee's and ZenMarket's
second-hand stock), plus an eBay-UK sold-listings price helper.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import time
import unicodedata
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from pricing import Listing, find_cycle_count

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class FetchError(Exception):
    """Raised when every fetch attempt failed. Keeps the last HTTP status and
    response body so --debug can save whatever the site sent back."""
    def __init__(self, msg: str, status=None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body or ""


_HTTP_BACKEND: Optional[str] = None
_SESSIONS: dict = {}      # impersonation profile -> curl_cffi Session
_WARMED: set = set()      # (profile, warmup_url) pairs already visited

# real-browser TLS/HTTP2 fingerprints to rotate through when a site says 403
IMPERSONATE_PROFILES = ["chrome", "safari", "chrome_android", "safari_ios"]


def http_backend() -> str:
    """'curl_cffi' (stealthy, recommended) or 'requests' (easily blocked)."""
    global _HTTP_BACKEND
    if _HTTP_BACKEND is None:
        try:
            import curl_cffi  # noqa: F401
            _HTTP_BACKEND = "curl_cffi"
        except ImportError:
            _HTTP_BACKEND = "requests"
    return _HTTP_BACKEND


def _http_get(url: str, referer: str = "", warmup: str = "") -> str:
    """GET a page looking like a real browser.

    With curl_cffi installed: rotates through several real-browser
    fingerprints, keeps cookies in a session, and (optionally) visits a
    'warmup' page first like a human would - this is what gets past
    Yahoo's bot-blocking. NOTE: we deliberately do NOT set our own
    User-Agent here; each impersonation profile supplies a matching one,
    and a mismatched UA is an instant bot giveaway.
    """
    base_headers = {
        "Accept-Language": "ja,en-GB;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        base_headers["Referer"] = referer

    if http_backend() == "curl_cffi":
        from curl_cffi import requests as creq
        last_status, last_body, last_err = None, "", None
        for prof in IMPERSONATE_PROFILES:
            try:
                sess = _SESSIONS.get(prof)
                if sess is None:
                    sess = creq.Session(impersonate=prof)
                    _SESSIONS[prof] = sess
                if warmup and (prof, warmup) not in _WARMED:
                    try:
                        sess.get(warmup, headers=base_headers, timeout=20)
                    except Exception:
                        pass  # warmup is best-effort
                    _WARMED.add((prof, warmup))
                    time.sleep(0.8)
                r = sess.get(url, headers=base_headers, timeout=25)
                if r.status_code == 200 and r.text:
                    return r.text
                last_status, last_body = r.status_code, r.text or ""
                # blocked with this fingerprint - try the next one
                time.sleep(1.2)
            except Exception as e:
                last_err = e
                time.sleep(1.2)
        msg = (f"HTTP {last_status}" if last_status else f"{last_err}")
        raise FetchError(
            f"{msg} after trying {len(IMPERSONATE_PROFILES)} browser fingerprints",
            status=last_status, body=last_body)

    # ---- plain-requests fallback (curl_cffi not installed) ----
    import requests
    headers = dict(base_headers)
    headers["User-Agent"] = UA
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} (plain requests - install "
                         f"curl_cffi for stealth mode)",
                         status=r.status_code, body=r.text or "")
    return r.text


# ============================================================================
# MERCARI JP  (official-app API via the `mercapi` library)
# ============================================================================

def _run_async(factory):
    """Run an async coroutine factory to completion in its OWN thread + event
    loop. Plain asyncio.run() breaks once Playwright's sync browser (used for
    Buyee) has claimed the main thread's async machinery."""
    import threading
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(factory())
        except BaseException as e:   # propagate to caller
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


async def _mercari_search_async(queries, conditions, pages, min_price) -> list[Listing]:
    from mercapi import Mercapi
    from mercapi.requests.search import SearchRequestData

    m = Mercapi()
    out: dict[str, Listing] = {}
    for q in queries:
        try:
            res = await m.search(
                q,
                item_conditions=list(conditions),
                status=[SearchRequestData.Status.STATUS_ON_SALE],
                sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
                price_min=int(min_price),
            )
        except Exception as e:
            print(f"  [mercari] search '{q}' failed: {e}")
            continue
        page = 0
        while True:
            for it in res.items:
                if it.real_price is None:
                    continue
                out[it.id_] = Listing(
                    item_id=it.id_,
                    source="mercari",
                    title=it.name,
                    price_jpy=int(it.real_price),
                    is_auction=False,
                    condition={1: "新品、未使用", 2: "未使用に近い"}.get(
                        it.item_condition_id, f"condition {it.item_condition_id}"
                    ),
                )
            page += 1
            if page >= pages:
                break
            try:
                if not res.meta.next_page_token:
                    break
                await asyncio.sleep(1.2)
                res = await res.next_page()
            except Exception:
                break
        await asyncio.sleep(1.2)
    return list(out.values())


def scan_mercari(cfg) -> list[Listing]:
    s = cfg["scan"]
    try:
        return _run_async(lambda: _mercari_search_async(
            s["queries"], s["mercari_conditions"], s["mercari_pages"], s["min_price_jpy"]))
    except Exception as e:
        print(f"  [mercari] scan failed entirely: {e}")
        return []


def fetch_mercari_cycles(item_id: str) -> Optional[int]:
    """Open the full Mercari listing and look for a battery-cycle count."""
    async def _go():
        from mercapi import Mercapi
        m = Mercapi()
        item = await m.item(item_id)
        return find_cycle_count((item.description or "") if item else "")
    try:
        return _run_async(_go)
    except Exception:
        return None


# ============================================================================
# YAHOO! AUCTIONS JP  (HTML search-results scraping)
# ============================================================================

# ---------------------------------------------------------------------------
# Buyee is protected by an AWS WAF *JavaScript challenge* (HTTP 202 + a
# challenge.js page). Plain HTTP clients cannot execute JavaScript, so they
# can never earn the required token. Solution: solve the challenge once in a
# real (invisible) Chromium via Playwright, then hand the earned token cookie
# to the fast curl_cffi layer. If the token handoff isn't accepted, we simply
# fetch every Buyee page through the browser (slower but bulletproof).
# ---------------------------------------------------------------------------

_BUYEE = {"cookie_header": "", "ua": "", "browser_only": False}
_PW = {"pw": None, "browser": None, "ctx": None, "page": None}

_WAF_MARKERS = ("awswaf", "challenge-container", "gokuProps")


def _looks_like_waf(html: str) -> bool:
    head = html[:4000]
    return any(m in head for m in _WAF_MARKERS)


_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")


def _browser_page():
    """Start (once) and return a headless-Chromium page via Playwright,
    masked so it doesn't announce itself as a robot: headless Chromium's
    default UA contains 'HeadlessChrome' and navigator.webdriver=true, and
    Buyee's edge server 403s exactly that."""
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise FetchError(
            "Playwright is not installed, and Buyee's bot-check needs a real "
            "browser. One-time fix, two commands:\n"
            "            python3 -m pip install playwright\n"
            "            python3 -m playwright install chromium")
    _PW["pw"] = sync_playwright().start()
    _PW["browser"] = _PW["pw"].chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _PW["ctx"] = _PW["browser"].new_context(
        user_agent=_DESKTOP_UA,
        locale="ja-JP",
        viewport={"width": 1366, "height": 900},
        extra_http_headers={"Accept-Language": "ja,en-GB;q=0.9,en;q=0.8"},
    )
    _PW["ctx"].add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    _PW["page"] = _PW["ctx"].new_page()
    import atexit
    atexit.register(_browser_close)
    # warm up like a human: land on the homepage first (also earns the WAF
    # token there, before we ask for anything search-shaped)
    try:
        _PW["page"].goto("https://buyee.jp/?lang=en",
                         wait_until="domcontentloaded", timeout=30000)
        _PW["page"].wait_for_timeout(2500)
    except Exception:
        pass
    return _PW["page"]


def _browser_close():
    for key in ("browser", "pw"):
        try:
            if _PW[key]:
                (_PW[key].close() if key == "browser" else _PW[key].stop())
        except Exception:
            pass
        _PW[key] = None
    _PW["ctx"] = _PW["page"] = None


_BLOCK_RE = re.compile(r"<title>\s*(403 Forbidden|Access Denied|Forbidden)", re.I)


def _looks_hard_blocked(html: str) -> bool:
    return len(html) < 2500 and bool(_BLOCK_RE.search(html))


def _browser_fetch(url: str, timeout_ms: int = 35000) -> str:
    """Load a Buyee URL in headless Chromium. The AWS WAF challenge runs its
    JavaScript and reloads the page by itself - we just poll until the
    content stops looking like a challenge."""
    page = _browser_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000
    html = page.content()
    while _looks_like_waf(html) and time.time() < deadline:
        page.wait_for_timeout(1500)   # challenge solves + auto-reloads
        html = page.content()
    if _looks_like_waf(html):
        raise FetchError("Buyee's bot-check did not clear in the browser "
                         "(it may have escalated to a CAPTCHA)", body=html)
    if _looks_hard_blocked(html):
        raise FetchError("Buyee's server refused the browser outright "
                         "(403 page)", status=403, body=html)
    return html


def _adopt_browser_cookies():
    """Copy the browser's earned buyee.jp cookies (incl. the WAF token) into
    a Cookie header the fast HTTP layer can reuse."""
    try:
        cookies = [c for c in _PW["ctx"].cookies()
                   if "buyee.jp" in (c.get("domain") or "")]
        _BUYEE["cookie_header"] = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies)
        _BUYEE["ua"] = _PW["page"].evaluate("navigator.userAgent")
    except Exception:
        pass


def _buyee_http_attempt(url: str) -> tuple:
    """One fast HTTP try. Returns (status_code, text)."""
    headers = {
        "Accept-Language": "ja,en-GB;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://buyee.jp/",
    }
    if _BUYEE["cookie_header"]:
        headers["Cookie"] = _BUYEE["cookie_header"]
    if _BUYEE["ua"]:
        headers["User-Agent"] = _BUYEE["ua"]
    if http_backend() == "curl_cffi":
        from curl_cffi import requests as creq
        sess = _SESSIONS.get("buyee-http")
        if sess is None:
            sess = creq.Session(impersonate="chrome")
            _SESSIONS["buyee-http"] = sess
        r = sess.get(url, headers=headers, timeout=25)
        return r.status_code, r.text or ""
    import requests
    headers.setdefault("User-Agent", UA)
    r = requests.get(url, headers=headers, timeout=25)
    return r.status_code, r.text or ""


def _buyee_get(url: str) -> str:
    """Fetch a Buyee page: fast HTTP when our WAF token is accepted,
    headless browser whenever it isn't."""
    if not _BUYEE["browser_only"]:
        try:
            status, text = _buyee_http_attempt(url)
            if status == 200 and text and not _looks_like_waf(text):
                return text
        except Exception:
            pass
        # token missing/expired/rejected -> earn one in the browser
    html = _browser_fetch(url)
    _adopt_browser_cookies()
    if not _BUYEE["cookie_header"]:
        _BUYEE["browser_only"] = True   # cookies unreadable; stay in browser
    time.sleep(0.6)
    return html


YAHOO_ID_RE = re.compile(r"/auction/([a-z]\d{6,12})", re.I)
PRICE_RE = re.compile(r"([\d,]+)\s*円")

def scan_yahoo(cfg, debug: bool = False) -> list[Listing]:
    """Yahoo! Auctions listings - fetched VIA BUYEE.

    Yahoo! JAPAN has geo-blocked all visitors from the UK/EEA since April
    2022, so auctions.yahoo.co.jp cannot be reached from a UK connection at
    all. Buyee exists precisely to give overseas buyers access to Yahoo
    Auctions, so we search Buyee's mirror of it instead.
    """
    s = cfg["scan"]
    out: dict[str, Listing] = {}
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("  [yahoo/buyee] WARNING: Playwright is not installed. Buyee's "
              "bot-check needs a real browser, so Yahoo results will fail.\n"
              "                One-time fix, two commands:\n"
              "                python3 -m pip install playwright\n"
              "                python3 -m playwright install chromium")
    extra = s.get("buyee_extra_params", "translationType=98&istatus=2")
    # Buyee's own condition filter can't be relied on, so each query is
    # searched twice with the two words Japanese sellers reliably put in
    # the title of an unused item: 未使用 (unused) and 新品 (brand new).
    variants = [f"{q} {w}" for q in s["queries"] for w in ("未使用", "新品")]
    for i, q in enumerate(variants):
        url = f"https://buyee.jp/item/search/query/{quote(q)}?{extra}"
        try:
            html = _buyee_get(url)
        except FetchError as e:
            print(f"  [yahoo/buyee] search '{q}' failed: {e}")
            if debug and e.body:
                with open("debug_buyee_blocked.html", "w", encoding="utf-8") as f:
                    f.write(e.body)
                print("  [yahoo/buyee] saved Buyee's block/error page to "
                      "debug_buyee_blocked.html - send it to Claude for a fix.")
            continue
        except Exception as e:
            print(f"  [yahoo/buyee] search '{q}' failed: {e}")
            continue
        if debug:
            fn = f"debug_buyee_{re.sub(r'[^A-Za-z0-9]+','_',q)}{i}.html"
            with open(fn, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  [yahoo/buyee] saved raw page to {fn}")

        soup = BeautifulSoup(html, "html.parser")
        # Tolerant parsing: any link to a Yahoo-auction item page is a result.
        anchors = soup.select("a[href*='/item/yahoo/auction/'], "
                              "a[href*='/jdirectitems/auction/']")
        found_here = 0
        for a in anchors:
            mid = BUYEE_YAHOO_ID_RE.search(a.get("href", ""))
            if not mid:
                continue
            item_id = mid.group(1)
            if item_id in out:
                continue
            card, text = _climb_to_card(a)
            title = _anchor_title(a, card)
            if not title:
                continue

            # Buyee may ignore the unused filter, so insist the title itself
            # says new/unused (Japanese sellers reliably put this in titles).
            blob = title + " " + text
            if not _UNUSED_HINT_RE.search(blob):
                continue
            if "中古" in blob and "未使用" not in blob:
                continue

            price, is_auction = _pick_price(text)
            if price is None or price < int(s["min_price_jpy"]):
                continue
            out[item_id] = Listing(
                item_id=item_id,
                source="yahoo",
                title=title,
                price_jpy=price,
                is_auction=is_auction,
                condition="未使用",
            )
            found_here += 1
        if not anchors:
            print(f"  [yahoo/buyee] 0 items parsed for '{q}' - Buyee may have "
                  f"changed its page layout. Run with --debug and send the "
                  f"debug_buyee_*.html file to Claude.")
        elif debug:
            print(f"  [yahoo/buyee] '{q}': {len(anchors)} cards on page, "
                  f"{found_here} passed the new/unused title check")
        time.sleep(1.5)
    return list(out.values())


# Buyee has rebranded Yahoo! Auctions as "JDirectItems Auction" for overseas
# users - accept item links under either name (IDs are the same).
BUYEE_YAHOO_ID_RE = re.compile(
    r"/item/(?:yahoo|jdirectitems)/auction/([a-z]?\d{6,13})", re.I)
# Accept BOTH "198,000円" / "198,000 yen" and "¥198,000" / "￥198,000".
BUYEE_PRICE_RE = re.compile(
    r"[¥￥]\s*([0-9][\d,]{2,})|([0-9][\d,]{2,})\s*(?:円|yen|JPY)", re.I)


def _first_price(text: str) -> Optional[int]:
    m = BUYEE_PRICE_RE.search(text)
    if not m:
        return None
    g = m.group(1) or m.group(2)
    return int(g.replace(",", ""))


_UNUSED_HINT_RE = re.compile(
    r"新品|未使用|未開封|デッドストック|unused|brand\s*new|new\s*in\s*box|sealed",
    re.I)


def _climb_to_card(a) -> tuple:
    """Walk up from an item link until the surrounding element contains a yen
    price - that element is the listing card. Layout-agnostic on purpose."""
    card = a
    for _ in range(5):
        if card.parent is None:
            break
        card = card.parent
        text = card.get_text(" ", strip=True)
        if BUYEE_PRICE_RE.search(text):
            return card, text
    return card, card.get_text(" ", strip=True)


def _anchor_title(a, card) -> str:
    title = (a.get("title") or "").strip()
    if not title:
        img = a.select_one("img[alt]") or card.select_one("img[alt]")
        if img:
            title = (img.get("alt") or "").strip()
    if not title:
        title = a.get_text(" ", strip=True)
    # drop obvious non-titles like a bare "Bid"/"Buy" button label
    return title if len(title) >= 8 else ""


def _pick_price(card_text: str) -> tuple:
    """Return (price, is_auction). Prefer the Buy-It-Now figure (即決 /
    'Buy It Now') if the card shows one, since that's a price you can
    actually pay; otherwise the current bid (price may rise)."""
    bin_m = re.search(
        r"(?:即決|Buy\s*It\s*Now)[^0-9¥￥]{0,20}(?:[¥￥]\s*)?([0-9][\d,]{2,})\s*(?:円|yen|JPY)?",
        card_text, re.I)
    if bin_m:
        return int(bin_m.group(1).replace(",", "")), False
    p = _first_price(card_text)
    if p is not None:
        return p, True
    return None, True


def fetch_yahoo_cycles(item_id: str) -> Optional[int]:
    """Battery-cycle lookup for a Yahoo auction - read from its BUYEE item
    page (which shows the seller's full description), because
    auctions.yahoo.co.jp itself is geo-blocked in the UK/EEA."""
    try:
        html = _buyee_get(
            f"https://buyee.jp/item/yahoo/auction/{item_id}?translationType=98")
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return find_cycle_count(text)
    except Exception:
        return None


# ============================================================================
# RAKUTEN RAKUMA  (2nd-biggest JP flea market, via Buyee - same browser path)
# ============================================================================

# Rakuma item links on Buyee look like /item/rakuma/<id> (ids are long
# alphanumerics). Accept a few spellings to be safe against markup churn.
BUYEE_RAKUMA_HREF_RE = re.compile(r"/(?:item/)?rakuma/(?:item/)?([A-Za-z0-9_-]{6,})", re.I)


def scan_rakuma(cfg, debug: bool = False) -> list[Listing]:
    """Rakuten Rakuma listings - fetched VIA BUYEE, exactly like Yahoo.

    Rakuma has no clean public API (unlike Mercari), and Buyee proxies it for
    overseas buyers, so we search Buyee's Rakuma vertical with the same
    headless-browser fetcher that clears Buyee's bot-check.
    """
    s = cfg["scan"]
    out: dict[str, Listing] = {}
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("  [rakuma/buyee] WARNING: Playwright is not installed - Rakuma "
              "needs the browser path. Install it (see the Yahoo note above).")
    extra = s.get("rakuma_extra_params", "status=all")
    variants = [f"{q} {w}" for q in s["queries"] for w in ("未使用", "新品")]
    for i, q in enumerate(variants):
        url = f"https://buyee.jp/rakuma/search?keyword={quote(q)}&{extra}"
        try:
            html = _buyee_get(url)
        except FetchError as e:
            print(f"  [rakuma/buyee] search '{q}' failed: {e}")
            if debug and e.body:
                with open("debug_rakuma_blocked.html", "w", encoding="utf-8") as f:
                    f.write(e.body)
                print("  [rakuma/buyee] saved Buyee's block/error page to "
                      "debug_rakuma_blocked.html - send it to Claude for a fix.")
            continue
        except Exception as e:
            print(f"  [rakuma/buyee] search '{q}' failed: {e}")
            continue
        if debug:
            fn = f"debug_rakuma_{re.sub(r'[^A-Za-z0-9]+','_',q)}{i}.html"
            with open(fn, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  [rakuma/buyee] saved raw page to {fn}")

        soup = BeautifulSoup(html, "html.parser")
        anchors = [a for a in soup.select("a[href*='rakuma']")
                   if BUYEE_RAKUMA_HREF_RE.search(a.get("href", ""))]
        found_here = 0
        for a in anchors:
            href = a.get("href", "")
            m = BUYEE_RAKUMA_HREF_RE.search(href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in out:
                continue
            card, text = _climb_to_card(a)
            title = _anchor_title(a, card)
            if not title:
                continue
            blob = title + " " + text
            if not _UNUSED_HINT_RE.search(blob):
                continue
            if "中古" in blob and "未使用" not in blob:
                continue
            # Rakuma is fixed-price (not auctions), so take the card's yen price.
            price, _ = _pick_price(text)
            if price is None or price < int(s["min_price_jpy"]):
                continue
            out[item_id] = Listing(
                item_id=item_id,
                source="rakuma",
                title=title,
                price_jpy=price,
                is_auction=False,
                condition="未使用",
                buyee_path=href,          # exact link we found = always valid
            )
            found_here += 1
        if not anchors:
            print(f"  [rakuma/buyee] 0 items parsed for '{q}' - Buyee's Rakuma "
                  f"layout may have changed. Run with --debug and send the "
                  f"debug_rakuma_*.html file to Claude.")
        elif debug:
            print(f"  [rakuma/buyee] '{q}': {len(anchors)} cards on page, "
                  f"{found_here} passed the new/unused title check")
        time.sleep(1.5)
    return list(out.values())


def fetch_rakuma_cycles(item_id: str) -> Optional[int]:
    """Battery-cycle lookup for a Rakuma item via its Buyee item page."""
    try:
        html = _buyee_get(f"https://buyee.jp/item/rakuma/{item_id}")
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return find_cycle_count(text)
    except Exception:
        return None


# ============================================================================
# EBAY UK  -  median of recent SOLD prices for new / open-box-unused units
# ============================================================================

def ebay_uk_sold_median(query: str, debug: bool = False) -> tuple[Optional[float], int]:
    """Returns (median GBP, sample size) from recent eBay UK sold listings,
    condition New (1000) + Open box (1500), UK located."""
    url = (
        "https://www.ebay.co.uk/sch/i.html?_nkw=" + quote(query)
        + "&LH_Sold=1&LH_Complete=1&LH_ItemCondition=1000%7C1500"
        + "&LH_PrefLoc=1&_ipg=120"
    )
    try:
        html = _http_get(url, referer="https://www.ebay.co.uk/")
    except Exception as e:
        print(f"  [ebay] fetch failed for '{query}': {e}")
        return None, 0
    if debug:
        fn = f"debug_ebay_{re.sub(r'[^A-Za-z0-9]+','_',query)[:40]}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
    soup = BeautifulSoup(html, "html.parser")
    prices: list[float] = []
    nodes = soup.select(".s-item__price") or soup.select("[class*='item__price']")
    texts = [n.get_text(" ", strip=True) for n in nodes]
    if not texts:  # newer eBay layouts: fall back to regexing the whole page
        texts = re.findall(r"£[\d,]+\.\d{2}", html)
    for t in texts:
        t = unicodedata.normalize("NFKC", t)
        if " to " in t.lower():
            continue
        m = re.search(r"£\s*([\d,]+(?:\.\d{2})?)", t)
        if m:
            prices.append(float(m.group(1).replace(",", "")))
    # keep plausible laptop prices, trim outliers with the IQR rule
    prices = [p for p in prices if 300 <= p <= 8000]
    if len(prices) < 5:
        return None, len(prices)
    prices.sort()
    q1 = prices[len(prices) // 4]
    q3 = prices[(len(prices) * 3) // 4]
    iqr = q3 - q1
    kept = [p for p in prices if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]
    if not kept:
        kept = prices
    return round(statistics.median(kept), 0), len(kept)
