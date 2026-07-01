"""
url_expander.py — Recover the link a tweet points to.

Tweets shared in the channel almost always link to a paper/article rather than
being the content themselves. Twitter/X renders the tweet via JavaScript, so
translation-server and the LLM (which see only raw HTML) can't read it — those
URLs land in the non_paper bucket. This module does the one thing we want: find
the external URL the tweet links to, so the normal metadata cascade can run on
*that*.

How: X's public syndication endpoint — `cdn.syndication.twimg.com/tweet-result`
— the same endpoint that powers embedded tweets on third-party sites. It needs
no login and no API key, and returns the tweet's text plus its links already
expanded (`entities.urls[].expanded_url`). We take the first link that isn't on
Twitter/X itself.

(The roadmap's original plan — fetch the tweet page and parse og:description —
no longer works: X now serves logged-out HTML fetches a login wall, and the
crawler-UA og: endpoint 404s. The syndication endpoint is the only no-auth way
left to read a tweet's links.)

Degrades gracefully: a media-only tweet, a deleted tweet, or any fetch failure
returns None, and the tweet stays a non_paper link exactly as before.
"""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import config

_TWEET_RE = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)
_SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=en&token={token}"

# Hosts that are the tweet wrapper itself, never the target we want.
_TWITTER_HOSTS = ("twitter.com", "x.com", "t.co", "twimg.com")


def is_tweet_url(url: str) -> bool:
    """True for a twitter.com / x.com status (individual tweet) URL."""
    return bool(_TWEET_RE.match((url or "").strip()))


def expand_tweet(url: str) -> tuple[str, Optional[str]]:
    """Resolve the link a tweet shares. Returns (status, target_url):

        "ok"      — found an external link; target_url is set
        "empty"   — tweet is alive but shares no external link
        "gone"    — tweet is permanently deleted/protected
        "error"   — transient failure (X down, network error, timeout)

    Dead-or-alive is decided by the tweet page's own HTTP status wherever
    possible (x.com answers 200 for live tweets and 404 for deleted ones, even
    logged out) — a signal independent of any X data format, so it keeps
    working if the syndication response changes shape or disappears. The
    syndication endpoint is used to *extract the link*; its dead/alive verdict
    (a 4xx, or an HTTP-200 "tombstone" body, which is how deleted tweets
    actually come back) only breaks ties when the page can't be reached.

    Never raises.
    """
    m = _TWEET_RE.match((url or "").strip())
    if not m:
        return ("gone", None)
    data, syndication_gone = _fetch_syndication(m.group(1))
    tombstoned = bool(data) and (
        data.get("__typename") == "TweetTombstone" or "tombstone" in data)
    if data is not None and not tombstoned:
        for entry in (data.get("entities") or {}).get("urls") or []:
            target = entry.get("expanded_url") or entry.get("url")
            if target and target.startswith("http") and not _is_twitter_host(target):
                return ("ok", target)
    # No link recovered — the tweet is dead, unreadable, or genuinely link-less.
    # Ask the page itself which it is (see docstring).
    page_gone = _fetch_page_gone(url)
    if page_gone is True:
        return ("gone", None)
    if syndication_gone or tombstoned:
        # Syndication says dead. If the page demonstrably loads the tweet does
        # exist (age-restricted tweets tombstone, for example) — keep it
        # visible as a link bookmark; otherwise trust the syndication verdict.
        return ("empty", None) if page_gone is False else ("gone", None)
    if data is None:
        return ("error", None)
    return ("empty", None)


# ── internals ─────────────────────────────────────────────────────────────────

def _fetch_syndication(tweet_id: str) -> tuple[Optional[dict], bool]:
    """Returns (data, gone). gone=True means the tweet is permanently
    deleted/protected (HTTP 4xx that won't change); data=None with gone=False
    means a transient failure (X down, network error, timeout)."""
    url = _SYNDICATION.format(id=tweet_id, token=_token(tweet_id))
    req = Request(url, headers={
        "User-Agent": config.BROWSER_USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read(500_000).decode("utf-8", errors="replace")), False
    except HTTPError as e:
        if e.code in (403, 404, 410):
            return None, True   # deleted or protected — permanent
        return None, False      # other HTTP error — possibly transient
    except Exception:
        return None, False      # network/timeout — transient


def _fetch_page_gone(url: str) -> Optional[bool]:
    """Dead-or-alive straight from the tweet's own x.com page, judged ONLY by
    the HTTP status: 200 = alive (False), 404/410 = deleted (True), anything
    else = can't tell (None). The response body is deliberately ignored, so
    this survives any markup/JSON redesign on X's side."""
    req = Request((url or "").strip(),
                  headers={"User-Agent": config.BROWSER_USER_AGENT})
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT):
            return False
    except HTTPError as e:
        return True if e.code in (404, 410) else None
    except Exception:
        return None


def _token(tweet_id: str) -> str:
    """A throwaway token the endpoint expects. Its exact value is not validated
    (empty works too), but we send a plausible id-derived base-36 string so the
    request looks like a normal embed client."""
    n = int(tweet_id) // (10 ** 10)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def _is_twitter_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _TWITTER_HOSTS)
