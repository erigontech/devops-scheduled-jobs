#!/usr/bin/env python3
"""Post new RSS/Atom entries to a notification webhook.

Input (env):
  RSS_FEEDS        lines "name | url [| exclude-regex]"; blank lines and '#' comments ignored
  RSS_EXCLUDE      default exclude regex for titles (e.g. pre-releases), used when a line has none
  NOTIFICATION_WEBHOOK  webhook URL (secret); not needed with DRY_RUN=true
  STATE_FILE       JSON state (seen entry IDs, consecutive failures), persisted by the workflow cache
  FAIL_AFTER       consecutive fetch failures of one feed before the run fails (default 3)
  MAX_PER_FEED     max posts per feed per run, the rest is only marked seen (default 5)
  DRY_RUN          "true" = print instead of posting
  RESEED           "true" = mark everything seen without posting

A feed seen for the first time is seeded (marked seen, nothing posted) so adding a feed never floods the channel.
Feed content is untrusted: mentions are disabled and text is truncated.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "infra-notify (+https://github.com)"}
KEEP_IDS = 200  # IDs kept beyond those still present in the feed


def env_bool(name):
    return os.environ.get(name, "").strip().lower() == "true"


def parse_feeds(text, default_exclude):
    feeds = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) < 2 or not parts[1].startswith("https://"):
            sys.exit(f"RSS_FEEDS line {n}: expected 'name | https://url [| exclude-regex]'")
        exclude = parts[2] if len(parts) == 3 else default_exclude
        feeds.append({"name": parts[0], "url": parts[1], "exclude": re.compile(exclude) if exclude else None})
    if len({f["name"] for f in feeds}) != len(feeds):
        sys.exit("RSS_FEEDS: feed names must be unique")
    return feeds


def fetch(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise
            print(f"  retry {url}: {e}")
            time.sleep(5 * (i + 1))


def entries(xml):
    """Return [(id, title, link)] newest first, for Atom or RSS 2.0."""
    root = ET.fromstring(xml)
    out = []
    for e in root.iter(ATOM + "entry"):
        link = next((l.get("href") for l in e.findall(ATOM + "link") if l.get("rel", "alternate") == "alternate"), None)
        out.append((e.findtext(ATOM + "id") or link, e.findtext(ATOM + "title") or "", link))
    for e in root.iter("item"):
        link = e.findtext("link")
        out.append((e.findtext("guid") or link, e.findtext("title") or "", link))
    # GitHub release titles are often names ("Sleepy Gary"): prefix the tag so it is visible and filterable
    out = [(i, f"{l.rsplit('/', 1)[1]} — {t}" if l and "/releases/tag/" in l and l.rsplit("/", 1)[1] not in t else t, l)
           for i, t, l in out]
    return [x for x in out if x[0]]


def clean(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def post(webhook, payload):
    payload["allowed_mentions"] = {"parse": []}  # never ping from feed content
    data = json.dumps(payload).encode()
    for _ in range(5):
        req = urllib.request.Request(webhook, data, {**UA, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).close()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise RuntimeError(f"Remote Service webhook HTTP {e.code}") from None  # never echo the webhook URL
            time.sleep(float(json.load(e).get("retry_after", 2)) + 0.5)
    raise RuntimeError("Remote service rate limit not cleared")


def main():
    feeds = parse_feeds(os.environ.get("RSS_FEEDS", ""), os.environ.get("RSS_EXCLUDE", ""))
    state_file = os.environ.get("STATE_FILE", "state/rss.json")
    dry, reseed = env_bool("DRY_RUN"), env_bool("RESEED")
    fail_after = int(os.environ.get("FAIL_AFTER", "3"))
    max_per_feed = int(os.environ.get("MAX_PER_FEED", "5"))
    webhook = os.environ.get("NOTIFICATION_WEBHOOK", "")
    if not webhook and not dry:
        sys.exit("NOTIFICATION_WEBHOOK is not set")

    try:
        with open(state_file) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    feeds_state = state.setdefault("feeds", {})
    hard_failures, summary = [], []

    for feed in feeds:
        st = feeds_state.setdefault(feed["name"], {"seen": [], "failures": 0})
        first = not st["seen"]
        try:
            items = entries(fetch(feed["url"]))
        except Exception as e:  # noqa: BLE001 - one bad feed must not block the others
            st["failures"] += 1
            msg = f"{feed['name']}: {type(e).__name__}: {clean(str(e), 200)} ({st['failures']} in a row)"
            print("  FAIL " + msg)
            summary.append(f"| {feed['name']} | fetch failed ({st['failures']} in a row) |")
            if st["failures"] >= fail_after:
                hard_failures.append(msg)
            continue
        st["failures"] = 0
        seen = set(st["seen"])
        new = [x for x in items if x[0] not in seen]
        shown = [x for x in new if not (feed["exclude"] and feed["exclude"].search(x[1]))]
        if first or reseed:
            note = f"seeded {len(items)}"
        else:
            batch = list(reversed(shown[:max_per_feed]))  # oldest first
            for i in range(0, len(batch), 10):  # Remote service: max 10 embeds per message
                embeds = [{"title": clean(t, 250), "url": l, "footer": {"text": feed["name"]}} for _, t, l in batch[i:i + 10]]
                if dry:
                    for em in embeds:
                        print(f"  DRY {feed['name']}: {em['title']} {em['url']}")
                else:
                    post(webhook, {"embeds": embeds})
            note = f"posted {len(batch)}, filtered {len(new) - len(shown)}, skipped {max(0, len(shown) - max_per_feed)}"
        current = [x[0] for x in items]  # remember everything still in the feed, plus some history
        st["seen"] = current + [i for i in st["seen"] if i not in set(current)][:KEEP_IDS]
        print(f"  ok   {feed['name']}: {len(items)} entries, {note}")
        summary.append(f"| {feed['name']} | {note} |")

    for gone in set(feeds_state) - {f["name"] for f in feeds}:
        del feeds_state[gone]  # removed from RSS_FEEDS
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### RSS feeds\n\n| Feed | Result |\n|---|---|\n" + "\n".join(summary) + "\n")
    if hard_failures:
        sys.exit("Feeds failing repeatedly:\n" + "\n".join(hard_failures))


if __name__ == "__main__":
    main()
