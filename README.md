# jobfeed-data

A daily, public job index built by GitHub Actions:

- every open role at about 16,000 companies on Greenhouse, Lever and Ashby (their public job-board APIs,
  Lever paced at one request a second), plus companies found through Common Crawl and Hacker News
- careers sites on Workday (~12,000), iCIMS (~6,900), SuccessFactors, Taleo and Oracle Recruiting, read the way
  search engines read them: the public sitemap plus each job page's schema.org JobPosting markup, honoring
  robots.txt, one request a second per site, at most 25,000 new job pages a day (US first)
- Workable's public job feed (US) and Hacker News "Who is hiring?" (USAJOBS is left out on purpose: its terms
  don't allow republishing, so the app fetches it privately with its own key)
- Department of Labor H-1B filings, refreshed when a new quarter is published
- per-posting history (first posted, reposts) and per-company hiring momentum

Each morning the workflow restores yesterday's index from the `index` release, updates it, and publishes
`index.db.gz` (SQLite) and `summary.json` to the same release. The jobfeed app downloads it daily when
`INDEX_URL` points at:

    https://github.com/<owner>/jobfeed-data/releases/download/index/index.db.gz

If a careers site answers GitHub's servers with a bot check, it's skipped (never forced) and shows as failing
in `summary.json`. Run `./home_run.sh` on your own computer to read those sites from a home connection and
republish; the next daily run carries on from there.

Contains only public job postings and public government data. Company board list: MIT-licensed, from
github.com/Riley-Dorrington/job-board-aggregator (see `jobfeed/directory/LICENSE-company-lists.txt`).
