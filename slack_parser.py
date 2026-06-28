"""
slack_parser.py — Extract URLs, PDF file uploads, and threaded comments from
Slack JSON exports.

A Slack export is a JSON array of message objects. Top-level messages and
thread replies live in the same array; replies have `parent_user_id` and
`thread_ts` set. Channel-bookkeeping messages (joins, purpose changes, etc.)
have a `subtype` and are skipped.
"""

from __future__ import annotations

import json
import re
from typing import Optional, TypedDict

from database import Comment


# ── Regexes ───────────────────────────────────────────────────────────────────

# <https://example.com>  or  <https://example.com|label>
URL_RE = re.compile(r'<(https?://[^>|]+)(?:\|[^>]*)?>?')

# DOI in free text or URL path
DOI_RE = re.compile(r'(10\.\d{4,}/[^\s&?#"\'<>]+)', re.I)

# <@U03M8H8N82Z> — Slack user mention; group(1) is the user id
USER_MENTION_RE = re.compile(r'<@([A-Z0-9]+)>')


# ── Types ─────────────────────────────────────────────────────────────────────

class ThreadedLink(TypedDict):
    url: str
    doi: Optional[str]        # extracted from the URL path, None if not present
    comments: list[Comment]   # the entire Slack thread the URL appeared in,
                              # in chronological order (parent first, then replies)


class ThreadedFile(TypedDict):
    url: str                  # the file's Slack permalink — stable, token-free
    download_url: str         # url_private — carries an expiring token; fetch now
    name: str                 # original filename, e.g. "hepburn_2018.pdf"
    comments: list[Comment]   # the whole thread, chronological


class ParsedFile(TypedDict):
    path: str
    messages: int             # raw message count (including subtypes)
    links: list[ThreadedLink]
    files: list[ThreadedFile]  # PDF *uploads* (the files[] array)


# ── Public API ────────────────────────────────────────────────────────────────

def parse_files(filepaths: list[str]) -> list[ParsedFile]:
    """
    Load and parse a list of Slack export files.

    Done in two passes so the user-id → real-name map sees every message
    before any text gets cleaned: a user might post a URL in one file but
    only have their real_name set in their user_profile in another.
    """
    loaded: list[tuple[str, list[dict]]] = []
    user_map: dict[str, str] = {}

    # Pass 1: load + collect user map
    for path in filepaths:
        try:
            with open(path, encoding="utf-8") as f:
                messages = json.load(f)
        except Exception:
            continue
        if not isinstance(messages, list):
            continue
        loaded.append((path, messages))
        _absorb_user_profiles(messages, user_map)

    # Pass 2: extract links + file uploads per file using the complete user map
    return [{
        "path": path,
        "messages": len(messages),
        "links": extract_links(messages, user_map),
        "files": extract_file_attachments(messages, user_map),
    } for path, messages in loaded]


def extract_links(messages: list[dict], user_map: dict[str, str]) -> list[ThreadedLink]:
    """
    Pull URLs out of a single export's messages, preserving the full Slack
    thread each URL was discussed in.

    Strategy:
      1. Group messages by thread (thread_ts if present, else ts — standalone
         messages become a "thread of one").
      2. For each thread, find every URL that appears in any message's text.
      3. Each unique URL becomes a ThreadedLink, with the *entire* thread
         attached as comments. That way reply discussion is preserved even
         when the replies don't themselves contain the URL.

    URL deduplication across files is the database's job, not ours.
    """
    threads: dict[str, list[dict]] = {}
    for msg in messages:
        if msg.get("subtype"):
            continue  # skip channel_join, channel_purpose, etc.
        thread_id = msg.get("thread_ts") or msg.get("ts")
        if not thread_id:
            continue
        threads.setdefault(thread_id, []).append(msg)

    links: list[ThreadedLink] = []
    for thread_msgs in threads.values():
        # Sort chronologically — Slack usually exports in order, but don't trust it
        thread_msgs.sort(key=lambda m: float(m.get("ts") or 0))

        urls_in_thread: list[str] = []
        seen: set[str] = set()
        for m in thread_msgs:
            for match in URL_RE.finditer(m.get("text") or ""):
                url = match.group(1).strip()
                if url not in seen:
                    seen.add(url)
                    urls_in_thread.append(url)

        if not urls_in_thread:
            continue

        comments = [_make_comment(m, user_map) for m in thread_msgs]
        for url in urls_in_thread:
            links.append({
                "url": url,
                "doi": extract_doi(url),
                "comments": comments,
            })

    return links


