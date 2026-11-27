#!/usr/bin/env bash
# Run this on your own computer if Workday (or another careers site) bot-checks GitHub's servers.
# It reads those sites from your home connection at the same polite pace, then republishes the index.
# Needs: Python 3.11+, `pip install -r requirements.txt`, and the GitHub CLI signed in (`gh auth login`).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
gh release download index --pattern index.db.gz --dir data --clobber
gunzip -f data/index.db.gz
python3 -m jobfeed.datajob crawl --ats "${ATS:-workday}" --minutes "${MINUTES:-120}"
python3 -m jobfeed.datajob finish --out index.db.gz --summary summary.json
gh release upload index index.db.gz summary.json --clobber
echo "Published. The app picks it up on its next daily pull."
