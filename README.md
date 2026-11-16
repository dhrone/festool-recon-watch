# Festool Recon watch

`recon_watch.py` checks festoolrecon.com every 15 minutes on GitHub Actions (`.github/workflows/recon-watch.yml`, public repo dhrone/festool-recon-watch). The timer is cron-job.org calling the workflow_dispatch API (GitHub's own scheduler skipped 45% of another repo's runs); the :07/:22/:37/:52 schedule is a backup. Every run pings healthchecks.io (`HC_PING_URL` secret), which alerts through ntfy if no check succeeds for 40 minutes and pushes alerts through ntfy (`NTFY_TOPIC` secret; the topic is also in `~/.config/ntfy-topic`). State and `logs/alerts.log` are committed by the workflow when they change. The old Mac job (`launchd.plist`) is disabled; run locally without `NTFY_TOPIC` and it falls back to a macOS notification.

Recon features one tool at a time on its home page. **When that tool changes, a macOS notification names it with its price** (`HOME` in `logs/alerts.log`, with the link). When it sells out the home page shows a "Wow, that went fast!!!" placeholder; that is logged as `SOLD OUT`, not notified.

In the background it reads the whole catalogue (about 500 listings, nearly all old and sold out; a dozen or so marked in stock without being featured) and logs, without a notification:

- **NEW**: a listing never seen before;
- **RESTOCK**: a known listing that was sold out (or missing) and is now in stock.

`logs/watch.log` has one line per run.

## How it stays under the store's limits

The store runs on Shopify behind Cloudflare. Each run reads the home page and the catalogue, about 3 pages of `/products.json` (the public endpoint the storefront serves; robots.txt allows it). Each run:

- starts after a random 0–90 s delay, and waits 3–8 s between pages;
- sends `If-None-Match`, so an unchanged page comes back as a 304 with no body;
- identifies itself honestly in the User-Agent;
- on 429/430/403/5xx or a non-JSON (challenge) reply, stops and backs off (Retry-After, else 30 min doubling to 4 h), and sends one "paused" notification.

That comes to at most 16 small requests an hour, fewer than one person browsing the site.

## Commands

    python3 recon_watch.py --now          # run once without the start delay
    tail -f logs/watch.log                # watch the runs
    launchctl bootout gui/$(id -u)/org.ritchey.recon-watch     # stop
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.ritchey.recon-watch.plist  # start

The dhrone/actions-watcher repo watches this workflow along with the rest.