# Document uploads we treat as papers (PDF + Word/RTF/ODT). Images, slide decks,
# and spreadsheets are deliberately excluded — they aren't papers.
_DOC_EXTS = {"pdf", "docx", "doc", "rtf", "odt"}


def _is_document_file(f: dict) -> bool:
    name = (f.get("name") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in _DOC_EXTS:
        return True
    mt = (f.get("mimetype") or "").lower()
    return any(s in mt for s in ("pdf", "wordprocessing", "msword",
                                 "opendocument.text", "rtf"))


def extract_file_attachments(messages: list[dict],
                             user_map: dict[str, str]) -> list["ThreadedFile"]:
    """Pull PDF *uploads* (the files[] array) out of messages — the upload
    counterpart to extract_links — with the whole thread attached as comments.

    Identity is the file's Slack permalink (stable, token-free). download_url is
    url_private, whose embedded token expires, so the bytes must be fetched at
    ingest time, not deferred. File-upload messages carry subtype 'file_share',
    so — unlike extract_links, which skips every subtype — we must keep those."""
    threads: dict[str, list[dict]] = {}
    for msg in messages:
        sub = msg.get("subtype")
        if sub and sub not in ("file_share", "thread_broadcast"):
            continue  # channel_join / purpose / etc. — but keep uploads
        thread_id = msg.get("thread_ts") or msg.get("ts")
        if not thread_id:
            continue
        threads.setdefault(thread_id, []).append(msg)

    out: list[ThreadedFile] = []
    for thread_msgs in threads.values():
        thread_msgs.sort(key=lambda m: float(m.get("ts") or 0))
        comments = [_make_comment(m, user_map) for m in thread_msgs]
        seen: set[str] = set()
        for m in thread_msgs:
            for f in (m.get("files") or []):
                if not _is_document_file(f):
                    continue
                permalink = (f.get("permalink") or "").strip()
                download = (f.get("url_private") or "").strip()
                if not permalink or not download or permalink in seen:
                    continue
                seen.add(permalink)
                out.append({
                    "url": permalink,
                    "download_url": download,
                    "name": f.get("name") or "",
                    "comments": comments,
                })
    return out


def extract_doi(text: str) -> Optional[str]:
    m = DOI_RE.search(text)
    return m.group(1).rstrip("./") if m else None


def clean_text(text: str, user_map: dict[str, str]) -> str:
    """
    Resolve user mentions to @real-names, strip URL/label syntax, decode
    common HTML entities, collapse whitespace.
    """
    text = USER_MENTION_RE.sub(
        lambda m: "@" + user_map.get(m.group(1), m.group(1)),
        text,
    )
    text = URL_RE.sub("", text)
    text = (text
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


# ── Internals ─────────────────────────────────────────────────────────────────

def _absorb_user_profiles(messages: list[dict], user_map: dict[str, str]) -> None:
    for msg in messages:
        uid = msg.get("user")
        profile = msg.get("user_profile") or {}
        name = profile.get("real_name") or profile.get("display_name")
        if uid and name and uid not in user_map:
            user_map[uid] = name


def _make_comment(msg: dict, user_map: dict[str, str]) -> Comment:
    profile = msg.get("user_profile") or {}
    author = (profile.get("real_name")
              or profile.get("display_name")
              or user_map.get(msg.get("user", ""), "Unknown"))
    return {
        "author": author,
        "ts": msg.get("ts", ""),
        "is_reply": bool(msg.get("parent_user_id")),
        "text": clean_text(msg.get("text", ""), user_map),
    }
