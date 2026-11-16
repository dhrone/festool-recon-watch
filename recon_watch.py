#!/usr/bin/env python3
"""Watch festoolrecon.com: alert when the tool on the home page changes.

Recon sells one featured tool at a time on its home page; that is the main
alert (a macOS notification naming the tool, its price and link). When the
featured tool sells out the home page shows a "Wow, that went fast!!!"
placeholder; that is logged, not notified.

In the background it also reads the store's catalogue, public Shopify JSON at
/products.json?limit=250&page=N (about 3 pages, ~500 listings, mostly old and
sold out), and logs (no notification):
  NEW       a product id never seen before (in stock or not)
  RESTOCK   a known product that was sold out, or gone from the list, and is now in stock
Every request sends If-None-Match, so an unchanged page costs a 304 and no body.

State lives in state/recon-state.json; alerts are appended to logs/alerts.log.
The first run only records what is there.

Politeness: one run every 15 min (launchd), a random 0-90 s start delay, a few
seconds between pages, an honest User-Agent, and on 429/403/5xx or a non-JSON
reply (a Cloudflare challenge) it stops and backs off (Retry-After, else
30 min doubling to 4 h) instead of retrying.
"""
import html
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.festoolrecon.com"
UA = "recon-watch/1.0 (personal restock alert; 1 check / 15 min)"
PAGE_SIZE = 250
MAX_PAGES = 10
HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "recon-state.json"
ALERTS = HERE / "logs" / "alerts.log"
SKIP_TYPES = {"Placeholder"}  # the "Wow, that went fast!!!" $0.01 item


def now():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"products": {}, "pages": {}, "backoff_until": None, "backoff_s": 0}


def save_state(st):
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    tmp.replace(STATE)


def notify(title, body):
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
    subprocess.run(["osascript", "-e", script], check=False)


