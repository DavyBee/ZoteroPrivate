"""
metadata.py — Fetch bibliographic metadata for a Paper.

The cascade, in order, stops at the first success:
    1. CrossRef DOI   — direct DOI lookup, when a DOI is already known
                        (authoritative, and polite to Citoid for the common case)
    2. Citoid         — Wikimedia's hosted Zotero-translator service over plain
                        HTTPS (no Docker); returns Zotero-native items for
                        accessible landing pages
    3. LLM (Claude)   — fetches the page and asks for structured JSON

Citoid runs the same translator engine that powers Zotero's browser connector
and produces output already in Zotero's native schema — no field-mapping
translation needed — but as a hosted, free service it has no SLA, so it sits
behind CrossRef (for DOIs) and ahead of the Claude fallback (for everything it
can't resolve: DOIs it misses, bot-blocked publishers, paywalls). This replaces
the old self-hosted zotero/translation-server (Docker); no local server is run.

Citoid requests are throttled and per-URL cached within a run (see _citoid_get)
to respect Wikimedia's REST usage policy.

Public entry point: enrich_paper(paper, anthropic_key, audit_log_path).
LLM calls are appended to llm_audit.jsonl regardless of success.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import config
import http_fetch
from database import Comment
from pdf_storage import save_pdf, save_slack_file, copy_to_upload_queue


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> Optional[dict]:
    """GET a URL expecting JSON. Returns parsed object or None on any error."""
    req = Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except HTTPError:
        return None
    except Exception:
        return None


def _fetch_page_text(url: str) -> tuple[str, str]:
    """
    Best-effort fetch of a URL's content for the LLM stage. Returns
    `(text, state)`:
      - text  — HTML tags stripped (first config.LLM_PAGE_TEXT_CHARS chars), or
                PDF text extracted via pypdf; "" when there's no extractable
                text (e.g. an image).
      - state — one of:
          "ok"      we got a 2xx — we actually saw the page (even if no text
                    came out of it, e.g. an image).
          "dead"    HTTP 404/410 — the server is alive and says the resource is
                    gone. The ONLY reliable "dead page" signal.
          "blocked" everything else (403/429/5xx, or a network/DNS/timeout
                    error): we didn't see the page and can't tell whether it's
                    permanently gone, so we treat it as a block.
    The cascade uses `state` to route a metadata-less result: "ok" → sort by the
    LLM's category (Links/Junk); "dead" non-papers → Likely junk; "blocked" (and
    anything still paper-shaped) → Access issues.

    Fetching goes through http_fetch.fetch — browser impersonation via
    curl_cffi when available — which is what gets us real content from the
    fingerprint-gated publisher CDNs that 403 a plain request. Hosts running a
    full JS challenge still return nothing here, so the paper falls to the
    manual "Access issues" path, same as before.
    """
    raw, content_type, status = http_fetch.fetch(url, max_bytes=500_000)
    if raw is None:
        return "", ("dead" if status in (404, 410) else "blocked")

    if "text/html" in content_type:
        html = raw.decode("utf-8", errors="replace")
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:config.LLM_PAGE_TEXT_CHARS], "ok"

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(raw)
        return (text[:config.LLM_PAGE_TEXT_CHARS] if text.strip()
                else "[PDF — text not extracted]"), "ok"

    return "", "ok"             # reachable (2xx) but no extractable text


# ── Citoid (Wikimedia's hosted Zotero translator) ─────────────────────────────

# Per-run cache (url → first Zotero item, or None) and a throttle timestamp. A
# big export can reference the same landing page more than once, and Wikimedia's
# REST usage policy asks callers to be polite — so we cache results for the run
# and space requests at least config.CITOID_MIN_INTERVAL apart.
_CITOID_CACHE: dict[str, Optional[dict]] = {}
_last_citoid_call = 0.0


def _citoid_get(url: str) -> Optional[dict]:
    """
    GET Citoid's Zotero endpoint for `url` and return the first Zotero-native
    item, or None. Citoid returns a JSON array of items; we take the first.

    Cached per-URL for the current run and throttled to respect Wikimedia's REST
    usage policy. A descriptive, contact-carrying User-Agent (config.
    CITOID_USER_AGENT) is required by that policy.
    """
    global _last_citoid_call
    if url in _CITOID_CACHE:
        return _CITOID_CACHE[url]

    wait = config.CITOID_MIN_INTERVAL - (time.monotonic() - _last_citoid_call)
    if wait > 0:
        time.sleep(wait)

    api = config.CITOID_BASE + url_quote(url, safe="")
    req = Request(api, headers={
        "User-Agent": config.CITOID_USER_AGENT,
        "Accept": "application/json",
    })
    item: Optional[dict] = None
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list) and data:
            item = data[0]
    except Exception:
        item = None
    finally:
        _last_citoid_call = time.monotonic()

    _CITOID_CACHE[url] = item
    return item


def fetch_translator(url: str) -> Optional[dict]:
    """
    Resolve `url` through Citoid (the hosted Zotero translator).

    No publisher-specific URL rewriting is done here — if Citoid can't handle
    the URL (e.g. it's a PDF, or a bot-blocked publisher), we let the cascade
    fall through to the LLM. The whole point of the translator is to handle the
    URL → metadata mapping for us; we don't want to maintain a parallel list of
    "publisher X's URL pattern Y maps to Z."

    Returns a non-junk Zotero item, or None. (Named fetch_translator for
    continuity — its output still flows to the "translator" metadata_source.)
    """
    raw = _citoid_get(url)
    if raw and not _is_translator_junk(raw):
        return raw
    return None


def fetch_webpage_metadata(url: str) -> Optional[dict]:
    """
    Run Citoid on a URL and return its raw extraction WITHOUT the citation-grade
    junk filter that fetch_translator applies.

    fetch_translator() rejects a title-only result because, for a paper, a
    bare webpage is junk. But for "save this URL as whatever Zotero makes of
    it" (the link-upload path for non-papers), that title-only webpage item is
    exactly what we want — it's what the Zotero connector / "Save to Zotero"
    button produces for an arbitrary page (the Embedded Metadata translator
    reading <title> / og: tags into a webpage item).

    Returns the flat fields dict (same shape as translator_to_fields), or None
    if Citoid returned nothing usable at all.
    """
    raw = _citoid_get(url)
    if raw and (raw.get("title") or "").strip():
        return translator_to_fields(raw)
    return None


def lookup_doi(doi: str) -> Optional[tuple[str, dict]]:
    """
    Resolve a DOI to a flat fields dict via a direct CrossRef call. CrossRef is
    authoritative for DOIs (Citoid often misses them), so the DOI path goes
    straight there rather than through the hosted translator.

    Returns (metadata_source_label, fields_dict) on success, or None.
    """
    data = fetch_crossref_doi(doi)
    if data and (data.get("title") or [None])[0]:
        return ("crossref_doi", crossref_to_fields(data))
    return None


def _is_pdf_url(url: str) -> bool:
    path = url.lower().split("?", 1)[0].split("#", 1)[0]
    return path.endswith(".pdf")


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """
    Pull readable text out of PDF bytes using pypdf, which handles the encodings
    our old hand-rolled regex didn't — `TJ` arrays with kerning (what LaTeX/
    pdfTeX emits for nearly all body text), hex strings, and ToUnicode CMaps.
    That covers the title/abstract/body of essentially all academic PDFs.

    Returns "" when nothing readable comes out — a scanned/image-only PDF, or a
    file pypdf can't parse — which the cascade reads as "no text" and falls back
    to the manual drag-in path.
    """
    import logging
    from io import BytesIO
    from pypdf import PdfReader

    # pypdf logs warnings to stderr on malformed/non-PDF input (e.g. a truncated
    # download or HTML mis-served as a PDF). We already handle those as "no text",
    # so silence the noise.
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = (page.extract_text() or "" for page in reader.pages)
        return "\n".join(p for p in pages if p).strip()
    except Exception:
        # Encrypted, malformed, or otherwise unreadable — treat as no text.
        return ""


def _first_pages_pdf(pdf_bytes: bytes, n: int) -> Optional[bytes]:
    """Return a new PDF containing only the first `n` pages of `pdf_bytes`, for the
    vision fallback (so Claude reads just the title/abstract pages, not the whole
    file). Returns None if the PDF can't be parsed/sliced."""
    import logging
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages[:n]:
            writer.add_page(page)
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return None


