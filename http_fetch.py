"""
http_fetch.py — the pipeline's single HTTP GET, with browser impersonation.

Every content fetch (publisher HTML for the LLM stage, PDF downloads) goes
through fetch() so the one anti-bot knob lives in exactly one place.

Why this exists: many publisher CDNs sit behind Cloudflare and return 403 to a
plain urllib request — they fingerprint the TLS/HTTP2 handshake and the
User-Agent — even for open-access content. curl_cffi issues the request with a
real browser's fingerprint, which the *fingerprint-gated* hosts (ScienceDirect,
Lancet, MIT Press, Duke UP, …) admit. Hosts running a full Cloudflare JS
challenge ("Just a moment…") still block us — that needs a JS runtime we
deliberately don't ship — so those papers (JAMA, BMJ, SSRN, Oxford) stay on the
manual "Access issues" path.

curl_cffi is optional. If it isn't installed (or the native wheel won't load)
we fall back to urllib with the browser User-Agent — i.e. exactly the previous
behaviour: no rescue, but everything still works. Ongoing maintenance is a
periodic `curl_cffi` version bump (it ships the fingerprints); failure is
graceful — a blocked fetch just returns nothing, same as before.
"""

from __future__ import annotations

import time
from typing import Optional

import config

try:
    from curl_cffi import requests as _cffi
except Exception:           # ImportError, or a broken/incompatible native wheel
    _cffi = None

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# curl_cffi impersonation target. "chrome" tracks the newest fingerprint the
# installed curl_cffi knows about; bumping the package is what keeps it current.
IMPERSONATE = "chrome"

_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
           "application/pdf,*/*;q=0.8")
_HEADERS = {"Accept": _ACCEPT, "Accept-Language": "en-US,en;q=0.9"}

_CHUNK = 65536


def fetch(url: str, *, max_bytes: int, timeout: Optional[int] = None,
          retries: int = 1) -> tuple[Optional[bytes], str, int]:
    """GET `url` like a browser. Returns (body, content_type, status):
      - 2xx        → (bytes, content_type, status_code)
      - non-2xx    → (None, "", status_code)   e.g. 404, 403, 503
      - network    → (None, "", 0)             DNS/connection/timeout, no HTTP
    Reads at most `max_bytes` (a runaway server can't fill memory).

    The `status` lets callers tell a definitively-gone page (404/410) apart from
    a block (403/429/5xx) or a transient network error — only 404/410 are a
    reliable "dead page" signal. Retries once by default with a 1s backoff
    (transient 5xx / rate limits), but never wastes a retry on 404/410.
    """
    timeout = timeout or config.HTTP_TIMEOUT
    status = 0
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(1.0)
        body, ct, status = (_get_cffi(url, max_bytes, timeout) if _cffi
                            else _get_urllib(url, max_bytes, timeout))
        if body is not None:
            return body, ct, status
        if status in (404, 410):          # definitively gone — don't retry
            break
    return None, "", status


def _get_cffi(url, max_bytes, timeout):
    """Browser-impersonating fetch via curl_cffi. Streams so the byte cap is
    honoured even when the server ignores length headers. Third return value is
    the HTTP status (0 if no response was received)."""
    try:
        r = _cffi.get(url, impersonate=IMPERSONATE, headers=_HEADERS,
                      timeout=timeout, stream=True)
    except Exception:
        return None, "", 0
    try:
        if not (200 <= r.status_code < 300):
            return None, "", r.status_code   # 403 challenge, 404 gone, etc.
        ct = (r.headers.get("Content-Type") or "").lower()
        buf = bytearray()
        for chunk in r.iter_content(_CHUNK):
            buf += chunk
            if len(buf) >= max_bytes:
                break
        return bytes(buf), ct, r.status_code
    except Exception:
        return None, "", 0
    finally:
        try:
            r.close()
        except Exception:
            pass


def _get_urllib(url, max_bytes, timeout):
    """Fallback when curl_cffi is unavailable: stdlib urllib with the browser
    User-Agent (the previous behaviour — no fingerprint impersonation). Third
    return value is the HTTP status (the HTTPError code on an error response, 0
    when no HTTP response arrived at all)."""
    req = Request(url, headers={"User-Agent": config.BROWSER_USER_AGENT, **_HEADERS})
    try:
        with urlopen(req, timeout=timeout) as resp:
            ct = (resp.headers.get("Content-Type") or "").lower()
            return resp.read(max_bytes), ct, getattr(resp, "status", 200)
    except HTTPError as e:
        return None, "", e.code             # the server answered with an error
    except (URLError, TimeoutError):
        return None, "", 0                  # no HTTP response (DNS/conn/timeout)
    except Exception:
        return None, "", 0