class Blocked(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after = status, retry_after


def get(url, etag, accept):
    """Return (body, etag), or (None, etag) on 304."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        if e.code in (403, 429, 430) or e.code >= 500:
            ra = e.headers.get("Retry-After")
            raise Blocked(e.code, int(ra) if ra and ra.isdigit() else None)
        raise


H1 = re.compile(r'<h1[^>]*product-single__title[^>]*>\s*(.*?)\s*</h1>', re.S)
HANDLE = re.compile(r'/products/([a-z0-9-]+)')


def fetch_home(cached):
    """The featured product: {'title', 'handle', 'etag'}; cached on 304."""
    body, etag = get(BASE + "/", (cached or {}).get("etag"), "text/html")
    if body is None and cached:
        return cached
    text = body.decode("utf-8", "replace")
    m = H1.search(text)
    if not m:
        if "cf-chl" in text or "challenge" in text.lower()[:5000]:
            raise Blocked("challenge page")
        return {"title": None, "handle": None, "etag": etag}
    h = HANDLE.search(text)
    return {"title": html.unescape(m.group(1)).strip(), "handle": h.group(1) if h else None, "etag": etag}


def fetch_page(n, cached):
    url = f"{BASE}/products.json?limit={PAGE_SIZE}&page={n}"
    body, etag = get(url, cached and cached.get("etag"), "application/json")
    if body is None:
        if cached:
            return cached["products"], True
        raise Blocked("304 with no cached page")
    try:
        data = json.loads(body)["products"]
    except (ValueError, KeyError):
        raise Blocked("non-JSON reply (challenge page?)")
    slim = [
        {
            "id": str(p["id"]),
            "title": p["title"].strip(),
            "handle": p["handle"],
            "type": p.get("product_type", ""),
            "price": min(float(v["price"]) for v in p["variants"]) if p["variants"] else None,
            "available": any(v.get("available") for v in p["variants"]),
        }
        for p in data
    ]
    return slim, {"etag": etag, "products": slim}


def back_off(st, where, b):
    wait = b.retry_after or min(max(st.get("backoff_s", 0) * 2, 1800), 4 * 3600)
    first = not st.get("backoff_s")
    st["backoff_s"] = wait
    st["backoff_until"] = (now() + timedelta(seconds=wait)).isoformat()
    save_state(st)
    log(f"blocked on {where}: {b}; backing off {wait // 60} min")
    if first:
        notify("Recon watch paused", f"festoolrecon.com replied {b}; pausing {wait // 60} min.")


def record(lines):
    ALERTS.parent.mkdir(exist_ok=True)
    with ALERTS.open("a") as f:
        for l in lines:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M} {l}\n")
    for l in lines:
        log(l)


def money(p):
    return f"${p['price']:,.2f}" if p and p.get("price") is not None else ""


def run():
    st = load_state()
    if st.get("backoff_until") and now() < datetime.fromisoformat(st["backoff_until"]):
        log(f"backing off until {st['backoff_until']}")
        return

    # 1. The home page: the main alert.
    try:
        home = fetch_home(st.get("home"))
    except Blocked as b:
        return back_off(st, "home page", b)

    # 2. The catalogue, for prices and the background log.
    products, pages = [], {}
    for n in range(1, MAX_PAGES + 1):
        time.sleep(random.uniform(3, 8))
        cached = st["pages"].get(str(n))
        try:
            items, page_state = fetch_page(n, cached)
        except Blocked as b:
            return back_off(st, f"catalogue page {n}", b)
        pages[str(n)] = cached if page_state is True else page_state
        products += items
        if len(items) < PAGE_SIZE:
            break
    st["backoff_s"], st["backoff_until"] = 0, None

    by_handle = {p["handle"]: p for p in products}
    old_home = st.get("home")
    st["home"] = home
    featured = by_handle.get(home["handle"]) if home["handle"] else None
    placeholder = featured is not None and featured["type"] in SKIP_TYPES
    if old_home is None:
        log(f"first run: home page shows {home['title']!r}")
    elif home["title"] is None and old_home.get("title") is not None:
        log("home page: no product title found (layout changed?)")
        notify("Recon watch", "Couldn't find the featured tool on festoolrecon.com; the page layout may have changed.")
    elif home["handle"] != old_home.get("handle") or home["title"] != old_home.get("title"):
        if placeholder:
            record([f"SOLD OUT home page now shows {home['title']!r}"])
        elif home["title"]:
            url = f"{BASE}/products/{home['handle']}" if home["handle"] else BASE
            record([f"HOME {home['title']} {money(featured)}  {url}"])
            notify("Festool Recon: new tool on the home page", f"{home['title']} {money(featured)}".strip())

    # Background: catalogue changes, logged only.
    first_run = not st["products"]
    known = st["products"]
    current = {p["id"]: p for p in products if p["type"] not in SKIP_TYPES}
    new, back = [], []
    for pid, p in current.items():
        old = known.get(pid)
        if old is None:
            new.append(p)
        elif p["available"] and (not old["available"] or not old.get("listed", True)):
            back.append(p)
    for pid, old in known.items():
        if pid not in current:
            old["listed"] = False
    for pid, p in current.items():
        known[pid] = {**p, "listed": True, "first_seen": known.get(pid, {}).get("first_seen", now().isoformat())}
    st["pages"] = pages
    st["last_ok"] = now().isoformat()
    save_state(st)

    in_stock = sum(p["available"] for p in current.values())
    if first_run:
        log(f"first run: recorded {len(current)} listings ({in_stock} in stock)")
        return
    log(f"ok: home {home['title']!r}; {len(current)} listings, {in_stock} in stock, {len(new)} new, {len(back)} restocked")

    def line(tag, p):
        stock = "" if p["available"] else " (sold out)"
        return f"{tag} {p['title']} {money(p)}{stock}  {BASE}/products/{p['handle']}"

    if new or back:
        record([line("NEW", p) for p in new] + [line("RESTOCK", p) for p in back])


if __name__ == "__main__":
    if "--now" not in sys.argv:
        time.sleep(random.uniform(0, 90))
    try:
        run()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"network error, skipped this check: {e}")  # next run retries; no backoff