def _clean_doi(doi: str) -> str:
    """Strip trailing punctuation that often gets caught by greedy regex."""
    return doi.rstrip(".,;:)>]\"'")


def _is_translator_junk(raw: dict) -> bool:
    """
    Reject responses too thin to be a real citation. A title alone isn't
    enough — every webpage has one. Require at least one of: an author,
    a publication date, an abstract, or a DOI.
    """
    if not (raw.get("title") or "").strip():
        return True
    has_author   = bool(raw.get("creators"))
    has_year     = bool(raw.get("date"))
    has_abstract = bool(raw.get("abstractNote"))
    has_doi      = bool(raw.get("DOI"))
    if not (has_author or has_year or has_abstract or has_doi):
        return True
    return False


def translator_to_fields(raw: dict) -> dict:
    """Convert a translation-server (Zotero-native) item into our flat field dict."""
    year_raw = raw.get("date") or ""
    year = year_raw[:4] if year_raw[:4].isdigit() else None
    journal = (raw.get("publicationTitle") or raw.get("websiteTitle")
               or raw.get("bookTitle") or raw.get("publisher")
               or raw.get("institution") or raw.get("repository"))
    isbn_raw = raw.get("ISBN")
    isbn = (isbn_raw[0] if isinstance(isbn_raw, list) and isbn_raw
            else isbn_raw if isinstance(isbn_raw, str) else None)
    return {
        "title":      raw.get("title"),
        "authors":    _translator_creators(raw),
        "abstract":   raw.get("abstractNote"),
        "year":       year,
        "journal":    journal,
        "item_type":  raw.get("itemType", "journalArticle"),
        "keywords":   [t["tag"] for t in (raw.get("tags") or []) if t.get("tag")],
        "volume":     raw.get("volume"),
        "issue":      raw.get("issue"),
        "pages":      raw.get("pages"),
        "publisher":  raw.get("publisher"),
        "isbn":       isbn,
        "language":   raw.get("language"),
        "doi":        raw.get("DOI"),
    }


