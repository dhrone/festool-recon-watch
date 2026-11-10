# Festool Recon watch

`recon_watch.py` checks festoolrecon.com every 15 minutes (launchd job `org.ritchey.recon-watch`, copy of the plist in `launchd.plist`) and raises a macOS notification naming each tool that is:

- **NEW**: a listing never seen before, or
- **RESTOCK**: a known listing that was sold out (or missing) and is now in stock.

Each alert is also appended to `logs/alerts.log` with its price and link; `logs/watch.log` has one line per run.

## How it stays under the store's limits

The store runs on Shopify behind Cloudflare. The full catalogue is about 3 pages of `/products.json` (the public endpoint the storefront serves; robots.txt allows it). Each run:

- starts after a random 0–90 s delay, and waits 3–8 s between pages;
- sends `If-None-Match`, so an unchanged page comes back as a 304 with no body;
- identifies itself honestly in the User-Agent;
- on 429/430/403/5xx or a non-JSON (challenge) reply, stops and backs off (Retry-After, else 30 min doubling to 4 h), and sends one "paused" notification.

That comes to at most 12 small requests an hour, fewer than one person browsing the site.

## Commands

    python3 recon_watch.py --now          # run once without the start delay
    tail -f logs/watch.log                # watch the runs
    launchctl bootout gui/$(id -u)/org.ritchey.recon-watch     # stop
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.ritchey.recon-watch.plist  # start

Runs only while the Mac is awake; a check missed during sleep runs on wake.
