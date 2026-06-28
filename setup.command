#!/bin/bash
#
# ============================================================================
#   setup.command  —  ONE-TIME setup for the Slack to Zotero app
# ============================================================================
#
#   WHAT THIS DOES
#   Gets your Mac ready to run the app. It installs the pieces the app needs
#   (a private Python "workspace" plus some software packages) and creates a
#   blank settings file for your keys. You only need to do this ONCE.
#
#   HOW TO USE IT
#   Double-click this file in Finder. A black Terminal window opens and shows
#   the progress. When it says "Setup complete", you're done.
#
#   GOOD TO KNOW
#   It is safe to run again any time. It skips anything that's already done,
#   and it never overwrites the keys you've saved.
#
#   READING THE STEPS BELOW  (just for the curious — you don't need to)
#   Lines that start with "#" are plain-English notes, not commands.
#   A few bits of shorthand show up again and again:
#       >/dev/null 2>&1   means "do this quietly, hide the technical chatter"
#       command -v NAME   means "is NAME installed on this Mac?"
#       exit 1            means "stop here, something needs your attention"
# ----------------------------------------------------------------------------

# Always work inside the folder this file lives in, no matter where it was
# double-clicked from.
cd "$(dirname "$0")" || exit 1

echo "─────────────────────────────────────────────"
echo " Slack→Zotero — one-time setup"
echo " Folder: $(pwd)"
echo "─────────────────────────────────────────────"
echo


# ── Step 1 of 4: Apple's developer tools ─────────────────────────────────────
# These come free from Apple and include "git" and "python3", which the app
# needs. If they're missing, we ask macOS to install them, then stop so you
# can let that finish before continuing.
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Installing Apple Command Line Tools (this gives you git + python3)."
  echo "A system dialog will open — click \"Install\", wait for it to finish,"
  echo "then double-click setup.command again."
  xcode-select --install 2>/dev/null
  echo
  read -r -p "Press Return to close this window."
  exit 1
fi


# ── Step 2 of 4: Python ──────────────────────────────────────────────────────
# The app is written in Python. Step 1 normally provides it; here we simply
# double-check it's really there before going on.
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 still not found. Install Python from"
  echo "       https://www.python.org/downloads/  then re-run setup.command."
  read -r -p "Press Return to close this window."
  exit 1
fi
echo "Using $(python3 --version)"
echo


# ── Step 3 of 4: the app's private workspace and its packages ────────────────
# We create a private Python workspace in a hidden folder called ".venv".
# Keeping the app's packages in here means they can't clash with anything else
# on your Mac. Then we download the packages the app needs into it.
if [ ! -d .venv ]; then
  echo "Creating the app's private workspace (.venv)…"
  if ! python3 -m venv .venv; then
    echo "ERROR: couldn't create the workspace."
    read -r -p "Press Return to close this window."
    exit 1
  fi
fi

echo "Installing the software packages the app needs (can take a couple of minutes)…"
source .venv/bin/activate                 # switch on the private workspace
python -m pip install --upgrade pip >/dev/null
if ! python -m pip install -r requirements.txt; then
  echo "ERROR: installing the packages failed (see the messages above)."
  read -r -p "Press Return to close this window."
  exit 1
fi
echo


# ── Step 4 of 4: your settings file (.env) ───────────────────────────────────
# The app reads your Zotero and Anthropic keys from a file called ".env".
# If you don't have one yet, we copy the blank template so you can fill it in.
# If you already have one, we leave it completely untouched.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created a blank settings file (.env). Open it and fill in your keys:"
  echo "   $(pwd)/.env"
  echo
fi


# No Docker step anymore: metadata now comes from CrossRef, Wikimedia's hosted
# Citoid service, and Claude — all over plain HTTPS, with nothing to install or
# run locally.


# ── All done ─────────────────────────────────────────────────────────────────
echo
echo "✅ Setup complete."
echo "   1. Double-click run.command to start the app."