def _translator_creators(raw: dict) -> list[str]:
    """Translator output uses Zotero's native creators array. Filter to authors."""
    out: list[str] = []
    for c in raw.get("creators") or []:
        if c.get("creatorType") and c.get("creatorType") != "author":
            continue
        last  = (c.get("lastName") or "").strip()
        first = (c.get("firstName") or "").strip()
        name  = (c.get("name") or "").strip()  # one-name creators
        if last:
            out.append(f"{last}, {first}".rstrip(", "))
        elif name:
            out.append(name)
    return out


# ── CrossRef DOI ──────────────────────────────────────────────────────────────

_CROSSREF_TYPE_MAP = {
    "journal-article":     "journalArticle",
    "book-chapter":        "bookSection",
    "proceedings-article": "conferencePaper",
    "report":              "report",
    "book":                "book",
    "posted-content":      "preprint",
    "dataset":             "dataset",
    "dissertation":        "thesis",
    "monograph":           "book",
    "reference-entry":     "encyclopediaArticle",
}


def fetch_crossref_doi(doi: str) -> Optional[dict]:
    """Direct DOI lookup. Returns the inner 'message' dict or None."""
    raw = _http_get_json(f"{config.CROSSREF_BASE}/{url_quote(doi, safe='/')}")
    return (raw or {}).get("message") if isinstance(raw, dict) else None


def crossref_to_fields(data: dict) -> dict:
    title_list = data.get("title") or []
    container  = data.get("container-title") or []
    abstract   = re.sub(r"<[^>]+>", " ", data.get("abstract") or "").strip() or None
    return {
        "title":      title_list[0] if title_list else None,
        "authors":    _crossref_authors(data),
        "abstract":   abstract,
        "year":       _crossref_year(data),
        "journal":    container[0] if container else data.get("publisher"),
        "item_type":  _CROSSREF_TYPE_MAP.get(data.get("type") or "", "journalArticle"),
        "volume":     data.get("volume"),
        "issue":      data.get("issue"),
        "pages":      data.get("page"),
        "publisher":  data.get("publisher"),
        "doi":        data.get("DOI"),
    }


def _crossref_authors(data: dict) -> list[str]:
    out: list[str] = []
    for a in data.get("author") or []:
        last  = (a.get("family") or "").strip()
        first = (a.get("given") or "").strip()
        if last:
            out.append(f"{last}, {first}".rstrip(", "))
        elif a.get("name"):
            out.append(a["name"].strip())
    return out


def _crossref_year(data: dict) -> Optional[str]:
    for f in ("published-print", "published-online", "created"):
        parts = (data.get(f) or {}).get("date-parts") or [[]]
        if parts and parts[0]:
            return str(parts[0][0])
    return None


# ── LLM fallback ──────────────────────────────────────────────────────────────

_LLM_REQUIRED_KEYS = {"title", "authors", "year", "item_type"}

# Below this many chars of extracted page/file text, the LLM had little to work
# with and is effectively guessing from the URL + Slack comments — flag it so the
# lab member verifies. The audit log shows clean extractions carry ~6k chars.
_LOW_CONTEXT_CHARS = 500


