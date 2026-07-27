# Daily Briefing — fact-aware, interest-routed news digest

A once-a-day job (free GitHub Actions) that pulls curated credible outlets, clusters
each story across them (corroboration = your fact-check signal), routes it to one of
four topic categories, summarizes it constrained to the fetched text (anti-hallucination),
caps its own spend, and publishes two things:

- **`feed.xml`** — subscribe to this in an RSS reader (NetNewsWire on iPhone, free).
- **`index.html`** — a mobile web page with collapsible categories and per-article "+"
  expanders. Each article opens to: **title → main point → 3-6 bullets → source links.**

## Categories (topic-based, not source-based)
- **Business** — markets, macro, crypto, FX, oil & gas, metals, startups (incl. Poland), deals.
- **Technology** — AI, chips, cybersecurity, autonomous & space, cloud/infra, big tech.
- **Health** — research findings only; a story enters Health *only* if a vetted research
  outlet (Nature, ScienceDaily, EurekAlert) is in the cluster.
- **Breaking News** — major worldwide stories that match none of the above.

Routing is driven by the `interests` keyword lists in `feeds.yaml` — edit them to tune.

## Source rules
- Global stories need **2+ credible sources** or they're hidden (✅ corroborated).
- The **Poland/EU-startup lane** (Notes from Poland, Sifted, EU-Startups, Silicon Canals)
  may appear single-source, clearly flagged 🟡, since those outlets are pre-vetted.
- State media (China Daily, SCMP) is down-weighted and stays on the strict 2+ track.

## Cost & safety
- ~**$0–$3/month**; **$0** with `llm_provider: none` or inside the Gemini free tier.
- `monthly_cost_cap_usd` is a hard circuit breaker — if hit, AI pauses, digest still ships.
- Never hard-fails: dead feeds are listed in the run report; the AI step falls back to
  outlet text; the API key lives only in GitHub Secrets.

## Setup
1. Public GitHub repo; add all files keeping `.github/workflows/digest.yml` intact.
2. (Optional) add `GEMINI_API_KEY` under Settings → Secrets and variables → Actions.
3. Settings → Pages → Source = GitHub Actions.
4. Actions → run the workflow. Feed: `https://<you>.github.io/<repo>/feed.xml`,
   page: `.../index.html`. Put the Pages URL into `site_url` in `feeds.yaml`.
5. Subscribe to `feed.xml` in NetNewsWire, or just bookmark `index.html`.

## Tuning (all in feeds.yaml)
- `focus_mode`: strict_topics | mostly_topics | even_split | broad
- `headline_slots`: how many Breaking News items to allow
- `hide_single_source`: false = show everything but flag unverified
- `cluster_similarity`: lower merges more aggressively (fewer real stories hidden)
- Add companies/tickers as their own interest for higher priority.

## Honest limits
This is a credibility-filtered, corroboration-scored briefing, not a truth oracle.
Health sourcing + corroboration is the best available proxy for "well-studied," but it
can't guarantee a paper is correct. A live "top companies by market cap" ranking is
reference data, not news — it needs a finance API, not RSS (ask to add it if you want it).

## Markets panel (top companies by market cap)
A compact ranked panel renders at the top of `index.html`, three regions side by side:
US (USD), Europe (EUR), Poland (PLN) — kept single-currency so the ranking is exact
without FX conversion. Configure the watchlists under `markets:` in `feeds.yaml`.

- **provider: yfinance** (default) — no API key, covers US + EU + Warsaw tickers. It's an
  unofficial Yahoo source, so it can occasionally be throttled; the job caches the last-good
  value per ticker and shows it marked `*` if a live fetch fails, and never breaks the digest.
- **provider: finnhub** — more reliable but US-only on the free tier. Add a `FINNHUB_API_KEY`
  secret (Settings → Secrets → Actions) and set `provider: finnhub`.
- **provider: none** — turns the panel off.

Add or remove companies by editing the `symbols` lists (use the Yahoo ticker, e.g. `CDR.WA`
for CD Projekt, `SAP.DE` for SAP). Requires `yfinance` (already in requirements.txt).
