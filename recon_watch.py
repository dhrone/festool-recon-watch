#!/usr/bin/env python3
"""Watch festoolrecon.com for new listings and restocks; alert with the tool names.

The store is Shopify, so the whole catalogue is public JSON at
/products.json?limit=250&page=N (about 3 pages). Each page is fetched with
If-None-Match, so an unchanged page costs the store a 304 and no body.

Alerts:
  NEW       a product id never seen before (in stock or not)
  RESTOCK   a known product that was sold out, or gone from the list, and is now in stock

State lives in state/recon-state.json; alerts are appended to logs/alerts.log
and shown as a macOS notification. The first run only records what is there.

Politeness: one run every 15 min (launchd), a random 0-90 s start delay, a few
seconds between pages, an honest User-Agent, and on 429/403/5xx or a non-JSON
reply (a Cloudflare challenge) it stops and backs off (Retry-After, else
30 min doubling to 4 h) instead of retrying.
"""
import json
import random
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


def fetch_page(n, cached):
    url = f"{BASE}/products.json?limit={PAGE_SIZE}&page={n}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    if cached and cached.get("etag"):
        req.add_header("If-None-Match", cached["etag"])
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return cached["products"], True
        if e.code in (403, 429, 430) or e.code >= 500:
            ra = e.headers.get("Retry-After")
            raise Blocked(e.code, int(ra) if ra and ra.isdigit() else None)
        raise
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


def run():
    st = load_state()
    if st.get("backoff_until") and now() < datetime.fromisoformat(st["backoff_until"]):
        log(f"backing off until {st['backoff_until']}")
        return

    products, pages = [], {}
    for n in range(1, MAX_PAGES + 1):
        if n > 1:
            time.sleep(random.uniform(3, 8))
        cached = st["pages"].get(str(n))
        try:
            items, page_state = fetch_page(n, cached)
        except Blocked as b:
            wait = b.retry_after or min(max(st.get("backoff_s", 0) * 2, 1800), 4 * 3600)
            first = not st.get("backoff_s")
            st["backoff_s"] = wait
            st["backoff_until"] = (now() + timedelta(seconds=wait)).isoformat()
            save_state(st)
            log(f"blocked on page {n}: {b}; backing off {wait // 60} min")
            if first:
                notify("Recon watch paused", f"festoolrecon.com replied {b}; pausing {wait // 60} min.")
            return
        pages[str(n)] = cached if page_state is True else page_state
        products += items
        if len(items) < PAGE_SIZE:
            break

    st["backoff_s"], st["backoff_until"] = 0, None
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
        log(f"first run: recorded {len(current)} listings ({in_stock} in stock); no alerts")
        return
    log(f"ok: {len(current)} listings, {in_stock} in stock, {len(new)} new, {len(back)} restocked")
    if not (new or back):
        return

    def line(tag, p):
        price = f"${p['price']:,.2f}" if p["price"] is not None else ""
        stock = "" if p["available"] else " (sold out)"
        return f"{tag} {p['title']} {price}{stock}  {BASE}/products/{p['handle']}"

    lines = [line("NEW", p) for p in new] + [line("RESTOCK", p) for p in back]
    ALERTS.parent.mkdir(exist_ok=True)
    with ALERTS.open("a") as f:
        for l in lines:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M} {l}\n")
    for l in lines:
        log(l)
    names = [p["title"] for p in new + back]
    title = f"Festool Recon: {len(new)} new, {len(back)} restocked" if new and back else (
        f"Festool Recon: new listing{'s' * (len(new) > 1)}" if new else f"Festool Recon: back in stock")
    notify(title, "; ".join(names))


if __name__ == "__main__":
    if "--now" not in sys.argv:
        time.sleep(random.uniform(0, 90))
    run()