def fetch_llm(url: str, comments: list[Comment],
              anthropic_key: str, audit_log_path: str,
              page_text: Optional[str] = None,
              pdf_doc: Optional[bytes] = None) -> Optional[dict]:
    """
    Ask Claude for structured metadata. By default fetches the page itself; pass
    `page_text` to run the LLM on already-extracted content instead (e.g. a local
    .docx's body — see enrich_local_file), in which case the content is "seen" so
    fetch_state is "ok". Pass `pdf_doc` (raw PDF bytes) instead to have Claude read
    the file with vision — the fallback for scanned / image-only PDFs with no text
    layer; the caller pre-slices it to the first pages (config.LLM_VISION_PAGES).
    Every call — prompt, response, parse, error — is appended to audit_log_path as
    JSONL for transparency.
    """
    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return None

    if pdf_doc is not None:
        fetch_state = "ok"             # Claude sees the file directly via vision
    elif page_text is None:
        page_text, fetch_state = _fetch_page_text(url)
    else:
        fetch_state = "ok"             # caller supplied the content (a local file)
    slack_context = "\n".join(
        f"[{c.get('author', '?')}] {c.get('text', '')}"
        for c in comments[:5]
    )

    prompt = (
        "Extract publication metadata from the information below.\n"
        "Return ONLY valid JSON with these exact keys (use null or [] for unknowns):\n"
        '{"title":null,"authors":[],"abstract":null,"year":null,"journal":null,'
        '"item_type":"journalArticle","doi":null,"volume":null,"issue":null,'
        '"pages":null,"keywords":[],"summary":null,"category":"paper"}\n\n'
        "item_type must be one of: journalArticle, preprint, report, book, "
        "bookSection, conferencePaper, thesis, blogPost, webpage\n"
        "authors should be ['Last, First', ...].\n\n"
        "The page often lists other works too — reference lists, 'related "
        "articles', 'cited by' entries, or recommendations. Extract the "
        "metadata for the ONE central article the page is primarily about: its "
        "main title/heading and author byline, normally at the top of the page "
        "and matching the page title. That central article is the work to "
        "cite. Do NOT return null just because other citations appear lower on "
        "the page — those are context, not the answer.\n\n"
        "summary is an ULTRA-SHORT label — 10 words MAX, a fragment not a "
        "sentence — naming just WHAT the source is or is about, enough to cull "
        "at a glance. Examples: 'NBER working paper on rent control', "
        "'Reporter's X/Twitter profile', 'County eviction dashboard', "
        "'Brookings report on housing vouchers', 'Google Drive folder'. "
        "Describe the source ITSELF only — do NOT mention access or availability "
        "status (no 'paywalled', 'blocked', 'login required', 'content "
        "unavailable', etc.); that's handled separately and is just noise here. "
        "Base it on the page content when available; otherwise infer from the "
        "URL/path and Slack discussion. Always provide it.\n\n"
        "category is a routing judgment, separate from the metadata fields. "
        "Everything here was deliberately shared in a research lab's "
        "paper-sharing channel, so assume good intent. Choose EXACTLY one of:\n"
        "  \"paper\" — the URL is, or points to, a SINGLE directly-citable "
        "work: a research paper, working paper, technical report, journal "
        "article, preprint, conference paper, thesis, or dataset publication "
        "(e.g. sciencedirect.com/article/, jamanetwork.com, springer.com, "
        "arxiv.org), OR a single piece of long-form written content a "
        "researcher would cite: a news/magazine article, op-ed, policy brief, "
        "think-tank or government report, or a substantive analytical blog "
        "post. Use \"paper\" even if you cannot extract the fields above "
        "because the page is paywalled or blocked, as long as the URL/domain/"
        "path indicate one specific such work. A file-hosting link (Google "
        "Drive, Dropbox, OneDrive, etc.) is \"paper\" when it points to a "
        "SINGLE such work — e.g. a hosted PDF of a working paper, article, or "
        "report — even though the file can't be read here; the filename, page "
        "title, or Slack discussion is usually enough to tell.\n"
        "  \"link\" — NOT a single citable work, but a genuine resource worth "
        "keeping in a reference library as a saved webpage: a journal homepage "
        "or issue table-of-contents, an author or organization profile page, a "
        "project/report landing or index page, a tweet or thread, a Google "
        "Drive folder (or a hosted file that is NOT a single citable work — a "
        "dataset, spreadsheet, or slide deck), a dataset portal, or a useful "
        "tool. Anything scholarly or that might point to a useful file is at "
        "least a \"link\" — never \"junk\". When unsure between \"paper\" and "
        "\"link\", choose \"link\".\n"
        "  \"junk\" — clutter with no place in a reference library: a reaction "
        "GIF or image, a dead or parked page, a login screen with nothing "
        "behind it, or a bare site homepage with no specific content. Only "
        "choose \"junk\" when you are confident; if the page content below is "
        "empty you usually CANNOT be confident, so prefer \"paper\" or "
        "\"link\".\n\n"
        f"URL: {url}\n\n"
        f"Slack discussion:\n{slack_context}\n\n"
        + ("The publication is the attached PDF (its first pages)."
           if pdf_doc is not None else f"Page content:\n{page_text}")
    )

    # Text-only by default; for the vision fallback, prepend the PDF as a document
    # block so Claude reads the scanned pages directly.
    if pdf_doc is not None:
        import base64
        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_doc).decode("ascii")}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    model = config.llm_model()
    audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url":       url,
        "model":     model,
        "prompt":    prompt,
        "vision":    pdf_doc is not None,
        "response":  None,
        "parsed":    None,
        "error":     None,
    }

    try:
        client = anthropic_sdk.Anthropic(api_key=anthropic_key)
        messages = [{"role": "user", "content": content}]
        try:
            resp = client.messages.create(model=model, max_tokens=800,
                                           messages=messages)
        except Exception as e:
            # Model retired/deprecated (404) → self-heal: switch to a current model
            # and retry once, so an unmaintained deploy keeps running with no human.
            new_model = (_auto_heal_model(anthropic_key, model)
                         if _is_model_unavailable(e) else None)
            if not new_model:
                raise
            audit["model"] = model = new_model
            audit["model_auto_switched"] = True
            resp = client.messages.create(model=model, max_tokens=800,
                                           messages=messages)
        raw_text = resp.content[0].text
        audit["response"] = raw_text
        # Some models (notably Haiku) emit valid JSON followed by explanatory
        # prose ("Note: ..."). Pull just the first balanced JSON object.
        cleaned = _extract_json_object(raw_text)
        parsed = json.loads(cleaned)
        if not _is_valid_llm_response(parsed):
            audit["error"] = "schema validation failed"
            return None
        audit["parsed"] = parsed
        # Pass through even null-title responses — the cascade interprets
        # "structurally valid + null title" via `category` + whether we actually
        # saw the page, routing to Links / Likely junk / Access issues rather
        # than treating it as an error.
        fields = _llm_to_fields(parsed)
        fields["_fetch_state"] = fetch_state
        # Vision read the actual pages — not thin context, even though there's no
        # page_text. Otherwise flag thin text so review can scrutinise it.
        fields["_low_context"] = (pdf_doc is None
            and len((page_text or "").strip()) < _LOW_CONTEXT_CHARS)
        return fields
    except Exception as e:
        # A 404 reaching here means the configured model was retired AND self-heal
        # couldn't find a replacement (no models API / offline). Flag it distinctly
        # so a wave of failures is diagnosable; otherwise it's a generic API error.
        if _is_model_unavailable(e):
            audit["error"] = (f"model '{model}' unavailable (deprecated/retired) "
                              f"and auto-fallback found no replacement: {e}")
        else:
            audit["error"] = f"{type(e).__name__}: {e}"
        return None
    finally:
        try:
            with open(audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _is_model_unavailable(e: Exception) -> bool:
    """A retired/deprecated/typo'd model surfaces as a 404 NotFoundError."""
    return getattr(e, "status_code", None) == 404 or type(e).__name__ == "NotFoundError"


def _auto_heal_model(anthropic_key: str, current_model: str) -> Optional[str]:
    """The configured model was retired (404). Pick the best currently-available
    replacement from the live Models API and persist it (config.set_llm_model), so
    an unmaintained deploy keeps working with no human in the loop — and the switch
    sticks for every later call and across restarts.

    Preference: newest Haiku-tier (matches today's cheap/fast/vision-capable
    choice), else newest Sonnet, else newest Opus, else the newest Claude model.
    Returns the chosen id, or None if no replacement could be resolved (no models
    API, offline) — in which case the caller surfaces the original 404."""
    try:
        import anthropic as anthropic_sdk
        models = anthropic_sdk.Anthropic(api_key=anthropic_key).models.list(
            limit=1000).data
    except Exception:
        return None
    claude = [m for m in models if str(getattr(m, "id", "")).startswith("claude")]
    if not claude:
        return None
    claude.sort(key=lambda m: str(getattr(m, "created_at", "") or ""), reverse=True)
    chosen = None
    for tier in ("haiku", "sonnet", "opus"):
        chosen = next((m.id for m in claude if tier in m.id and m.id != current_model),
                      None)
        if chosen:
            break
    chosen = chosen or claude[0].id
    if chosen == current_model:
        return None
    config.set_llm_model(chosen)
    return chosen


def _extract_json_object(text: str) -> str:
    """
    Pull the first balanced JSON object out of a string that may contain
    surrounding prose, code-fence markers, or trailing commentary. Walks
    the text tracking brace depth and string state so commentary like
    'Note: ...' after the closing `}` is dropped cleanly.
    """
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    if start == -1:
        return text  # let json.loads fail naturally

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return text  # unbalanced — let json.loads fail


def _is_valid_llm_response(parsed) -> bool:
    """
    Schema-shape check only — does NOT require title to be set. A null-title
    response from a structurally valid call is the LLM's way of saying "I
    couldn't pull a citation"; the cascade then uses `category` and whether we
    saw the page to route it to Links / Likely junk / Access issues, rather than
    treating it as an error.
    """
    if not isinstance(parsed, dict):
        return False
    return _LLM_REQUIRED_KEYS.issubset(parsed.keys())


def _is_useful_partial(out: dict) -> bool:
    """
    Decide whether a title-less LLM result is worth keeping as `metadata_source
    = "llm"` instead of marking the paper a non_paper. The bar is intentionally
    high: weak partials (year-only, journal-only, year+journal-only) are
    closer to noise than to signal — there are thousands of articles in any
    given (journal, year), so they can't uniquely identify a publication.

    A useful partial requires:
      - a DOI (Zotero will resolve it to full metadata on upload), OR
      - authors AND year AND journal all present (manually findable later).
    """
    if out.get("doi"):
        return True
    authors = out.get("authors") or []
    return bool(authors and out.get("year") and out.get("journal"))


def _llm_to_fields(d: dict) -> dict:
    # `category` is a routing hint for the cascade ("paper" | "link" | "junk"),
    # not a Paper field we persist. enrich_paper pops it (and _fetch_state) out
    # before merging.
    return {
        "title":        d.get("title"),
        "authors":      d.get("authors") or [],
        "abstract":     d.get("abstract"),
        "year":         str(d["year"]) if d.get("year") else None,
        "journal":      d.get("journal"),
        "item_type":    d.get("item_type") or "journalArticle",
        "keywords":     d.get("keywords") or [],
        "volume":       d.get("volume"),
        "issue":        d.get("issue"),
        "pages":        d.get("pages"),
        "doi":          d.get("doi"),
        "summary":      d.get("summary"),
        "category":     (d.get("category") or "paper"),
    }


# ── Cascade ───────────────────────────────────────────────────────────────────

def enrich_paper(paper: dict, anthropic_key: Optional[str] = None,
                 audit_log_path: Optional[str] = None) -> dict:
    """
    Run the full cascade for one paper. Returns a dict of fields the caller
    should merge into the Paper. Bookkeeping fields are always set; metadata
    fields are only set when a stage actually resolves them.
    """
    url = paper["url"]
    doi = paper.get("doi")
    audit_log_path = audit_log_path or str(config.LLM_AUDIT_PATH)

    fields: dict = {
        "attempts":            (paper.get("attempts") or 0) + 1,
        "metadata_fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_error":          None,
    }

    # A tweet is just a plain link — never enrich it for its own metadata. The
    # paper a tweet points to is recovered separately (expand_tweets); the tweet
    # itself stays a bare bookmark in the Links tab, so it can't masquerade as a
    # citable work or collide with that paper in the Duplicates tab.
    from url_expander import is_tweet_url
    if is_tweet_url(url):
        fields["metadata_source"] = "link"
        fields["item_type"] = "webpage"
        return fields

    # 0. Slack file upload (the url is a Slack permalink, not a web page). Download
    #    the file via its url_private and read it with the LLM — handled here so it
    #    rides the normal Enrich run, no separate step. Expired token → needs_manual
    #    with the permalink so the lab member can grab it from Slack by hand.
    slack_url = paper.get("slack_file_url")
    if slack_url:
        path = paper.get("local_pdf_path")
        if not path:
            got = save_slack_file(slack_url, url, paper.get("slack_file_name") or "")
            if got is None:
                fields["metadata_source"] = "needs_manual"
                fields["summary"] = f"Slack upload: {paper.get('slack_file_name') or ''}"
                fields["last_error"] = "slack file download failed (token expired?)"
                return fields
            path, size = got
            fields["local_pdf_path"] = path
            fields["pdf_size_bytes"] = size
        if anthropic_key:
            fields.update(enrich_local_file(path, url, paper.get("comments") or [],
                                            anthropic_key, audit_log_path))
        else:
            fields["metadata_source"] = "pdf_saved"
        if fields.get("metadata_source") == "pdf_saved":
            copy_to_upload_queue(path)
        return fields

    # Metadata and the PDF are fetched independently and we keep both outcomes.
    # The best case is full metadata WITH the PDF attached on upload (publisher
    # landing pages often have both citation_* meta tags AND a citation_pdf_url),
    # so the lab member never has to run Zotero's desktop "Retrieve Metadata for
    # PDF" feature (which is unreliable — Zotero's PDF recognizer fails on PDFs
    # whose first-page text or DOI isn't well-indexed by recognize.zotero.org,
    # even when the same paper's browser-connector path would work). save_pdf
    # runs first and regardless of which metadata stage wins, so a CrossRef or
    # Citoid hit still gets the file attached.
    pdf_result = save_pdf(url)
    if pdf_result is not None:
        path, size = pdf_result
        fields["local_pdf_path"] = path
        fields["pdf_size_bytes"] = size

    # 1. CrossRef, if we already have a DOI (from Slack). Authoritative for DOIs,
    #    and keeps the common case off the free, SLA-less Citoid service.
    if doi:
        result = lookup_doi(doi)
        if result:
            source, data = result
            _merge_truthy(fields, data)
            fields["metadata_source"] = source
            return fields

    # 2. Citoid — Wikimedia's hosted Zotero translator — on the URL.
    raw = fetch_translator(url)
    if raw:
        _merge_truthy(fields, translator_to_fields(raw))
        fields["metadata_source"] = "translator"
        return fields

    # PDF saved but NO metadata yet. Read the PDF with the LLM (better than
    # Zotero's recognizer for non-indexed working papers/reports, and the API
    # upload keeps the Slack notes + attaches the PDF). Drag-in ("pdf_saved") is
    # only the fallback — when the PDF has no extractable text (scanned) or there's
    # no API key.
    if pdf_result is not None:
        if anthropic_key:
            fields.update(enrich_local_file(path, url, paper.get("comments") or [],
                                            anthropic_key, audit_log_path))
        else:
            fields["metadata_source"] = "pdf_saved"
        if fields.get("metadata_source") == "pdf_saved":
            copy_to_upload_queue(path)
        return fields

    # 3. LLM fallback
    if anthropic_key:
        out = fetch_llm(url, paper.get("comments") or [],
                        anthropic_key, audit_log_path)
        if out is None:
            # API/parse failure — set error so the paper can be retried later
            fields["last_error"] = "llm: API call failed (see state/llm_audit.jsonl)"
        else:
            # Routing-only hints — never persisted as Paper fields.
            category = out.pop("category", "paper")
            fetch_state = out.pop("_fetch_state", "blocked")

            # The LLM's one-line read + its paper/link/junk guess — persisted
            # everywhere it ran (at-a-glance aids for culling the Links /
            # Likely-junk / Access-issues queues, where we often have no title).
            # Set before the routing branches below.
            if out.get("summary"):
                fields["summary"] = str(out["summary"]).strip()
            fields["llm_category"] = category
            fields["low_context"] = bool(out.pop("_low_context", False))

            # 3a. If the LLM produced a NEW DOI (one we didn't already try
            #     against CrossRef above), route it through the same identifier
            #     lookup before accepting raw LLM data. CrossRef output is more
            #     reliable, and resolving the DOI is a free hallucination check —
            #     a DOI that doesn't resolve there is suspect.
            llm_doi = out.get("doi")
            if llm_doi and llm_doi != doi:
                result = lookup_doi(llm_doi)
                if result:
                    source, data = result
                    _merge_truthy(fields, data)
                    fields["metadata_source"] = source
                    return fields

            # 3b. Citable metadata extracted → "llm" (Ready after review).
            #     A useful partial counts: DOI present (Zotero resolves it at
            #     upload), OR authors+year+journal all present (manually
            #     findable later). Weaker partials fall through to 3c.
            if out.get("title") or _is_useful_partial(out):
                _merge_truthy(fields, out)
                fields["metadata_source"] = "llm"
                return fields

            # 3c. No citable metadata. Sort the residue by what we saw plus the
            #     LLM's category:
            #       saw it (ok) + clutter        → "non_paper"   (Likely junk)
            #       saw it (ok) + worth keeping  → "link"        (Links)
            #       dead (404/410) + non-paper   → "non_paper"   (Likely junk)
            #       everything else              → "needs_manual" (Access issues)
            #     "Everything else" = a citable work we couldn't retrieve, a
            #     blocked page (403/429/5xx/network — we can't tell if it's gone),
            #     or a dead URL the LLM still reads as a paper (might be findable
            #     by hand). A junk/link verdict is only trusted when we saw the
            #     page; a "dead" page is auto-junked ONLY when it isn't a paper,
            #     so a moved-but-real paper is never bulk-deleted.
            if fetch_state == "ok" and category == "junk":
                fields["metadata_source"] = "non_paper"
            elif fetch_state == "ok" and category == "link":
                fields["metadata_source"] = "link"
            elif fetch_state == "dead" and category != "paper":
                fields["metadata_source"] = "non_paper"
            else:
                fields["metadata_source"] = "needs_manual"
            return fields
    else:
        reasons = ["Citoid returned nothing usable"]
        if not doi:
            reasons.append("no DOI to look up via CrossRef")
        reasons.append("no anthropic key for LLM")
        fields["last_error"] = "; ".join(reasons)

    fields["metadata_source"] = "none"
    return fields


def _merge_truthy(target: dict, source: dict) -> None:
    """Copy from source into target, but only fields with a truthy value."""
    for k, v in source.items():
        if v not in (None, "", [], {}):
            target[k] = v


# ── Local file (PDF / .docx) metadata via the LLM ─────────────────────────────
# Preferred over Zotero's desktop recognizer for PDF-only papers: the recognizer
# (recognize.zotero.org) comes back bare for working papers / reports / preprints,
# and the drag-in path it requires can't attach the Slack notes. Reading the
# file's text with the LLM gives better metadata for those AND lets us upload via
# the API (PDF attached + notes). Drag-in (pdf_saved) stays as a fallback.

def _extract_docx_text(path: str) -> str:
    """Pull the body text out of a .docx (stdlib zipfile — no dependency), with
    paragraph breaks preserved. Returns "" if it isn't a readable docx."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)             # paragraphs → newlines
    text = re.sub(r"<[^>]+>", " ", xml)            # strip all XML tags
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _extract_file_text(file_path: str) -> str:
    """Extract text from a local PDF or .docx for the LLM (stdlib only). Returns
    "" when nothing readable comes out (e.g. a scanned/image-only PDF). Capped at
    config.LLM_PAGE_TEXT_CHARS, same as the page-text fetch."""
    if file_path.lower().endswith(".docx"):
        text = _extract_docx_text(file_path)
    else:                                          # PDF (the default)
        try:
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError:
            return ""
        text = _extract_pdf_text(raw)
    return (text or "")[:config.LLM_PAGE_TEXT_CHARS]


def enrich_local_file(file_path: str, url: str, comments: list[Comment],
                      anthropic_key: Optional[str], audit_log_path: str) -> dict:
    """Extract a local file's text (PDF or .docx) and run the LLM extractor on it.
    Returns the fields to merge into the paper:
      - title (or useful partial) found → metadata_source "llm" + the metadata
      - otherwise (no key, unreadable/scanned, or nothing extracted) → "pdf_saved",
        falling back to the manual drag-in path (the file is on disk either way).
    Routing-only hints (category / _fetch_state) are dropped; summary is kept.

    A PDF with no usable text layer (scanned / image-only — under
    _LOW_CONTEXT_CHARS of extracted text) falls back to Claude vision on its first
    config.LLM_VISION_PAGES pages instead of sending the near-empty text."""
    text = _extract_file_text(file_path)
    out = None
    if anthropic_key:
        is_pdf = not file_path.lower().endswith(".docx")
        if is_pdf and len(text.strip()) < _LOW_CONTEXT_CHARS:
            try:
                with open(file_path, "rb") as f:
                    doc = _first_pages_pdf(f.read(), config.LLM_VISION_PAGES)
            except OSError:
                doc = None
            if doc is not None:
                out = fetch_llm(url, comments, anthropic_key, audit_log_path,
                                pdf_doc=doc)
        # Text path: the normal case, and the fallback if vision was skipped or
        # the API call failed but we still have some extracted text to try.
        if out is None and text.strip():
            out = fetch_llm(url, comments, anthropic_key, audit_log_path,
                            page_text=text)
    fields: dict = {}
    if out is not None:
        out.pop("category", None)
        out.pop("_fetch_state", None)
        fields["low_context"] = bool(out.pop("_low_context", False))
        if out.get("title") or _is_useful_partial(out):
            _merge_truthy(fields, out)
            fields["metadata_source"] = "llm"
            return fields
        if out.get("summary"):
            fields["summary"] = out["summary"]
    fields["metadata_source"] = "pdf_saved"        # fall back to manual drag-in
    return fields
