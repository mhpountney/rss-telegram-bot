#!/usr/bin/env python3
"""Poll RSS feeds and post new items to a Telegram channel.

State of already-seen items is kept in seen.json so nothing is posted twice.
On the very first run (no seen.json) every current item is recorded as seen
WITHOUT posting, so you don't flood the channel with the whole backlog.
"""
import html
import json
import os
import sys
import time
from pathlib import Path

import feedparser
import requests

# Feed titles can contain non-ASCII characters; force UTF-8 output so logging
# them never crashes on consoles with a legacy codepage (e.g. Windows cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.txt"
SEEN_FILE = HERE / "seen.json"

# Most recent N entries to consider per feed each run (guards against floods).
MAX_PER_FEED = 10
# Hard cap on how many keys we keep in seen.json so it can't grow forever.
SEEN_HISTORY_LIMIT = 5000

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_feeds() -> list[str]:
    urls = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def load_seen() -> list[str]:
    # A list (not a set) so the on-disk order is stable across runs and we
    # only ever produce a diff when genuinely new ids are appended.
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return []


def save_seen(seen_list: list[str]) -> None:
    # Keep only the most recent keys to bound file size.
    trimmed = seen_list[-SEEN_HISTORY_LIMIT:]
    SEEN_FILE.write_text(json.dumps(trimmed, indent=0), encoding="utf-8")


def entry_id(entry) -> str:
    return getattr(entry, "id", None) or entry.get("link") or entry.get("title", "")


def format_message(feed_title: str, entry) -> str:
    title = html.escape(entry.get("title", "(no title)"))
    link = entry.get("link", "")
    source = html.escape(feed_title)
    return f"<b>{title}</b>\n{link}\n\n<i>{source}</i>"


def send_to_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if resp.status_code == 429:
        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
        time.sleep(retry_after + 1)
        return send_to_telegram(token, chat_id, text)
    resp.raise_for_status()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERROR: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        return 1

    feeds = load_feeds()
    if not feeds:
        print("No feeds configured in feeds.txt.", file=sys.stderr)
        return 1

    seen_list = load_seen()
    seen = set(seen_list)
    first_run = not SEEN_FILE.exists()
    if first_run:
        print("First run: seeding seen.json without posting the backlog.")

    new_items = []  # (published_sort_key, feed_title, entry)
    for url in feeds:
        parsed = feedparser.parse(url)
        if parsed.bozo:
            print(f"WARN: could not fully parse {url}: {parsed.bozo_exception}")
        feed_title = parsed.feed.get("title", url)
        for entry in parsed.entries[:MAX_PER_FEED]:
            eid = entry_id(entry)
            if eid in seen:
                continue
            seen.add(eid)
            seen_list.append(eid)
            if first_run:
                continue
            sort_key = entry.get("published_parsed") or entry.get("updated_parsed")
            new_items.append((sort_key or time.gmtime(0), feed_title, entry))

    # Post oldest first so the channel reads chronologically.
    new_items.sort(key=lambda x: x[0])

    posted = 0
    for _, feed_title, entry in new_items:
        try:
            send_to_telegram(token, chat_id, format_message(feed_title, entry))
            posted += 1
            time.sleep(1)  # be gentle with the Telegram API
        except Exception as exc:  # noqa: BLE001 - keep going on a single failure
            print(f"WARN: failed to post '{entry.get('title')}': {exc}")

    save_seen(seen_list)
    print(f"Done. Posted {posted} new item(s); tracking {len(seen_list)} seen id(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
