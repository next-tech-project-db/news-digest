# Fact-aware news digest → RSS

A once-a-day job that pulls a curated list of credible outlets, clusters the same
story across them (that's the corroboration/fact-check signal), summarizes each one
using **only** the fetched text (so the AI can't invent facts), tags credibility,
caps its own spend, and publishes an **RSS feed you subscribe to in any reader**
(e.g. NetNewsWire on iPhone — free).

Cost at a typical ~30-article day: roughly **$0–$3/month**, and **$0** if you leave
the AI off or stay inside the Gemini free tier.

## How it protects against the things you asked about

- **"Shady news":** only sources in `feeds.yaml` are ever pulled. Curation is the filter.
- **Corroboration:** a story confirmed by ≥2 distinct outlets is tagged ✅; a lone
  report is tagged ⚠️ single-source. This is the reliable, honest form of fact-checking.
- **Hallucination:** the model is told to use only the provided article text and to
  flag conflicts. A second-pass verification via web search is an optional add-on.
- **Rate limits / "fatigue":** RSS has no real limit; the only metered call is one
  batched LLM request per run. The run report shows spend + how much cap is left.
- **Expenses:** `monthly_cost_cap_usd` is a hard circuit breaker. If hit, the AI step
  pauses and the digest still ships using outlets' own summaries. Never a surprise bill.
- **No infra to break:** GitHub Actions runs it; GitHub Pages hosts it. Nothing to maintain.

## Setup (about 10 minutes, $0)

1. **Create a GitHub repo** and drop these files in (keep the folder structure).
2. **(Optional) AI summaries:** get a Gemini API key from Google AI Studio, then in
   the repo go to *Settings → Secrets and variables → Actions → New repository secret*,
   name it `GEMINI_API_KEY`. Skip this to run at $0 (you'll get outlet summaries instead).
3. **Enable Pages:** *Settings → Pages → Build and deployment → Source = GitHub Actions.*
4. **First run:** *Actions* tab → `news-digest` → *Run workflow*. After it finishes, your
   feed is at `https://<you>.github.io/<repo>/feed.xml` and a readable page at `.../index.html`.
5. **Put that Pages URL** into `site_url` in `feeds.yaml` and commit (makes feed links absolute).
6. **Subscribe on your phone:** open NetNewsWire → add feed → paste the `feed.xml` URL.

It then updates itself every morning at 06:00 UTC. Change the time/frequency in
`.github/workflows/digest.yml` (the `cron` line). For a **weekly** digest, also set
`lookback_hours: 168` in `feeds.yaml`.

## Tuning

Everything lives in `feeds.yaml`:
- Add/remove outlets under `sources` (verify each URL resolves — the run report lists
  any feed that failed).
- `cluster_similarity` — lower groups more stories together, higher is stricter.
- `max_stories_per_category`, `monthly_cost_cap_usd`, and the model/price fields.

## Run it locally to test

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...     # optional
python news_digest.py         # writes public/feed.xml and public/index.html
```

## Honest limitations

This is a **credibility-filtered, corroboration-scored digest**, not a truth oracle.
It can't adjudicate whether a claim is objectively true — no automated system reliably
can. It reduces bad information by (1) restricting sources, (2) surfacing corroboration,
and (3) constraining summaries to source text. Treat ⚠️ single-source items with the
skepticism the tag implies.
