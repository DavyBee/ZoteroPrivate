#!/bin/bash
#
# ============================================================================
#   run.command  —  START the Slack to Zotero app
# ============================================================================
#
#   WHAT THIS DOES
#   Starts everything the app needs, then opens the app in your web browser.
#
#   HOW TO USE IT
#   Double-click this file in Finder. A black Terminal window opens and the app
#   appears in your browser a few seconds later. Leave the Terminal window open
#   while you work — closing it stops the app.
#
#   BEFORE THE FIRST RUN
#   Double-click setup.command once, and fill in your keys in the .env file.
#
#   (Lines that start with "#" are plain-English notes, not commands.)
# ----------------------------------------------------------------------------

# Always work inside the folder this file lives in.
cd "$(dirname "$0")" || exit 1


# ── Check that setup has been done ───────────────────────────────────────────
# The ".venv" workspace and the ".env" settings file are both created by
# setup.command. If either is missing, point the user back to setup.
if [ ! -d .venv ]; then
  echo "It looks like you haven't run setup.command yet. Please double-click setup.command"
  echo "first, then run.command."
  read -r -p "Press Return to end this processs."
  exit 1
fi
if [ ! -f .env ]; then
  echo "No settings file (.env) found. Run setup.command, then fill in your keys."
  read -r -p "Press Return to end this processs."
  exit 1
fi


# ── Start the app ────────────────────────────────────────────────────────────
# No Docker needed anymore: metadata now comes from CrossRef, Wikimedia's hosted
# Citoid service, and Claude — all over plain HTTPS, nothing to run locally.
# Switch on the private workspace, then launch the app. It opens in your
# browser automatically.
echo "Starting the app… your browser will open in a moment."
echo "Keep this window open while you work — close it to stop the app."
source .venv/bin/activate
streamlit run app.py
