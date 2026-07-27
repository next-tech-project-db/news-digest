#!/usr/bin/env python3
"""
Personal fact-aware news digest.

Pipeline:
  1. Fetch a curated allowlist of RSS sources (credibility is controlled here).
  2. Keep only items inside the lookback window; de-duplicate.
  3. Cluster the same story across outlets  -> corroboration signal (pure code, $0).
  4. Rank clusters (corroboration x recency x source weight); keep top N per category.
  5. Summarize each cluster with an LLM CONSTRAINED to the fetched source text
     (this is the anti-hallucination step: no open-web invention).
  6. Tag each story: corroborated (>=N distinct sources) vs single-source (flagged).
  7. Track token spend against a hard monthly cap (circuit breaker).
  8. Emit an RSS 2.0 feed + an HTML page. Subscribe to the feed in any reader.

Design choices that keep cost + risk low:
  - RSS has no meaningful rate limit; the ONLY metered calls are to the LLM.
  - Clustering/corroboration is lexical, so it needs no embeddings API.
  - If no API key is set (or the cost cap is hit), the job still ships a digest
    using the outlets' own summaries. It NEVER hard-fails on the AI step.
"""

import os
import re
import sys
import json
import html
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
import requests
import feedparser

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.yaml"
OUT_DIR = ROOT / "public"
STATE_PATH = ROOT / "state.json"        # running monthly spend, persisted across runs

STOPWORDS = set("""
the a an and or but of to in on at for from with by as is are was were be been being
this that these those it its into over under after before new says say said report
reports amid over about will has have had how why what when who
""".split())


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("settings", {})
    s = cfg["settings"]
    s.setdefault("lookback_hours", 30)               # 30 = daily, 168 = weekly
    s.setdefault("max_stories_per_category", 6)
    s.setdefault("min_sources_for_corroborated", 2)
    s.setdefault("cluster_similarity", 0.32)
    s.setdefault("monthly_cost_cap_usd", 3.00)
    s.setdefault("llm_provider", "gemini")           # "gemini" | "none"
    s.setdefault("gemini_model", "gemini-flash-lite-latest")
    s.setdefault("price_input_per_mtok", 0.10)
    s.setdefault("price_output_per_mtok", 0.40)
    s.setdefault("site_title", "My Fact-Aware News Digest")
    s.setdefault("site_url", "")                      # your GitHub Pages URL (for the feed)
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"month": "", "spent_usd": 0.0, "requests": 0}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def parse_entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_sources(cfg):
    lookback = timedelta(hours=cfg["settings"]["lookback_hours"])
    cutoff = datetime.now(timezone.utc) - lookback
    items, failures = [], []

    for src in cfg.get("sources", []):
        url = src["url"]
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                failures.append((src["name"], str(parsed.get("bozo_exception", "parse error"))))
                continue
        except Exception as e:                       # a dead feed must never kill the run
            failures.append((src["name"], str(e)))
            continue

        for e in parsed.entries:
            ts = parse_entry_time(e)
            if ts and ts < cutoff:
                continue
            title = strip_html(e.get("title", "")).strip()
            link = e.get("link", "")
            if not title or not link:
                continue
            summary = strip_html(e.get("summary", e.get("description", "")))[:1200]
            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": src["name"],
                "category": src.get("category", "General"),
                "weight": float(src.get("weight", 1.0)),
                "ts": ts.isoformat() if ts else "",
                "ts_obj": ts or datetime.now(timezone.utc),
            })
    # de-duplicate identical links
    seen, deduped = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)
    return deduped, failures


# --------------------------------------------------------------------------- #
# Clustering  ->  corroboration
# --------------------------------------------------------------------------- #
def tokens(text):
    words = re.findall(r"[a-z0-9]{4,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_items(items, threshold):
    for it in items:
        it["_tok"] = tokens(it["title"] + " " + it["summary"])
    clusters = []
    for it in sorted(items, key=lambda x: x["ts_obj"], reverse=True):
        placed = False
        for cl in clusters:
            if jaccard(it["_tok"], cl["_tok"]) >= threshold:
                cl["items"].append(it)
                cl["_tok"] |= it["_tok"]
                placed = True
                break
        if not placed:
            clusters.append({"items": [it], "_tok": set(it["_tok"])})
    return clusters


def rank_and_select(clusters, cfg):
    now = datetime.now(timezone.utc)
    min_src = cfg["settings"]["min_sources_for_corroborated"]
    per_cat = cfg["settings"]["max_stories_per_category"]

    scored = []
    for cl in clusters:
        its = cl["items"]
        sources = sorted({i["source"] for i in its})
        newest = max(i["ts_obj"] for i in its)
        age_h = max((now - newest).total_seconds() / 3600.0, 0.1)
        recency = 1.0 / (1.0 + age_h / 12.0)         # decays over ~half a day
        weight = max(i["weight"] for i in its)
        score = len(sources) * 2.0 + recency + weight
        # category = the one most sources filed it under
        cats = {}
        for i in its:
            cats[i["category"]] = cats.get(i["category"], 0) + 1
        category = max(cats, key=cats.get)
        lead = max(its, key=lambda i: (i["weight"], i["ts_obj"]))
        scored.append({
            "id": hashlib.sha1(lead["link"].encode()).hexdigest()[:12],
            "headline": lead["title"],
            "category": category,
            "sources": sources,
            "corroborated": len(sources) >= min_src,
            "items": its,
            "newest": newest,
            "score": score,
        })

    # keep top N per category, preserving configured category order
    order = cfg.get("categories") or sorted({s["category"] for s in scored})
    selected = []
    for cat in order:
        cat_stories = sorted([s for s in scored if s["category"] == cat],
                             key=lambda s: s["score"], reverse=True)
        selected.extend(cat_stories[:per_cat])
    # any category not in the configured order still gets through
    leftover_cats = {s["category"] for s in scored} - set(order)
    for cat in sorted(leftover_cats):
        cat_stories = sorted([s for s in scored if s["category"] == cat],
                             key=lambda s: s["score"], reverse=True)
        selected.extend(cat_stories[:per_cat])
    return selected


# --------------------------------------------------------------------------- #
# LLM summarization (constrained to fetched text)
# --------------------------------------------------------------------------- #
SYSTEM_INSTRUCTION = (
    "You are a careful news editor. You will be given clusters of real articles "
    "about the same story, each with a headline and the outlet's own summary text. "
    "For EACH cluster, write a concise digest. RULES: Use ONLY facts present in the "
    "provided text. Never add numbers, names, quotes, or claims that are not in the "
    "source text. If the sources conflict, note the disagreement. If the provided "
    "text is too thin to summarize, set summary to the lead headline and leave "
    "highlights empty. Return STRICT JSON only, no markdown."
)


def build_llm_payload(stories):
    clusters = []
    for s in stories:
        srcs = []
        for it in s["items"][:6]:                    # cap sources per story to bound tokens
            srcs.append({"outlet": it["source"], "headline": it["title"],
                         "text": it["summary"][:600]})
        clusters.append({"id": s["id"], "sources": srcs})
    instruction = (
        SYSTEM_INSTRUCTION
        + "\n\nReturn a JSON object: {\"stories\":[{\"id\":str,\"summary\":str "
        "(2-3 sentences),\"highlights\":[str,str,...] (2-4 key points),"
        "\"side_notes\":[str] (0-2 secondary details)}]}\n\nCLUSTERS:\n"
        + json.dumps(clusters, ensure_ascii=False)
    )
    return instruction


def call_gemini(prompt, cfg):
    """Returns (parsed_json_or_None, input_tokens, output_tokens)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None, 0, 0
    model = cfg["settings"]["gemini_model"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[llm] request failed, falling back to raw summaries: {e}", file=sys.stderr)
        return None, 0, 0

    usage = data.get("usageMetadata", {})
    in_tok = usage.get("promptTokenCount", 0)
    out_tok = usage.get("candidatesTokenCount", 0)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return parsed, in_tok, out_tok
    except Exception as e:
        print(f"[llm] could not parse model JSON, using raw summaries: {e}", file=sys.stderr)
        return None, in_tok, out_tok


def summarize(stories, cfg, state):
    """Attaches summary/highlights/side_notes to each story. Cost-capped."""
    provider = cfg["settings"]["llm_provider"]
    cap = cfg["settings"]["monthly_cost_cap_usd"]
    over_budget = provider == "none" or state["spent_usd"] >= cap

    ai_result = {}
    cost_this_run = 0.0
    if not over_budget and stories:
        prompt = build_llm_payload(stories)
        parsed, in_tok, out_tok = call_gemini(prompt, cfg)
        cost_this_run = (in_tok / 1e6) * cfg["settings"]["price_input_per_mtok"] \
                      + (out_tok / 1e6) * cfg["settings"]["price_output_per_mtok"]
        state["spent_usd"] = round(state["spent_usd"] + cost_this_run, 6)
        state["requests"] = state.get("requests", 0) + 1
        if parsed and isinstance(parsed.get("stories"), list):
            ai_result = {x.get("id"): x for x in parsed["stories"] if x.get("id")}

    for s in stories:
        ai = ai_result.get(s["id"])
        if ai:
            s["summary"] = ai.get("summary") or s["headline"]
            s["highlights"] = [h for h in ai.get("highlights", []) if h][:4]
            s["side_notes"] = [n for n in ai.get("side_notes", []) if n][:2]
            s["ai"] = True
        else:                                        # graceful fallback: outlet's own text
            lead = max(s["items"], key=lambda i: (i["weight"], i["ts_obj"]))
            s["summary"] = lead["summary"] or s["headline"]
            s["highlights"] = []
            s["side_notes"] = []
            s["ai"] = False

    report = {
        "over_budget": state["spent_usd"] >= cap,     # true only if the CAP is the reason
        "cost_this_run": round(cost_this_run, 6),
        "month_spent": round(state["spent_usd"], 4),
        "cap": cap,
        "ai_used": any(s["ai"] for s in stories),
    }
    return report


# --------------------------------------------------------------------------- #
# Output: RSS + HTML
# --------------------------------------------------------------------------- #
def rss_datetime(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def esc(t):
    return html.escape(t or "", quote=True)


def story_body_html(s):
    tag = ("✅ Corroborated · " + ", ".join(s["sources"])
           if s["corroborated"]
           else "⚠️ Single source · " + s["sources"][0])
    parts = [f"<p><em>{esc(tag)}</em></p>", f"<p>{esc(s['summary'])}</p>"]
    if s["highlights"]:
        parts.append("<p><strong>Highlights</strong></p><ul>"
                     + "".join(f"<li>{esc(h)}</li>" for h in s["highlights"]) + "</ul>")
    if s["side_notes"]:
        parts.append("<p><strong>Also worth noting</strong></p><ul>"
                     + "".join(f"<li>{esc(n)}</li>" for n in s["side_notes"]) + "</ul>")
    links = "".join(
        f'<li><a href="{esc(i["link"])}">{esc(i["source"])}: {esc(i["title"])}</a></li>'
        for i in s["items"][:6])
    parts.append(f"<p><strong>Read the sources</strong></p><ul>{links}</ul>")
    if not s["ai"]:
        parts.append("<p><small>Summary shown as published by the outlet "
                     "(AI summary skipped this run).</small></p>")
    return "".join(parts)


def build_rss(stories, cfg, report):
    s = cfg["settings"]
    now = datetime.now(timezone.utc)
    items_xml = []

    # a small run-report item so you can see spend + corroboration at a glance
    corr = sum(1 for x in stories if x["corroborated"])
    report_desc = (
        f"<p>{len(stories)} stories · {corr} corroborated · "
        f"{len(stories) - corr} single-source.</p>"
        f"<p>Spend this month: ${report['month_spent']:.4f} of ${report['cap']:.2f} cap"
        + (" — <strong>cap reached, AI paused</strong>." if report["over_budget"]
           else ".") + "</p>"
        f"<p>AI summaries: {'on' if report['ai_used'] else 'off (fallback to outlet text)'}.</p>"
    )
    items_xml.append(f"""    <item>
      <title>🧾 Digest run report — {now:%Y-%m-%d}</title>
      <link>{esc(s['site_url'] or 'https://example.com')}</link>
      <guid isPermaLink="false">report-{now:%Y%m%d}</guid>
      <pubDate>{rss_datetime(now)}</pubDate>
      <description>{esc(report_desc)}</description>
    </item>""")

    for st in stories:
        title = f"[{st['category']}] {st['headline']}"
        body = story_body_html(st)
        items_xml.append(f"""    <item>
      <title>{esc(title)}</title>
      <link>{esc(st['items'][0]['link'])}</link>
      <guid isPermaLink="false">{st['id']}</guid>
      <category>{esc(st['category'])}</category>
      <pubDate>{rss_datetime(st['newest'])}</pubDate>
      <description>{esc(body)}</description>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>{esc(s['site_title'])}</title>
  <link>{esc(s['site_url'] or 'https://example.com')}</link>
  <description>Curated, corroboration-scored daily digest.</description>
  <lastBuildDate>{rss_datetime(now)}</lastBuildDate>
{chr(10).join(items_xml)}
</channel></rss>
"""
    return feed


def build_html(stories, cfg, report, failures):
    s = cfg["settings"]
    now = datetime.now(timezone.utc)
    by_cat = {}
    for st in stories:
        by_cat.setdefault(st["category"], []).append(st)
    order = cfg.get("categories") or list(by_cat.keys())
    order = [c for c in order if c in by_cat] + [c for c in by_cat if c not in order]

    blocks = []
    for cat in order:
        cards = []
        for st in by_cat[cat]:
            badge = ('<span class="ok">✅ corroborated</span>' if st["corroborated"]
                     else '<span class="warn">⚠️ single source</span>')
            hl = ("<ul>" + "".join(f"<li>{esc(h)}</li>" for h in st["highlights"]) + "</ul>"
                  if st["highlights"] else "")
            srcs = " · ".join(esc(x) for x in st["sources"])
            cards.append(
                f'<article><h3>{esc(st["headline"])} {badge}</h3>'
                f'<p>{esc(st["summary"])}</p>{hl}'
                f'<p class="src">{srcs}</p></article>')
        blocks.append(f'<section><h2>{esc(cat)}</h2>{"".join(cards)}</section>')

    fail_html = ""
    if failures:
        fl = "".join(f"<li>{esc(n)} — {esc(m)}</li>" for n, m in failures)
        fail_html = f'<section class="fail"><h2>Feeds that failed this run</h2><ul>{fl}</ul></section>'

    cap_note = ("cap reached — AI paused" if report["over_budget"]
                else f"AI {'on' if report['ai_used'] else 'off'}")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(s['site_title'])}</title>
<style>
 body{{font:16px/1.55 -apple-system,system-ui,sans-serif;max-width:44rem;margin:0 auto;padding:1.2rem;color:#1a1a1a}}
 h1{{font-size:1.5rem;margin:.2rem 0}} .meta{{color:#666;font-size:.85rem;margin-bottom:1.5rem}}
 h2{{border-bottom:2px solid #eee;padding-bottom:.2rem;margin-top:2rem}}
 article{{margin:1rem 0;padding-bottom:1rem;border-bottom:1px solid #f0f0f0}}
 h3{{font-size:1.05rem;margin:.2rem 0}} .src{{color:#888;font-size:.8rem}}
 .ok{{color:#0a7d2c;font-size:.7rem;font-weight:600}} .warn{{color:#b26a00;font-size:.7rem;font-weight:600}}
 .fail{{color:#a00;font-size:.85rem}} ul{{margin:.3rem 0}}
</style></head><body>
<h1>{esc(s['site_title'])}</h1>
<p class="meta">{now:%A, %d %B %Y · %H:%M UTC} · {len(stories)} stories · spend ${report['month_spent']:.4f}/${report['cap']:.2f} · {cap_note}</p>
{''.join(blocks)}
{fail_html}
</body></html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    state = load_state()
    if state.get("month") != current_month():        # reset spend at month rollover
        state = {"month": current_month(), "spent_usd": 0.0, "requests": 0}

    items, failures = fetch_sources(cfg)
    print(f"[fetch] {len(items)} items, {len(failures)} feed failures")

    clusters = cluster_items(items, cfg["settings"]["cluster_similarity"])
    stories = rank_and_select(clusters, cfg)
    print(f"[cluster] {len(clusters)} clusters -> {len(stories)} selected stories")

    report = summarize(stories, cfg, state)
    save_state(state)
    print(f"[cost] this run ${report['cost_this_run']:.5f} · "
          f"month ${report['month_spent']:.4f}/${report['cap']:.2f} · "
          f"ai_used={report['ai_used']}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "feed.xml").write_text(build_rss(stories, cfg, report), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(build_html(stories, cfg, report, failures), encoding="utf-8")
    print(f"[out] wrote {OUT_DIR/'feed.xml'} and {OUT_DIR/'index.html'}")


if __name__ == "__main__":
    main()
