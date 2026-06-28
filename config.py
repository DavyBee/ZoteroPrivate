"""
config.py — Paths, environment variables, and constants.

Reads a .env file in this directory (KEY=VALUE per line) without pulling in
python-dotenv. Real environment variables take precedence over .env values.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# ── Data files ────────────────────────────────────────────────────────────────

# The live database lives alone in the project root so it's unmistakable. Every
# other piece of app state — the post-enrich baseline + single-step undo
# snapshots, the ingest registry, and the LLM audit log — lives under state/ so
# it doesn't clutter the root or get mistaken for the database.
DB_PATH              = PROJECT_ROOT / "zotero_database.db"
STATE_DIR            = PROJECT_ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
# Kept for backwards compatibility with Database.load()'s two-arg signature; the
# processed-files bookkeeping now lives in a table inside DB_PATH, not this file.
PROCESSED_FILES_PATH = STATE_DIR / "processed_files.json"
LLM_AUDIT_PATH       = STATE_DIR / "llm_audit.jsonl"

# ── PDF storage ───────────────────────────────────────────────────────────────

# Where downloaded PDFs live. Lab members import these into Zotero desktop
# manually, where the recognizer fills in metadata. See pdf_storage.py.
PDFS_DIR        = PROJECT_ROOT / "pdfs"
PDF_MAX_BYTES   = 50 * 1024 * 1024     # 50 MB per file — skip larger ones
PDF_FETCH_BYTES = 60 * 1024 * 1024     # hard read cap (slightly above max)

# ── Network ───────────────────────────────────────────────────────────────────

USER_AGENT     = "eviction-lab-zotero-importer/1.0"
# A realistic Chrome string for content fetches that get bot-detected when
# we use the friendly USER_AGENT (e.g., bse.eu, some BMJ pages, Cloudflare-
# protected publisher CDNs). Used by pdf_storage.py for PDF downloads.
# The friendly USER_AGENT is still preferred for API calls (CrossRef gives
# polite-UA clients better priority); Citoid has its own contact-carrying UA
# (CITOID_USER_AGENT) required by Wikimedia's usage policy.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
CROSSREF_BASE  = "https://api.crossref.org/works"
HTTP_TIMEOUT   = 15    # seconds

# Citoid — Wikimedia's hosted Zotero-translator service (the same translator
# engine Zotero's browser connector uses), reached over plain HTTPS with no
# Docker. GET <base><url-encoded URL> returns a JSON array of Zotero-native
# items. This replaces the old self-hosted zotero/translation-server.
CITOID_BASE = "https://en.wikipedia.org/api/rest_v1/data/citation/zotero/"

# Wikimedia's REST usage policy requires a descriptive User-Agent with contact
# info; using the friendly default without contact risks being throttled/blocked.
CITOID_USER_AGENT = (
    "eviction-lab-zotero-importer/1.0 "
    "(https://github.com/DavyBee/ZoteroForEvictionLab; david.beeson123@gmail.com)"
)

# Minimum seconds between Citoid requests (politeness throttle for the free,
# SLA-less service — matters most during a full-history backfill). Applied in
# metadata._citoid_get.
CITOID_MIN_INTERVAL = 1.0

# ── LLM ───────────────────────────────────────────────────────────────────────

LLM_MODEL_DEFAULT = "claude-haiku-4-5"

# Max characters of fetched page text handed to the LLM stage (HTML stripped of
# tags, or text extracted from a PDF). Generous on purpose: these runs are
# infrequent and cheap, and on a publisher page the author byline can sit well
# past a small cap. See metadata._fetch_page_text.
LLM_PAGE_TEXT_CHARS = 30_000


# ── .env loading ──────────────────────────────────────────────────────────────

def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """
    Tiny .env reader. Comments and blanks are ignored; values can be optionally
    wrapped in single or double quotes.

    By default (override=False) it only sets os.environ entries that aren't
    already set, so real shell variables win — that's the right behavior for the
    one-time load at import. With override=True the *file* wins instead: the GUI
    calls it on every rerun (see app.main) so the .env file is the authoritative
    source of settings and a hand-edited value takes effect on the next refresh,
    rather than being shadowed by a stale value already in os.environ.
    """
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # Strip inline comments (whitespace + # after the value), matching shell
        # .env convention. Done BEFORE trimming so a blank value followed only by
        # a comment (e.g. "KEY=    # note") resolves to empty — if we trimmed
        # first the leading spaces would vanish and the value would start with
        # "#", hiding the " #" marker and leaving the comment text as the value.
        if " #" in val:
            val = val[: val.index(" #")]
        val = val.strip().strip('"').strip("'")
        if override:
            os.environ[key] = val
        else:
            os.environ.setdefault(key, val)


def load_streamlit_secrets() -> None:
    """Bridge Streamlit's secrets manager into os.environ.

    Streamlit Community Cloud has no .env — secrets are entered in the app's
    dashboard (TOML) and exposed at runtime as st.secrets. We copy each top-level
    scalar secret into os.environ (without overriding anything already set), so
    every accessor below keeps reading os.environ and the rest of the codebase is
    byte-for-byte identical whether it runs on the cloud or locally.

    A no-op when Streamlit isn't importable, isn't running, or has no secrets
    configured — i.e. all local-dev and CLI cases — so config stays cheap there.
    Called AFTER load_dotenv() so a local .env (and real shell vars) take
    precedence; on the cloud, where neither exists, st.secrets fills the gap.
    """
    try:
        import streamlit as st
    except Exception:
        return
    try:
        items = list(st.secrets.items())
    except Exception:
        # No secrets.toml / not inside a Streamlit runtime — nothing to bridge.
        return
    for key, val in items:
        if isinstance(val, (str, int, float, bool)):
            os.environ.setdefault(key, str(val))


# Auto-load on import (idempotent, only sets vars that aren't already set).
load_dotenv()
load_streamlit_secrets()


# ── Accessors ─────────────────────────────────────────────────────────────────

def zotero_api_key() -> str | None:
    return os.environ.get("ZOTERO_API_KEY")

def zotero_library_id() -> str | None:
    return os.environ.get("ZOTERO_LIBRARY_ID")

def zotero_library_type() -> str:
    return os.environ.get("ZOTERO_LIBRARY_TYPE", "group")

def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")

def zotero_collection_key() -> str | None:
    return os.environ.get("ZOTERO_COLLECTION_KEY")

def llm_model() -> str:
    # Fall back on an empty value too, not just an absent one: the .env ships
    # LLM_MODEL blank ("use the built-in default"), and .get's default only
    # covers a missing key, so an empty string would otherwise stick.
    return os.environ.get("LLM_MODEL") or LLM_MODEL_DEFAULT

# ── Writing settings back ───────────────────────────────────────────────────

def update_env(updates: dict[str, str]) -> list[str]:
    """Apply settings to the running process (os.environ) AND persist them to
    the .env file. Existing .env lines/comments are preserved; changed keys are
    rewritten in place, new keys appended. Blank values are skipped (use a key's
    existing value rather than clearing it). Called by the Settings page.

    Returns the list of keys whose value actually changed (so the UI can report
    exactly what was written, instead of always claiming success).
    """
    updates = {k: v for k, v in updates.items() if v is not None and v != ""}
    if not updates:
        return []
    changed = sorted(k for k, v in updates.items() if os.environ.get(k) != v)
    for k, v in updates.items():
        os.environ[k] = v   # override (unlike load_dotenv's setdefault)

    path = PROJECT_ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed
