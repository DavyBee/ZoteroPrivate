# Zotero Import Tool

Turn a Slack export into well-formed Zotero library entries: ingest the export,
automatically gather bibliographic metadata, review/curate, then upload to
Zotero.

No Docker, no local servers. Metadata comes from three plain-HTTPS sources, in
order: **CrossRef** (for DOIs) → **Citoid** (Wikimedia's hosted Zotero
translator) → **Claude** (AI fallback). The library is stored in a single
**SQLite** file (`zotero_database.db`).

## Need before

1. **Zotero API key** (with write access to the target library)
1. **Anthropic API key** (enables the AI fallback stage; optional but recommended)

There is **nothing else to install** beyond Python, which `setup.command`
handles.

---

## First time setup

**1. Download the project.** Clone this repository:

```
cd Desktop
git clone https://github.com/DavyBee/ZoteroForEvictionLab
```

**2. Run the setup script.** In the cloned folder, double-click
**`setup.command`**. A Terminal window will open and set up everything the app
needs (a private Python workspace, the required packages, and a blank settings
file). When it says **“Setup complete”**, you're done with this step. It may take
a couple of minutes.

> If macOS blocks the script the first time (it might say "unidentified developer"),
> right-click `setup.command` → **Open** → **Open**.

**3. Fill in your keys.** Setup creates a settings file called **`.env`** in the
project folder. Either edit these values directly in a text editor or change them
in the Settings page of the app (usually easier).

| Setting                  | Where to get the values                                                                                      |
| ------------------------ | ---------------------------------------------------------------------------------------------------- |
| `ZOTERO_API_KEY`         | <https://www.zotero.org/settings/keys> → *Create new private key*. Give it **write** access to the library. |
| `ZOTERO_LIBRARY_ID`      | For a **group** library, it's the group's numeric ID in the URL (the numbers in the URL of a shared library). (Default is the Eviction Lab shared library ID already.) |
| `ZOTERO_LIBRARY_TYPE`    | `group` for a shared lab library, or `user` for your personal one. (Default is `group` for the Eviction Lab.) |
| `ZOTERO_COLLECTION_KEY`  | A specific folder to add items to. Leave blank to add to the library root. Found in the URL when viewing that folder on the Zotero web library. |
| `ANTHROPIC_API_KEY`      | <https://console.anthropic.com/> → *API Keys*. Enables the AI fallback stage.                        |
| `LLM_MODEL`              | Leave blank to use the built-in default (a small, fast Claude Haiku model). Selectable from a dropdown of currently available models; may need changing if Anthropic discontinues the current one. |

Make sure to click save. Your keys stay private on your machine.

---

## Running the app

Whenever you want to use the app, double-click **`run.command`**. It launches the
app, which opens automatically in your default web browser. (No Docker, no
servers — metadata is fetched over plain HTTPS.)

**Keep the Terminal window open while you work** — closing it stops the app.

## Closing the app

Just close the Terminal window running the app, and close the browser tab.

---

## Using the app

### 1. Ingest your Slack export

Drop your Slack export `.json` files into the upload area (or pick them from
Finder) and ingest them. The app extracts (where possible), from every message
and its thread replies: shared **links**, **PDF links**, **DOIs**, and the
**Slack author and comments** for context. Files you've already ingested are
skipped automatically.

### 2. Enrich

Click **▶ Enrich all pending** to gather metadata for the pending sources. For
each link the app runs a few steps, stopping at the first that succeeds:

1. **PDF grab** (always attempted) — looks for a downloadable PDF on the page
   (including PDFs linked in the page's HTML) and saves a copy if found, to be
   attached on upload.
1. **CrossRef** — if a DOI was found in the Slack text, looks it up directly
   (authoritative for DOIs). Marked **Ready**.
1. **Citoid** — sends the link to Wikimedia's hosted Zotero translator engine. If
   it recognizes the source, full bibliographic metadata is saved and the item is
   marked **Ready**.
1. **AI fallback (Claude)** — as a last resort, sends a chunk of the page's text
   (or of the PDF, when available) to a small Claude model to infer the title,
   authors, journal, year, and a short summary, and to sort the source into one
   of the review categories below.

Sources where the AI had very little to read (so it guessed from the URL or Slack
context) are flagged with a **⚠** so you know to double-check them.

If any sources error during a run (e.g. a transient Citoid or Anthropic outage,
or a blocked page), they're collected in a **retry-safe** panel at the top of the
Enrich tab — nothing is lost, and re-running Enrich after the service recovers
picks them back up.

> **Tweets:** if the Enrich screen reports tweet links, click **🐦 Expand tweet
> links** to add the URLs *inside* those tweets as new pending papers/links.
> These new links will be enriched separately, while the tweets are treated as
> "expanded tweets" in the review section. When a link inside a tweet can't be
> accessed, nothing changes and the tweet is likely sent to "Links".

### 3. Review and Edit

Open the **Review** screen. It has sub-tabs — **Ready · LLM · Access
issues · Links · Likely junk · Expanded tweets · Duplicates** — that hold the
sources enrichment sorted for you.

There are two ways to interact with the review page:

- The **✏️ Edit table cells** toggle turns the table into an editor, so you can
  fix **Title / Authors / Year / Journal** of any incorrect items (unlikely to
  occur). Edits save to the database automatically as you make them — there's no
  Save button.
- The action row — **✅ Move all to Ready**, **☑ Move selected to Ready**, and
  **🗑 Delete selected** — moves rows into the ready-to-upload tab or removes
  them. (Select rows by clicking their checkboxes first.)

What each tab is for:

- **Ready** — full metadata found, or items manually moved here. Ready for upload
  to Zotero.
- **LLM** — metadata the AI assembled, worth a quick review for weird
  hallucinations (very unlikely). Correct anything with the edit toggle, then
  **Move to Ready** the correct ones.
- **Access issues** — pages we couldn't retrieve (paywalls, bot-blockers, dead or
  empty pages). To handle these, open the link and use the Zotero browser
  extension to add them to the library; if you do, select the source for deletion
  so we don't double-upload. To simply save them as bare links instead, select
  all and **Move to Ready**.
- **Links** — reachable pages that aren't citable papers but are worth keeping
  (tweets, author/organization profiles, journal tables of contents, report
  landing pages, datasets, tools). **Move to Ready** to save them as webpage
  bookmarks, or delete. **Open in browser** to check them first.
- **Likely junk** — probably no place in the library. Worth a check, but usually
  safe to delete; **Move to Ready** anything misclassified.
- **Expanded tweets** — the leftover tweet "wrappers" after expansion. Delete
  them, or **Move to Ready** to keep the tweet itself as a link recording the
  context.
- **Duplicates** — groups of sources that look like the same work (matching DOI
  or title). When you select one, the rest are deleted from the database.

### 4. Upload to Zotero

Click **⬆ Upload all ready** to push every **Ready** item into your Zotero
library. For items that have a saved PDF *and* full metadata, the PDF is attached
automatically. Each item's Slack discussion (the original message plus thread
replies) is uploaded as an attached note. Make sure you have the correct Zotero
API key, group ID, and folder key.

### 5. PDF-only papers (manual drag-in)

Some PDFs can't be read automatically (e.g. scanned/image-only files, or when no
Anthropic key is set). These land in the **PDF only** group and are copied to a
**`pdfs_to_upload/`** folder. Click **📁 Open the default folder** to reveal it,
drag those PDFs into Zotero Desktop (which fills in their metadata), then click
**✓ Confirm … added to Zotero** to clear the queue.

### Viewing the library

The full database is viewable lower on the page, split into **Not uploaded** and
**Uploaded** tabs — so you can see at a glance what's already in Zotero and what
still needs enriching or uploading.

### Backups

The whole library lives in `zotero_database.db`. From the **Settings** tab, use
**⬇️ Download a backup copy (.db)** to save a portable snapshot any time — drop it
back in as `zotero_database.db` to restore. Keep periodic copies (especially when
running on the cloud, where a hosted database is the only data home).

---

## Troubleshooting

- **Nothing uploads / authentication errors.** Re-check `ZOTERO_API_KEY`,
  `ZOTERO_LIBRARY_ID`, and `ZOTERO_LIBRARY_TYPE` in `.env`, and confirm the API
  key has **write** access to that library.
- **The AI fallback never runs.** You likely have no `ANTHROPIC_API_KEY` set —
  the app skips that stage and tells you so.
- **A batch of sources errored during Enrich.** Usually a transient outage of a
  metadata service (Citoid or Anthropic) or a temporarily blocked page. They're
  kept and shown in the retry-safe panel on the Enrich tab; just Enrich again
  once the service recovers.
- **“This is the first time…” when you run the app.** You haven't run
  `setup.command` yet — do that first.

---

## Good to know

- **Upload is one-way** (this app → Zotero). The app never reads back from or
  modifies your existing Zotero library beyond adding the items you upload.
- Your data and keys stay on your Mac. `.env`, the local database
  (`zotero_database.db`), and downloaded PDFs are not shared.
- Re-running `setup.command` any time is safe — it skips what's already done and
  never overwrites your saved keys.
- **Hosting it online:** the same code runs on Streamlit Community Cloud with a
  hosted database (Turso). The operational steps are in `DEPLOY.md`.
- **Upgrading an old install:** if you have a legacy `zotero_database.json` from
  before the SQLite switch, run `python migrate_to_sqlite.py` once to convert it
  (it verifies the result and keeps the JSON as a backup).
