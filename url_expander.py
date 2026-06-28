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

        "ok"    — found an external link; target_url is set
        "empty" — the tweet was read fine but shares no external link
        "error" — the tweet couldn't be read (not a tweet, endpoint down,
                  deleted/protected tweet, network failure)

    The caller must treat "error" differently from "empty": an "error" should
    NOT be marked as processed, so it retries once the endpoint is reachable
    again. Never raises.
    """
    m = _TWEET_RE.match((url or "").strip())
    if not m:
        return ("error", None)
    data = _fetch_syndication(m.group(1))
    if data is None:
        return ("error", None)
    for entry in (data.get("entities") or {}).get("urls") or []:
        target = entry.get("expanded_url") or entry.get("url")
        if target and target.startswith("http") and not _is_twitter_host(target):
            return ("ok", target)
    return ("empty", None)


# ── internals ─────────────────────────────────────────────────────────────────

def _fetch_syndication(tweet_id: str) -> Optional[dict]:
    url = _SYNDICATION.format(id=tweet_id, token=_token(tweet_id))
    req = Request(url, headers={
        "User-Agent": config.BROWSER_USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read(500_000).decode("utf-8", errors="replace"))
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
