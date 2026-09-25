#!/usr/bin/env python3
"""
Daily Briefing — personal, fact-aware, interest-routed news digest.

Flow: fetch curated RSS -> window+dedupe -> cluster across outlets (corroboration)
-> route each story to a category by matched interests -> gate/verify
-> summarize (constrained to fetched text) -> cost-capped -> emit RSS + collapsible HTML.

Categories are TOPIC-based (Business / Technology / Health), driven by interest
keywords. Anything major that matches none goes to Breaking News (headline reserve).
Health is gated: a story only enters Health if the cluster includes a research outlet.

PV Tube (top of the page): photovoltaics / energy storage / inverter news from trade press,
split EU / Asia / US, configured in the `pv_tube:` block of feeds.yaml. See the PV TUBE
section below. Run `python news_digest.py --validate-pv` to check the PV feed URLs (no AI cost).
"""
from __future__ import annotations

import os, re, sys, json, html, hashlib, math, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml, requests, feedparser

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.yaml"
OUT_DIR = ROOT / "public"
STATE_PATH = ROOT / "state.json"

STOPWORDS = set("""
the a an and or but of to in on at for from with by as is are was were be been being
this that these those it its into over under after before new says say said report
reports amid about will has have had how why what when who
""".split())


# --------------------------------------------------------------------------- #
def load_config():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
    s = cfg.setdefault("settings", {})
    for k, v in {"lookback_hours": 30, "max_stories_per_category": 8,
                 "min_sources_for_corroborated": 2, "hide_single_source": True,
                 "focus_mode": "mostly_topics", "headline_slots": 6,
                 "interest_boost": 3.0, "cluster_similarity": 0.28,
                 "monthly_cost_cap_usd": 3.00, "llm_provider": "gemini",
                 "gemini_model": "gemini-flash-lite-latest",
                 "price_input_per_mtok": 0.10, "price_output_per_mtok": 0.40,
                 "site_title": "Daily Briefing", "site_url": ""}.items():
        s.setdefault(k, v)
    cfg.setdefault("interests", [])
    cfg.setdefault("categories", ["Breaking News", "Business", "Technology", "Health"])
    cfg.setdefault("markets", {"enabled": False, "provider": "none", "regions": []})
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
def parse_entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def strip_html(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if len(p.strip()) > 1]


def fetch_sources(cfg):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["settings"]["lookback_hours"])
    items, failures = [], []
    for src in cfg.get("sources", []):
        try:
            parsed = feedparser.parse(src["url"])
            if parsed.bozo and not parsed.entries:
                failures.append((src["name"], str(parsed.get("bozo_exception", "parse error"))))
                continue
        except Exception as e:
            failures.append((src["name"], str(e)))
            continue
        for e in parsed.entries:
            ts = parse_entry_time(e)
            if ts and ts < cutoff:
                continue
            title = strip_html(e.get("title", ""))
            link = e.get("link", "")
            if not title or not link:
                continue
            items.append({
                "title": title, "link": link,
                "summary": strip_html(e.get("summary", e.get("description", "")))[:1200],
                "source": src["name"], "weight": float(src.get("weight", 1.0)),
                "trust_single": bool(src.get("trust_single", False)),
                "research": bool(src.get("research", False)),
                "ts_obj": ts or datetime.now(timezone.utc),
            })
    seen, deduped = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"]); deduped.append(it)
    return deduped, failures


# --------------------------------------------------------------------------- #
def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in STOPWORDS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def cluster_items(items, threshold):
    for it in items:
        it["_tok"] = tokens(it["title"] + " " + it["summary"])
    clusters = []
    for it in sorted(items, key=lambda x: x["ts_obj"], reverse=True):
        for cl in clusters:
            if jaccard(it["_tok"], cl["_tok"]) >= threshold:
                cl["items"].append(it); cl["_tok"] |= it["_tok"]; break
        else:
            clusters.append({"items": [it], "_tok": set(it["_tok"])})
    return clusters


def route(text, interests, has_research):
    """Return (category|None, matched_names, interest_score). None => drop."""
    t = " " + text.lower() + " "
    scored, matched, total = [], [], 0.0
    for topic in interests:
        hits = sum(1 for kw in topic.get("keywords", []) if kw.lower() in t)
        if hits:
            sc = hits * float(topic.get("weight", 1.0))
            scored.append((sc, topic["category"]))
            matched.append(topic["name"]); total += sc
    if not scored:
        return "Breaking News", matched, 0.0        # no topic -> general headline
    for sc, cat in sorted(scored, key=lambda x: x[0], reverse=True):
        if cat == "Health" and not has_research:
            continue                                # Health needs a research outlet
        return cat, matched, total
    return None, matched, total                     # matched only ungated Health -> drop


def rank_and_select(clusters, cfg):
    s = cfg["settings"]
    now = datetime.now(timezone.utc)
    min_src = s["min_sources_for_corroborated"]
    hide_single = s["hide_single_source"]
    interests = cfg.get("interests", []) or []
    boost = float(s["interest_boost"])

    stories = []
    for cl in clusters:
        its = cl["items"]
        sources = sorted({i["source"] for i in its})
        corroborated = len(sources) >= min_src
        trusted_single = (len(sources) == 1) and any(i["trust_single"] for i in its)
        if not corroborated and not trusted_single and hide_single:
            continue
        has_research = any(i["research"] for i in its)
        text = " ".join(i["title"] + " " + i["summary"] for i in its)
        category, matched, iscore = route(text, interests, has_research)
        if category is None:
            continue
        newest = max(i["ts_obj"] for i in its)
        age_h = max((now - newest).total_seconds() / 3600.0, 0.1)
        recency = 1.0 / (1.0 + age_h / 12.0)
        weight = max(i["weight"] for i in its)
        lead = max(its, key=lambda i: (i["weight"], i["ts_obj"]))
        corr = "corroborated" if corroborated else ("trusted_single" if trusted_single else "single")
        stories.append({
            "id": hashlib.sha1(lead["link"].encode()).hexdigest()[:12],
            "headline": lead["title"], "category": category, "sources": sources,
            "corr": corr, "items": its, "newest": newest,
            "on_topic": category != "Breaking News", "matched": matched,
            "score": len(sources) * 2.0 + recency + weight + boost * iscore,
        })

    on = [x for x in stories if x["on_topic"]]
    off = sorted([x for x in stories if not x["on_topic"]], key=lambda x: x["score"], reverse=True)
    mode, slots = s["focus_mode"], int(s["headline_slots"])
    if mode == "strict_topics":
        pool = on
    elif mode == "mostly_topics":
        pool = on + off[:slots]
    elif mode == "even_split":
        pool = on + off[:max(slots, len(on))]
    else:
        pool = stories

    per_cat = s["max_stories_per_category"]
    order = (cfg.get("categories") or []) + \
            [c for c in {x["category"] for x in pool} if c not in (cfg.get("categories") or [])]
    selected = []
    for cat in order:
        cat_stories = sorted([x for x in pool if x["category"] == cat],
                             key=lambda x: x["score"], reverse=True)
        selected.extend(cat_stories[:per_cat])
    return selected


# --------------------------------------------------------------------------- #
SYSTEM_INSTRUCTION = (
    "You are a careful news editor. For each cluster of real articles about the same story "
    "(headline + the outlet's own summary), produce: (1) main_point — ONE sentence stating "
    "the single most important takeaway; (2) highlights — 3 to 6 short bullet points covering "
    "the key concepts (more only if truly needed). RULES: use ONLY facts in the provided text; "
    "never add numbers, names, quotes, or claims not present; if sources conflict, say so; if "
    "the text is too thin, set main_point to the lead headline and highlights to []. STRICT JSON only."
)


def build_llm_payload(stories):
    clusters = [{"id": x["id"], "sources": [
        {"outlet": i["source"], "headline": i["title"], "text": i["summary"][:600]}
        for i in x["items"][:6]]} for x in stories]
    return (SYSTEM_INSTRUCTION + '\n\nReturn {"stories":[{"id":str,"main_point":str,'
            '"highlights":[str,...]}]}\n\nCLUSTERS:\n' + json.dumps(clusters, ensure_ascii=False))


def call_gemini(prompt, cfg):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None, 0, 0
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{cfg['settings']['gemini_model']}:generateContent")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    if cfg["settings"]["gemini_model"].startswith("gemini-3"):
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "minimal"}
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[llm] request failed, using raw summaries: {e}", file=sys.stderr)
        return None, 0, 0
    u = data.get("usageMetadata", {})
    out_tok = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)   # thinking is billed too
    try:
        parts = data["candidates"][0]["content"]["parts"]
        parsed = json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
        return parsed, u.get("promptTokenCount", 0), out_tok
    except Exception as e:
        print(f"[llm] bad JSON, using raw summaries: {e}", file=sys.stderr)
        return None, u.get("promptTokenCount", 0), out_tok


def summarize(stories, cfg, state):
    cap = cfg["settings"]["monthly_cost_cap_usd"]
    off_budget = cfg["settings"]["llm_provider"] == "none" or state["spent_usd"] >= cap
    ai, cost = {}, 0.0
    if not off_budget and stories:
        parsed, itok, otok = call_gemini(build_llm_payload(stories), cfg)
        cost = itok / 1e6 * cfg["settings"]["price_input_per_mtok"] + \
               otok / 1e6 * cfg["settings"]["price_output_per_mtok"]
        state["spent_usd"] = round(state["spent_usd"] + cost, 6)
        state["requests"] = state.get("requests", 0) + 1
        if parsed and isinstance(parsed.get("stories"), list):
            ai = {x.get("id"): x for x in parsed["stories"] if x.get("id")}
    for st in stories:
        a = ai.get(st["id"])
        if a:
            st["main_point"] = a.get("main_point") or st["headline"]
            st["highlights"] = [h for h in a.get("highlights", []) if h][:7]
            st["ai"] = True
        else:                                        # fallback: split the outlet's own text
            lead = max(st["items"], key=lambda i: (i["weight"], i["ts_obj"]))
            sents = sentences(lead["summary"])
            st["main_point"] = sents[0] if sents else st["headline"]
            st["highlights"] = sents[1:6]
            st["ai"] = False
    return {"over_budget": state["spent_usd"] >= cap, "cost_this_run": round(cost, 6),
            "month_spent": round(state["spent_usd"], 4), "cap": cap,
            "ai_used": any(s["ai"] for s in stories)}


# --------------------------------------------------------------------------- #
def esc(t):
    return html.escape(t or "", quote=True)


def corr_label(st):
    return {"corroborated": "corroborated · " + ", ".join(st["sources"]),
            "trusted_single": "trusted single source · " + st["sources"][0],
            "single": "single source · " + st["sources"][0]}[st["corr"]]


def rss_dt(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ---- RSS item: Title (item title) > main point > bullets > sources ----
def story_body_html(st):
    p = [f"<p><em>{esc(corr_label(st))}</em></p>",
         f"<p><strong>{esc(st['main_point'])}</strong></p>"]
    if st["highlights"]:
        p.append("<ul>" + "".join(f"<li>{esc(h)}</li>" for h in st["highlights"]) + "</ul>")
    if st["matched"]:
        p.append(f"<p><small>Topics: {esc(', '.join(st['matched']))}</small></p>")
    p.append("<p><strong>Sources:</strong> " + " · ".join(
        f'<a href="{esc(i["link"])}">{esc(i["source"])}</a>' for i in st["items"][:6]) + "</p>")
    if not st["ai"]:
        p.append("<p><small>Outlet's own text (AI skipped this run).</small></p>")
    return "".join(p)


def build_rss(stories, cfg, report, markets=None):
    s, now = cfg["settings"], datetime.now(timezone.utc)
    corr = sum(1 for x in stories if x["corr"] == "corroborated")
    ts = sum(1 for x in stories if x["corr"] == "trusted_single")
    rep = (f"<p>{len(stories)} stories · {corr} corroborated · {ts} trusted single-source.</p>"
           f"<p>Spend this month: ${report['month_spent']:.4f} of ${report['cap']:.2f}"
           + (" — <strong>cap reached, AI paused</strong>." if report["over_budget"] else ".") + "</p>"
           f"<p>AI summaries: {'on' if report['ai_used'] else 'off (outlet text)'}.</p>")
    if report.get("pv_line"):
        rep += f"<p>{esc(report['pv_line'])}</p>"
    if markets:
        led = "; ".join(f"{r['name']}: {esc(r['rows'][0]['name'])} {esc(fmt_cap(r['rows'][0]['cap'], r['cur']))}"
                        for r in markets)
        rep += f"<p>Market cap leaders — {led}</p>"
    items = [f"""    <item>
      <title>Digest run report — {now:%Y-%m-%d}</title>
      <link>{esc(s['site_url'] or 'https://example.com')}</link>
      <guid isPermaLink="false">report-{now:%Y%m%d}</guid>
      <pubDate>{rss_dt(now)}</pubDate>
      <description>{esc(rep)}</description>
    </item>"""]
    for st in stories:
        items.append(f"""    <item>
      <title>{esc('[' + st['category'] + '] ' + st['headline'])}</title>
      <link>{esc(st['items'][0]['link'])}</link>
      <guid isPermaLink="false">{st['id']}</guid>
      <category>{esc(st['category'])}</category>
      <pubDate>{rss_dt(st['newest'])}</pubDate>
      <description>{esc(story_body_html(st))}</description>
    </item>""")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>\n'
            f"  <title>{esc(s['site_title'])}</title>\n"
            f"  <link>{esc(s['site_url'] or 'https://example.com')}</link>\n"
            "  <description>Personalized, corroboration-scored briefing.</description>\n"
            f"  <lastBuildDate>{rss_dt(now)}</lastBuildDate>\n"
            + "\n".join(items) + "\n</channel></rss>\n")


# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#F7F8FA;--ink:#14181F;--muted:#6B7280;--line:#E3E6EB;
--ok:#1F7A4D;--tw:#9A6A00;--warn:#B45309;--accent:#303A66;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-text-size-adjust:100%}
header,main{max-width:46rem;margin:0 auto;padding:0 1.1rem}
header{padding-top:1.4rem;border-bottom:1px solid var(--line);margin-bottom:.4rem}
h1{font-size:1.55rem;font-weight:800;letter-spacing:-.02em;margin:0 0 .25rem}
.mono{font-family:var(--mono)}
.meta{color:var(--muted);font-size:.73rem;margin:.1rem 0}
.legend{font-size:.7rem;color:var(--muted);margin:.4rem 0 .9rem}
.k{font-weight:700}.k.ok{color:var(--ok)}.k.tw{color:var(--tw)}.k.warn{color:var(--warn)}
details.cat{border-bottom:1px solid var(--line)}
details.cat>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:.5rem;padding:.9rem .1rem;user-select:none}
details.cat>summary::-webkit-details-marker{display:none}
.cn{font-size:1.05rem;font-weight:800;letter-spacing:-.01em}
.cc{margin-left:auto;color:var(--muted);font-size:.7rem;background:#EEF0F4;border-radius:999px;padding:.08rem .55rem}
details.cat>summary::after{content:"–";margin-left:.35rem;color:var(--muted);font-weight:800;font-size:1.1rem}
details.cat:not([open])>summary::after{content:"+"}
.arts{padding:.1rem 0 .7rem}
details.art{border-left:3px solid var(--line);margin:.45rem 0;background:#fff;border-radius:5px;box-shadow:0 1px 2px rgba(20,24,31,.05)}
details.art.ok{border-left-color:var(--ok)}details.art.tw{border-left-color:var(--tw)}details.art.warn{border-left-color:var(--warn)}
details.art>summary{list-style:none;cursor:pointer;display:flex;align-items:baseline;gap:.55rem;padding:.7rem .8rem}
details.art>summary::-webkit-details-marker{display:none}
.tag{font-size:.58rem;text-transform:uppercase;letter-spacing:.05em;padding:.14rem .4rem;border-radius:3px;white-space:nowrap;flex:none;font-family:var(--mono)}
.art.ok .tag{color:var(--ok);background:#E7F3EC}.art.tw .tag{color:var(--tw);background:#F6EFDD}.art.warn .tag{color:var(--warn);background:#F6ECE0}
.t{font-weight:600;font-size:.97rem;line-height:1.35}
details.art>summary::after{content:"+";margin-left:auto;color:var(--muted);font-weight:800;flex:none;padding-left:.4rem}
details.art[open]>summary::after{content:"–"}
.body{padding:0 .85rem .9rem 1rem}
.mp{font-size:.98rem;font-weight:600;margin:.15rem 0 .55rem}
ul.hl{margin:.3rem 0 .55rem;padding-left:1.15rem}ul.hl li{margin:.3rem 0}
.topics{font-size:.66rem;color:var(--accent);margin:.45rem 0 .2rem;text-transform:lowercase;font-family:var(--mono)}
.sources{font-size:.7rem;color:var(--muted);margin:.35rem 0 0;word-break:break-word;font-family:var(--mono)}
.sources a,.body a{color:var(--accent);text-decoration:none;border-bottom:1px solid #C7CEE0}
.note{font-size:.64rem;color:var(--muted);margin:.4rem 0 0;font-family:var(--mono)}
.fail .cn{color:var(--warn)}.fail li{color:var(--warn);font-size:.72rem;margin:.2rem 0;font-family:var(--mono)}
.mgrid{display:grid;grid-template-columns:1fr;gap:.4rem;padding:.2rem 0 .5rem}
@media(min-width:38rem){.mgrid{grid-template-columns:repeat(3,1fr)}}
.mreg h3{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:.4rem 0 .3rem;font-family:var(--mono)}
ol.mlist{list-style:none;margin:0;padding:0}
ol.mlist li{display:flex;align-items:baseline;gap:.4rem;font-size:.82rem;padding:.16rem 0;border-bottom:1px solid #F0F2F5}
.rk{color:var(--muted);font-size:.68rem;width:1rem;font-family:var(--mono)}
.nm{flex:1;font-weight:600}
.cap{font-family:var(--mono);font-size:.74rem;color:var(--ink)}
.chg{font-family:var(--mono);font-size:.7rem;width:3.1rem;text-align:right}
.chg.up{color:var(--ok)}.chg.dn{color:#C0392B}
.stale{color:var(--warn);font-weight:700}
.mnote{font-size:.62rem;color:var(--muted);font-family:var(--mono);margin:.2rem 0 0}
"""


def fmt_cap(v, cur):
    if not v:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"{cur}{v/div:.2f}{unit}"
    return f"{cur}{v:,.0f}"


def _yf_one(sym):
    import yfinance as yf
    cap = last = prev = None
    try:
        fi = yf.Ticker(sym).fast_info
        get = (lambda k: fi.get(k)) if isinstance(fi, dict) else (lambda k: getattr(fi, k, None))
        cap, last, prev = get("market_cap"), get("last_price"), get("previous_close")
    except Exception:
        pass
    pct = ((last / prev) - 1) * 100 if last and prev else None
    return cap, pct


def _finnhub_one(sym, key):
    cap = pct = None
    try:
        p = requests.get("https://finnhub.io/api/v1/stock/profile2",
                         params={"symbol": sym, "token": key}, timeout=20).json()
        mc = p.get("marketCapitalization")             # millions, USD
        cap = mc * 1e6 if mc else None
    except Exception:
        pass
    try:
        q = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": sym, "token": key}, timeout=20).json()
        pct = q.get("dp")
    except Exception:
        pass
    return cap, pct


def fetch_market_caps(cfg, state):
    """Ranked market caps per region. Caches last-good; never raises to caller."""
    import time
    m = cfg.get("markets", {})
    if not m.get("enabled") or m.get("provider", "none") == "none":
        return []
    provider = m["provider"]
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    cache = state.setdefault("markets_cache", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    out = []
    for region in m.get("regions", []):
        rows = []
        for e in region.get("symbols", []):
            sym = e["sym"]
            cap = pct = None
            stale = False
            for _ in range(2):
                cap, pct = (_finnhub_one(sym, key) if provider == "finnhub" else _yf_one(sym))
                if cap:
                    break
                time.sleep(0.6)
            if cap:
                cache[sym] = {"cap": cap, "pct": pct, "ts": now_iso}
            elif sym in cache:                          # fall back to last-good value
                cap, pct, stale = cache[sym]["cap"], cache[sym].get("pct"), True
            if cap:
                rows.append({"name": e.get("name", sym), "cap": cap, "pct": pct, "stale": stale})
            time.sleep(0.25)
        rows.sort(key=lambda r: r["cap"], reverse=True)
        if rows:
            out.append({"name": region["name"], "cur": region.get("currency", "$"), "rows": rows})
    return out


def build_markets_panel(markets):
    if not markets:
        return ""
    cols = []
    for reg in markets:
        lis = []
        for i, r in enumerate(reg["rows"], 1):
            if r["pct"] is None:
                chg = ""
            else:
                chg = f"<span class='chg {'up' if r['pct'] >= 0 else 'dn'}'>{r['pct']:+.1f}%</span>"
            stale = "<span class='stale' title='last known'>*</span>" if r["stale"] else ""
            lis.append(f"<li><span class='rk'>{i}</span><span class='nm'>{esc(r['name'])}</span>"
                       f"<span class='cap'>{esc(fmt_cap(r['cap'], reg['cur']))}{stale}</span>{chg}</li>")
        cols.append(f"<div class='mreg'><h3>{esc(reg['name'])}</h3><ol class='mlist'>{''.join(lis)}</ol></div>")
    return ("<details class='cat mkt' open><summary><span class='cn'>Markets — top by market cap</span>"
            "</summary><div class='mgrid'>" + "".join(cols)
            + "</div><p class='mnote'>* = last known value (live fetch failed)</p></details>")


def build_html(stories, cfg, report, failures, markets=None, pv_html=""):
    s, now = cfg["settings"], datetime.now(timezone.utc)
    by_cat = {}
    for st in stories:
        by_cat.setdefault(st["category"], []).append(st)
    order = [c for c in (cfg.get("categories") or []) if c in by_cat] + \
            [c for c in by_cat if c not in (cfg.get("categories") or [])]
    cls = {"corroborated": "ok", "trusted_single": "tw", "single": "warn"}
    lbl = {"corroborated": "corroborated", "trusted_single": "trusted single", "single": "single"}

    blocks = []
    for cat in order:
        arts = []
        for st in by_cat[cat]:
            c = cls[st["corr"]]
            bullets = ("<ul class='hl'>" + "".join(f"<li>{esc(h)}</li>" for h in st["highlights"])
                       + "</ul>") if st["highlights"] else ""
            topics = (f"<p class='topics'>topics: {esc(' · '.join(st['matched']))}</p>"
                      if st["matched"] else "")
            srcs = " · ".join(f'<a href="{esc(i["link"])}">{esc(i["source"])}</a>'
                              for i in st["items"][:6])
            note = "" if st["ai"] else "<p class='note'>outlet text · AI skipped</p>"
            arts.append(
                f"<details class='art {c}'><summary><span class='tag'>{lbl[st['corr']]}</span>"
                f"<span class='t'>{esc(st['headline'])}</span></summary>"
                f"<div class='body'><p class='mp'>{esc(st['main_point'])}</p>{bullets}{topics}"
                f"<p class='sources'>sources: {srcs}</p>{note}</div></details>")
        blocks.append(
            f"<details class='cat' open><summary><span class='cn'>{esc(cat)}</span>"
            f"<span class='cc mono'>{len(by_cat[cat])}</span></summary>"
            f"<div class='arts'>{''.join(arts)}</div></details>")

    fail = ""
    if failures:
        fail = ("<details class='cat fail'><summary><span class='cn'>Feeds that failed</span>"
                f"<span class='cc mono'>{len(failures)}</span></summary><div class='arts'><ul>"
                + "".join(f"<li>{esc(n)} — {esc(m)}</li>" for n, m in failures)
                + "</ul></div></details>")

    cap_note = ("cap reached — AI paused" if report["over_budget"]
                else ("AI on" if report["ai_used"] else "AI off"))
    meta = (f"{now:%a %d %b %Y · %H:%M UTC} · {len(stories)} stories · "
            f"${report['month_spent']:.4f}/${report['cap']:.2f} · {cap_note}")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(s['site_title'])}</title>
<style>{CSS}</style></head><body>
<header><h1>{esc(s['site_title'])}</h1><p class="meta mono">{esc(meta)}</p>
<p class="legend mono"><span class="k ok">■</span> corroborated (2+)&nbsp;&nbsp;<span class="k tw">■</span> trusted single&nbsp;&nbsp;<span class="k warn">■</span> single</p></header>
<main>{pv_html}{build_markets_panel(markets)}{''.join(blocks)}{fail}</main></body></html>"""


# =========================================================================== #
# PV TUBE — photovoltaics · energy storage · inverters, split EU / Asia / US
#
# * Allowlisted trade-press feeds only (feeds.yaml → pv_tube.sources). Each feed declares
#   its OWNER so corroboration counts independent newsrooms, not domains.
#   corroborated = 2+ different owners; trusted single = one trusted outlet; rest hidden.
# * Sponsored / partner / press-release / webinar items are dropped before any AI call.
# * One batched Gemini call per run (same model, prices and key as the digest), constrained
#   to fetched text. Every number (and, for English sources, every proper name) in the AI
#   output is checked against the source; failing bullets are removed, failing stories
#   fall back to the outlet's own text.
# * Worst-case cost is computed BEFORE the call; PV has its own sub-cap and also respects
#   the global monthly cap. Its data lives in state.json under "pv_tube".
# * Never breaks the digest: build_pv_tube() always returns a renderable section.
# =========================================================================== #
PV_REGIONS = ("EU", "Asia", "US")
PV_REGION_LABEL = {"EU": "🇪🇺 EU", "Asia": "🌏 Asia", "US": "🇺🇸 US"}
PV_UA = "Mozilla/5.0 (compatible; personal-news-digest/1.0; +https://github.com/)"
PV_API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------- keyword filter
# Polish stems cover inflection: fotowoltaika/-i/-ce, falownik/-a/-ów, magazyn(y/ów) energii ...
PV_KEYWORD_RX = [
    re.compile(r"fotowolta\w*", re.I),
    re.compile(r"magazyn\w*\s+energii", re.I),
    re.compile(r"inwerter\w*", re.I),
    re.compile(r"falownik\w*", re.I),
    re.compile(r"\bPV\b"),                     # case-sensitive: avoids "pv"/"present value" noise
    re.compile(r"photovoltaic\w*", re.I),
    re.compile(r"\b(micro-?)?inverters?\b", re.I),
    re.compile(r"\b(battery|energy)[\s-]storage\b", re.I),
    re.compile(r"\bBESS\b"),
    re.compile(r"\bsolar[\s-](power|panels?|modules?|pv|farms?|plants?|parks?|projects?|capacity|"
               r"installations?|cells?|wafers?|glass|manufactur\w*|tariffs?|market|energy|arrays?|"
               r"developers?|industry|generation|installers?|auctions?|tenders?)\b", re.I),
    re.compile(r"\bsolar\s*\+\s*storage\b", re.I),
]

# ---------------------------------------------------------------- shady-content filter
PV_SHADY_RX = re.compile(
    r"sponsored|advertorial|partner content|promoted|in association with|brought to you by|"
    r"\bwebinar\b|press release|paid post|"
    r"artykuł sponsorowany|materiał (partnera|promocyjny|sponsorowany)|\breklama\b|\bpromocja\b",
    re.I,
)
PV_SHADY_URL_RX = re.compile(r"/(sponsored|partner|partners|advertorial|promo|webinars?|press-releases?)/", re.I)

# ---------------------------------------------------------------- region classifier (case-sensitive)
def _pv_rx(words):
    return [re.compile(w) for w in words]

PV_GEO = {
    "EU": _pv_rx([
        r"\bEU\b", r"\bEurop\w*", r"\bBrussels\b", r"\bUE\b", r"\bUni\w* Europejsk\w*", r"\bBruksel\w*",
        r"\bAustria\w*", r"\bBelgi\w*", r"\bBulgari\w*", r"\bBułgari\w*", r"\bCroatia\w*", r"\bChorwacj\w*",
        r"\bCyprus\b", r"\bCzech\w*", r"\bDenmark\b", r"\bDanish\b", r"\bDani[ai]\b", r"\bEstoni\w*",
        r"\bFinland\w*", r"\bFinlandi\w*", r"\bFrance\b", r"\bFrench\b", r"\bFrancj\w*", r"\bGerman\w*",
        r"\bNiem\w*", r"\bGreece\b", r"\bGreek\b", r"\bGrecj\w*", r"\bHungar\w*", r"\bWęgr\w*",
        r"\bIreland\b", r"\bIrish\b", r"\bIrlandi\w*", r"\bItal\w*", r"\bWło\w*", r"\bLatvia\w*",
        r"\bŁotw\w*", r"\bLithuania\w*", r"\bLitw\w*", r"\bLuxembourg\w*", r"\bMalta\b",
        r"\bNetherlands\b", r"\bDutch\b", r"\bHolandi\w*", r"\bPoland\b", r"\bPolish\b", r"\bPols\w*",
        r"\bPortug\w*", r"\bRomania\w*", r"\bRumuni\w*", r"\bSlovak\w*", r"\bSłowac\w*", r"\bSloven\w*",
        r"\bSłoweni\w*", r"\bSpain\b", r"\bSpanish\b", r"\bHiszpan\w*", r"\bSwed\w*", r"\bSzwecj\w*",
    ]),
    "EU_WIDER": _pv_rx([
        r"\bUK\b", r"\bBritain\b", r"\bBritish\b", r"\bUnited Kingdom\b", r"\bEngland\b", r"\bScotland\b",
        r"\bWielk\w* Brytani\w*", r"\bNorw\w*", r"\bSwitzerland\b", r"\bSwiss\b", r"\bSzwajcari\w*",
        r"\bUkrain\w*",
    ]),
    "Asia": _pv_rx([
        r"\bAsia\w*", r"\bAzj\w*", r"\bASEAN\b", r"\bChin(a|ese|y|ach|ie|ą|ski\w*)\b", r"\bBeijing\b",
        r"\bShanghai\b", r"\bIndi(a|an|e|i|ach|ą|ami)\b", r"\bDelhi\b", r"\bJapan\w*", r"\bJaponi\w*",
        r"\bKore\w*", r"\bTaiwan\w*", r"\bTajwan\w*", r"\bViet\s?nam\w*", r"\bWietnam\w*",
        r"\bIndonesi\w*", r"\bPhilippin\w*", r"\bFilipin\w*", r"\bThai\w*", r"\bTajlandi\w*",
        r"\bMalaysi\w*", r"\bMalezj\w*", r"\bSingapore\b", r"\bSingapur\w*", r"\bPakistan\w*",
        r"\bBangladesh\w*", r"\bSri Lanka\w*", r"\bKazakh\w*", r"\bKazach\w*", r"\bUzbek\w*",
        r"\bTokyo\b", r"\bTokio\b",
    ]),
    "ASIA_GULF": _pv_rx([
        r"\bSaudi\w*", r"\bArabi\w* Saudyjsk\w*", r"\bUAE\b", r"\bEmirat\w*", r"\bDubai\b",
        r"\bAbu Dhabi\b", r"\bOman\w*", r"\bQatar\w*", r"\bKatar\w*", r"\bIsrael\w*", r"\bIzrael\w*",
    ]),
    "US": _pv_rx([
        r"\bU\.S\.(?=\W|$)", r"\bUS\b", r"\bUSA\b", r"\bUnited States\b", r"\bAmeric\w*", r"\bAmeryk\w*",
        r"\bStan\w* Zjednoczon\w*", r"\bFERC\b", r"\bDOE\b", r"\bEIA\b", r"\bSEIA\b", r"\bNREL\b",
        r"\bInflation Reduction Act\b", r"\bCAISO\b", r"\bERCOT\b", r"\bPJM\b", r"\bMISO\b", r"\bNYISO\b",
        r"\bCalifornia\w*", r"\bTexas\b", r"\bArizona\b", r"\bNevada\b", r"\bFlorida\b", r"\bNew York\b",
        r"\bMassachusetts\b", r"\bIllinois\b", r"\bIndiana\b", r"\bOhio\b", r"\bColorado\b", r"\bUtah\b",
        r"\bNorth Carolina\b", r"\bVirginia\b", r"\bNew Jersey\b", r"\bNew Mexico\b", r"\bOregon\b",
        r"\bMichigan\b", r"\bMinnesota\b", r"\bPennsylvania\b",
    ]),
}

PV_STOP = set("""the a an and or of to in on for with by from at as is are was were be been this that
its it's their new says said will could would into over after about more than up out has have had
not but also per via i w z na do się oraz dla od po przez jest są że to ten ta jak które który która
""".split())


# ---------------------------------------------------------------- data classes
@dataclass
class PvItem:
    source: str
    owner: str
    lang: str
    trust_single: bool
    default_region: str | None
    title: str
    link: str
    text: str
    published: datetime
    region: str | None = None
    tokens: dict = field(default_factory=dict)
    fp: set = field(default_factory=set)


@dataclass
class PvStory:
    items: list
    region: str
    badge: str                     # 🟢 / 🟡
    key: str
    summary: dict | None = None    # {"title","main_point","bullets","claim_type"}
    mode: str = "ai"               # "ai" | "extractive"


@dataclass
class PvResult:
    html: str
    used_urls: set
    cost_usd: float
    status: dict
    digest_stories: list = field(default_factory=list)   # same dict shape as news_digest.py stories
    report_line: str = ""


# ---------------------------------------------------------------- helpers
def _pv_clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _pv_norm_url(u: str) -> str:
    p = urlparse(u)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def _pv_domain_ok(link: str, allowed: list[str]) -> bool:
    host = urlparse(link).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allowed)


def pv_matches_keywords(text: str) -> bool:
    return any(rx.search(text) for rx in PV_KEYWORD_RX)


def pv_is_shady(title: str, text: str, link: str, categories: list[str]) -> bool:
    blob = " ".join([title, " ".join(categories)])
    if PV_SHADY_RX.search(blob) or PV_SHADY_URL_RX.search(link):
        return True
    # Sponsored markers often sit only at the very start of the body
    return bool(PV_SHADY_RX.search(text[:160]))


def pv_classify_region(title: str, text: str, default: str | None, cfg: dict) -> str | None:
    scores = {r: 0 for r in PV_REGIONS}
    groups = {"EU": ["EU"] + (["EU_WIDER"] if cfg.get("eu_include_wider_europe", True) else []),
              "Asia": ["Asia"] + (["ASIA_GULF"] if cfg.get("asia_include_gulf", False) else []),
              "US": ["US"]}
    first_pos = {}
    for region, keys in groups.items():
        for k in keys:
            for rx in PV_GEO[k]:
                for m in rx.finditer(title):
                    scores[region] += 3
                    first_pos[region] = min(first_pos.get(region, 10**6), m.start())
                scores[region] += len(rx.findall(text[:1500]))
    best = max(scores.values())
    if best == 0:
        return default                       # regional outlet → its home region; global outlet → None (dropped)
    top = [r for r, s in scores.items() if s == best]
    if len(top) == 1:
        return top[0]
    if default in top:
        return default
    return min(top, key=lambda r: first_pos.get(r, 10**6))


def _pv_tok(text: str) -> dict:
    words = re.findall(r"[a-ząćęłńóśźż0-9]{3,}", text.lower())
    tf = {}
    for w in words:
        if w not in PV_STOP:
            tf[w] = tf.get(w, 0) + 1
    return tf


_PV_UNIT_RX = re.compile(r"(\d[\d\s\u00a0.,]*\d|\d)\s?(GWh|MWh|kWh|TWh|GW|MW|kW|%|zł|PLN|EUR|€|USD|\$|/W|W)(?![A-Za-z])")


def _pv_fingerprint(text: str) -> set[str]:
    """Language-independent story fingerprint: numbers with units ('1,2 GW' == '1.2 GW')."""
    fp = set()
    for num, unit in _PV_UNIT_RX.findall(text):
        vals = _pv_numbers(num)
        if vals:
            unit = {"zł": "PLN", "€": "EUR", "$": "USD"}.get(unit, unit)
            fp.add(f"{next(iter(vals))}{unit}")
    return fp


def _pv_similar(a: "PvItem", b: "PvItem", thr: float) -> bool:
    cos = _pv_cos(a.tokens, b.tokens)
    if cos >= thr:
        return True
    shared = len(a.fp & b.fp)                 # cross-language match (PL ↔ EN) via shared figures
    return shared >= 2 or (shared == 1 and cos >= 0.10)


def _pv_cos(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


# ---------------------------------------------------------------- numeric / entity grounding
_PV_NUM_RX = re.compile(r"\d[\d\s\u00a0\u202f.,]*\d|\d")


def _pv_numbers(text: str) -> set[str]:
    """Normalise EN (1,200.5) and PL (1 200,5) formats to a canonical form."""
    out = set()
    for raw in _PV_NUM_RX.findall(text):
        s = re.sub(r"[\s\u00a0\u202f]", "", raw).rstrip(".,")
        if not s:
            continue
        if "," in s and "." in s:                       # 1,200.5 or 1.200,5
            s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
        elif "," in s:                                  # 1,5 (PL decimal) or 1,200 (EN thousands)
            parts = s.split(",")
            s = s.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else s.replace(",", ".")
        elif s.count(".") > 1:                          # 1.200.000 (PL thousands)
            s = s.replace(".", "")
        try:
            v = float(s)
        except ValueError:
            continue
        out.add(f"{v:g}")
    return out


_PV_ENTITY_OK = {"EU", "US", "U.S.", "UK", "PV", "BESS", "GW", "MW", "GWh", "MWh", "kW", "kWh", "TWh",
              "AC", "DC", "CEO", "Asia", "Europe", "European", "Chinese", "American", "AI", "The", "A", "An",
              "In", "On", "At", "It", "Its", "This", "That", "These", "Those", "However", "According",
              "Meanwhile", "Also", "While", "After", "Before", "Despite", "Both", "Each", "Most", "Some",
              "Many", "Several", "Overall", "Total", "Figures", "Data", "Sources", "Source", "Analysts",
              "Officials"}


def pv_grounded(claim: str, source_text: str, check_entities: bool) -> bool:
    src_nums = _pv_numbers(source_text)
    # strip [S1]-style refs before checking numbers
    claim_nums = _pv_numbers(re.sub(r"\[S\d+\]", "", claim))
    if not claim_nums <= src_nums:
        return False
    if check_entities:
        low = source_text.lower()
        words = claim.split()
        for i, raw in enumerate(words):
            w = re.sub(r"(['’]s)$", "", raw.strip(".,;:()\"'’“”"))
            # sentence-initial words ARE checked (that's where "Tesla supplied..." hides);
            # ordinary words pass because the model paraphrases source vocabulary.
            if len(w) < 3 or not w[0].isupper() or w in _PV_ENTITY_OK:
                continue
            if not all(part.lower() in low for part in w.split("-") if len(part) >= 3):
                return False
    return True


# ---------------------------------------------------------------- state
def _pv_init_state(st: dict) -> dict:
    for k in ("ledger", "cache", "shown", "health", "alerts"):
        st.setdefault(k, {})
    return st


def pv_load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    return _pv_init_state(st)


def pv_prune_state(st: dict, today: str) -> None:
    cutoff = (datetime.fromisoformat(today) - timedelta(days=10)).date().isoformat()
    st["cache"] = {k: v for k, v in st["cache"].items() if v.get("date", "") >= cutoff}
    st["shown"] = {k: v for k, v in st["shown"].items() if v >= cutoff}
    months = sorted(st["ledger"])[-3:]
    st["ledger"] = {m: st["ledger"][m] for m in months}


def pv_save_state(path: str, st: dict, today: str) -> None:
    pv_prune_state(st, today)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)          # atomic: a crash can't leave a half-written ledger


# ---------------------------------------------------------------- fetch
def _pv_fetch_one(src: dict, timeout: tuple) -> tuple[dict, list, str | None]:
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(src["url"], headers={"User-Agent": PV_UA}, timeout=timeout)
            if r.status_code in (429, 503) and attempt == 0:
                time.sleep(min(int(r.headers.get("Retry-After", "5") or 5), 15))
                continue
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if parsed.bozo and not parsed.entries:
                return src, [], f"unparseable feed ({type(parsed.bozo_exception).__name__})"
            return src, parsed.entries, None
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(2)
    return src, [], last_err


def pv_fetch_all(cfg: dict, state: dict, now: datetime) -> tuple[list[PvItem], dict]:
    window = timedelta(hours=cfg.get("window_hours", 36))
    items, stats = [], {"fetched": 0, "keyword": 0, "shady": 0, "offdomain": 0, "noregion": 0}
    timeout = (5, cfg.get("fetch_timeout_s", 15))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda s: _pv_fetch_one(s, timeout), cfg["sources"]))
    for src, entries, err in results:
        h = state["health"].setdefault(src["name"], {"fails": 0})
        if err:
            h["fails"] += 1
            h["last_error"] = err
            continue
        h["fails"], h["last_ok"] = 0, now.date().isoformat()
        h.pop("last_error", None)
        allowed = src.get("link_domains") or [urlparse(src["url"]).netloc.lower().removeprefix("www.")]
        for e in entries[: cfg.get("max_entries_per_feed", 40)]:
            stats["fetched"] += 1
            t = e.get("published_parsed") or e.get("updated_parsed")
            if not t:
                continue
            pub = datetime(*t[:6], tzinfo=timezone.utc)
            if now - pub > window or pub - now > timedelta(hours=2):
                continue
            title = _pv_clean(e.get("title", ""))
            body = _pv_clean((e.get("content") or [{}])[0].get("value", "") or e.get("summary", ""))
            link = e.get("link", "")
            cats = [c.get("term", "") for c in e.get("tags", []) or []]
            if not title or not link:
                continue
            if not _pv_domain_ok(link, allowed):
                stats["offdomain"] += 1
                continue
            if pv_is_shady(title, body, link, cats):
                stats["shady"] += 1
                continue
            if not pv_matches_keywords(f"{title} {body[:2000]} {' '.join(cats)}"):
                continue
            stats["keyword"] += 1
            region = pv_classify_region(title, body, src.get("region"), cfg)
            if region not in PV_REGIONS:
                stats["noregion"] += 1
                continue
            it = PvItem(src["name"], src["owner"], src.get("lang", "en"), bool(src.get("trust_single")),
                      src.get("region"), title, link, body[:4000], pub, region)
            it.tokens = _pv_tok(f"{title} {title} {body[:1200]}")
            it.fp = _pv_fingerprint(f"{title} {body[:1500]}")
            items.append(it)
    return items, stats


# ---------------------------------------------------------------- cluster + corroborate + rank
def pv_build_stories(items: list[PvItem], cfg: dict, state: dict, today: str) -> list[PvStory]:
    thr = cfg.get("cluster_similarity", 0.28)
    items = sorted(items, key=lambda i: i.published, reverse=True)
    clusters: list[list[PvItem]] = []
    for it in items:
        for c in clusters:
            if c[0].region == it.region and _pv_similar(c[0], it, thr):
                c.append(it)
                break
        else:
            clusters.append([it])

    stories = []
    for c in clusters:
        # de-duplicate the same URL syndicated twice
        seen, uniq = set(), []
        for it in c:
            if _pv_norm_url(it.link) not in seen:
                seen.add(_pv_norm_url(it.link))
                uniq.append(it)
        owners = {i.owner for i in uniq}
        if len(owners) >= 2:
            badge = "🟢"
        elif any(i.trust_single for i in uniq):
            badge = "🟡"
        else:
            continue
        urls = sorted(_pv_norm_url(i.link) for i in uniq)
        # anti-fatigue: skip if every URL was already shown on an EARLIER day
        if all(state["shown"].get(u, today) < today for u in urls):
            continue
        key = hashlib.sha1("|".join(urls).encode()).hexdigest()[:16]
        stories.append(PvStory(uniq, uniq[0].region, badge, key))

    def score(s: PvStory):
        age_h = (datetime.now(timezone.utc) - s.items[0].published).total_seconds() / 3600
        return (2 if s.badge == "🟢" else 0) + len({i.owner for i in s.items}) - age_h / 24

    per = cfg.get("max_per_region", 3)
    out = []
    for r in PV_REGIONS:
        out += sorted([s for s in stories if s.region == r], key=score, reverse=True)[:per]
    return out


# ---------------------------------------------------------------- summarise
PV_SYSTEM_PROMPT = """You summarise solar PV, energy-storage and inverter news for a daily digest.
Rules — these are strict:
1. Use ONLY the source text provided for each story. No outside knowledge, no background, no predictions.
2. Write in English. Translate Polish sources faithfully.
3. Copy every number exactly as the source states it (you may translate unit words, e.g. "mln zł" -> "million PLN"). Never calculate, convert currencies, add up or round numbers. Do not abbreviate periods (write "first half", not "H1").
4. If sources disagree, say so in a bullet and attribute each figure to its source tag, e.g. [S1].
5. claim_type: "official_data" if the core fact comes from a regulator, grid operator, statistics office or government; "company_claim" if it rests on a company's own announcement (e.g. efficiency records, orders, capacity plans) without independent confirmation in the text; otherwise "reported".
6. Use sentence case; capitalise only proper names, spelled as in the source.
7. Give 2 to 6 bullets. Fewer is better than padding — never invent detail to reach a count.
Return ONLY a JSON array, no prose, no code fences:
[{"id": "<story id>", "title": "<neutral headline in sentence case, max 14 words>", "main_point": "<one sentence>", "bullets": ["..."], "claim_type": "reported|official_data|company_claim"}]"""


def _pv_source_block(story: PvStory, max_sources: int, chars: int) -> tuple[str, str, list]:
    picked, owners = [], set()
    for it in story.items:                      # one source per owner, prefer independents
        if it.owner not in owners:
            picked.append(it)
            owners.add(it.owner)
        if len(picked) == max_sources:
            break
    parts, raw = [], []
    for n, it in enumerate(picked, 1):
        txt = it.text[:chars]
        parts.append(f"[S{n}] {it.source} ({it.lang})\nHEADLINE: {it.title}\nTEXT: {txt}")
        raw.append(f"{it.title} {txt}")
    return "\n\n".join(parts), " ".join(raw), [it.source for it in picked]


PV_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _pv_post_with_retry(url: str, headers: dict, body: dict):
    """One retry on rate-limit / overload, honouring Retry-After (capped). Returns (response|None, err)."""
    for attempt in range(2):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=90)
        except requests.RequestException as e:
            if attempt == 0:
                time.sleep(5)
                continue
            return None, type(e).__name__
        if r.status_code in (429, 500, 502, 503, 529) and attempt == 0:
            time.sleep(min(float(r.headers.get("retry-after", "10") or 10), 20))
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:150]}"
        return r, None
    return None, "retries exhausted"


def _pv_call_llm(prompt: str, cfg: dict, api_key: str) -> tuple[str | None, dict, str | None]:
    """Returns (text, usage{in,out,model}, error). Usage includes thinking tokens (billed as output)."""
    max_out = cfg["max_output_tokens"]
    if cfg["provider"] == "anthropic":
        body = {"model": cfg["model"], "max_tokens": max_out, "temperature": 0, "system": PV_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}]}
        r, err = _pv_post_with_retry(PV_API_URL, {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"}, body)
        if err:
            return None, {}, err
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                      "model": d.get("model", cfg["model"])}, None

    # --- Gemini (default: same provider, model and key as the rest of the digest) ---
    gen = {"temperature": 0, "maxOutputTokens": max_out, "responseMimeType": "application/json"}
    if cfg["model"].startswith("gemini-3"):
        gen["thinkingConfig"] = {"thinkingLevel": "minimal"}      # thinking tokens are billed as output
    body = {"systemInstruction": {"parts": [{"text": PV_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    r, err = _pv_post_with_retry(PV_GEMINI_URL.format(model=cfg["model"]),
                              {"x-goog-api-key": api_key, "content-type": "application/json"}, body)
    if err:
        return None, {}, err
    d = r.json()
    u = d.get("usageMetadata", {})
    usage = {"in": u.get("promptTokenCount", 0),
             "out": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
             "model": d.get("modelVersion", cfg["model"])}
    cands = d.get("candidates") or []
    if not cands:
        return None, usage, f"no candidates ({d.get('promptFeedback', {}).get('blockReason', 'unknown')})"
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if cands[0].get("finishReason") not in (None, "STOP"):
        return None, usage, f"finishReason={cands[0].get('finishReason')}"
    return text, usage, None


def _pv_parse_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _pv_extractive(story: PvStory) -> dict:
    it = story.items[0]
    sentences = re.split(r"(?<=[.!?])\s+", it.text)
    main = next((s for s in sentences if len(s) > 40), it.text[:220])[:300]
    return {"title": it.title, "main_point": main, "bullets": [], "claim_type": "reported"}


def pv_summarise(stories: list[PvStory], cfg: dict, state: dict, month: str, today: str,
              api_key: str | None) -> tuple[float, list[str]]:
    notes, cost = [], 0.0
    todo = []
    for s in stories:
        cached = state["cache"].get(s.key)
        if cached:
            s.summary, s.mode = cached["summary"], cached["mode"]
        else:
            todo.append(s)
    if not todo:
        return 0.0, notes

    ms, chars = cfg.get("max_sources_per_story", 2), cfg.get("chars_per_source", 900)
    blocks = {s.key: _pv_source_block(s, ms, chars) for s in todo}
    prompt = "\n\n=====\n\n".join(f"STORY id={k}\n{b[0]}" for k, b in blocks.items())

    p_in, p_out = cfg["price_in"], cfg["price_out"]
    est_in_tokens = (len(PV_SYSTEM_PROMPT) + len(prompt)) / 2.5          # conservative for Polish text
    # x2 output: allowance for thinking tokens; x2 overall: one retry
    worst = 2 * (est_in_tokens * p_in + 2 * cfg["max_output_tokens"] * p_out) / 1e6
    spent = state["ledger"].get(month, 0.0)
    cap = cfg.get("monthly_cost_cap_usd", 0.75)

    text = None
    if not api_key:
        notes.append(f"no API key in {cfg['api_key_env']} — extractive mode")
    elif spent + worst > cap:
        notes.append(f"budget guard: ${spent:.2f} spent + ${worst:.3f} worst case > ${cap:.2f} cap — extractive mode")
    elif cfg.get("_global_left") is not None and worst > cfg["_global_left"]:
        notes.append("global monthly cap nearly reached — extractive mode")
    else:
        text, usage, err = _pv_call_llm(prompt, cfg, api_key)
        if usage:
            cost = (usage["in"] * p_in + usage["out"] * p_out) / 1e6
            state["ledger"][month] = round(spent + cost, 6)
            # "-latest" aliases get hot-swapped by the provider → prices in feeds.yaml can silently go stale
            prev = state.get("model_seen")
            if prev and prev != usage["model"]:
                notes.append(f"MODEL CHANGED: {prev} → {usage['model']} — re-check prices in feeds.yaml")
            state["model_seen"] = usage["model"]
        if err:
            notes.append(f"AI call failed ({err}) — extractive mode")

    by_id = {str(d.get("id")): d for d in _pv_parse_json_array(text) if isinstance(d, dict)}
    dropped_bullets = 0
    for s in todo:
        d, src_text = by_id.get(s.key), blocks[s.key][1]
        check_ent = all(i.lang == "en" for i in s.items)
        ok = (d and isinstance(d.get("title"), str) and isinstance(d.get("main_point"), str)
              and isinstance(d.get("bullets"), list)
              and pv_grounded(d["title"], src_text, False)          # headlines: numbers only
              and pv_grounded(d["main_point"], src_text, check_ent))
        if ok:
            bullets = [b for b in d["bullets"][:6] if isinstance(b, str) and pv_grounded(b, src_text, check_ent)]
            dropped_bullets += min(len(d["bullets"]), 6) - len(bullets)
            ctype = d.get("claim_type") if d.get("claim_type") in ("reported", "official_data", "company_claim") else "reported"
            names = blocks[s.key][2]

            def refs(t: str) -> str:          # "[S2]" → "(Utility Dive)" — readable attribution
                return re.sub(r"\s*\[S(\d+)\]", lambda m: f" ({names[int(m.group(1)) - 1]})"
                              if 0 < int(m.group(1)) <= len(names) else "", t).strip()
            if len(bullets) >= 1:
                s.summary, s.mode = {"title": refs(d["title"]), "main_point": refs(d["main_point"]),
                                     "bullets": [refs(b) for b in bullets], "claim_type": ctype}, "ai"
            else:
                ok = False
        if not ok:
            s.summary, s.mode = _pv_extractive(s), "extractive"
        # Cache whenever the model actually answered (incl. grounding failures → no paid retry loop).
        # If the call never happened (budget/API down), don't cache: the next run may still use AI.
        if text is not None:
            state["cache"][s.key] = {"summary": s.summary, "mode": s.mode, "date": today}
    if dropped_bullets:
        notes.append(f"{dropped_bullets} bullet(s) removed by grounding check")
    return cost, notes


# ---------------------------------------------------------------- alerts (deduplicated → no alert fatigue)
def pv_alerts(cfg: dict, state: dict, month: str) -> list[str]:
    out = []
    spent, cap = state["ledger"].get(month, 0.0), cfg.get("monthly_cost_cap_usd", 0.75)
    for pct in (80, 100):
        key = f"budget{pct}:{month}"
        if spent >= cap * pct / 100 and key not in state["alerts"]:
            state["alerts"][key] = True
            out.append(f"PV Tube AI spend at {pct}% of cap (${spent:.2f} / ${cap:.2f}) for {month}")
    n = cfg.get("dead_feed_after_runs", 3)
    for name, h in state["health"].items():
        key = f"dead:{name}"
        if h.get("fails", 0) >= n and key not in state["alerts"]:
            state["alerts"][key] = True
            out.append(f"PV Tube feed '{name}' failing {h['fails']} runs in a row: {h.get('last_error', '')}")
        elif h.get("fails", 0) == 0:
            state["alerts"].pop(key, None)       # re-arm after recovery
    return out


def _pv_emit(msgs: list[str], summary_md: str) -> None:
    for m in msgs:
        print(f"::warning title=PV Tube::{m}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary_md + "\n")


# ---------------------------------------------------------------- render
PV_CLAIM_LABEL = {"official_data": "official data", "company_claim": "company claim — not independently confirmed",
               "reported": ""}
_PV_CLS = {"🟢": ("ok", "corroborated"), "🟡": ("tw", "trusted single")}


def pv_render(stories: list[PvStory], footer: str, cfg: dict) -> str:
    """One collapsible 'PV Tube' category, styled with news_digest.py's existing CSS classes."""
    e = html.escape
    regions = []
    for r in PV_REGIONS:
        rs = [s for s in stories if s.region == r]
        arts = []
        for s in rs:
            sm, (c, lbl) = s.summary, _PV_CLS[s.badge]
            bullets = ("<ul class='hl'>" + "".join(f"<li>{e(b)}</li>" for b in sm["bullets"]) + "</ul>"
                       if sm["bullets"] else "")
            claim = PV_CLAIM_LABEL.get(sm.get("claim_type", ""), "")
            claim_html = f"<p class='topics'>{e(claim)}</p>" if claim else ""
            srcs = " · ".join(f'<a href="{e(i.link)}">{e(i.source)}</a>' + (" 🇵🇱" if i.lang == "pl" else "")
                              for i in s.items)
            note = "" if s.mode == "ai" else "<p class='note'>outlet text · AI skipped</p>"
            arts.append(
                f"<details class='art {c}'><summary><span class='tag'>{lbl}</span>"
                f"<span class='t'>{e(sm['title'])}</span></summary>"
                f"<div class='body'><p class='mp'>{e(sm['main_point'])}</p>{bullets}{claim_html}"
                f"<p class='sources'>sources: {srcs}</p>{note}</div></details>")
        if not arts:
            arts.append(f"<p class='note'>no new verified PV news in the last "
                        f"{cfg.get('window_hours', 36)} h</p>")
        regions.append(f"<div class='mreg'><h3>{PV_REGION_LABEL[r]}</h3>{''.join(arts)}</div>")
    return (f"<details class='cat pv' open><summary><span class='cn'>☀️ PV Tube</span>"
            f"<span class='cc mono'>{len(stories)}</span></summary><div class='arts'>"
            + "".join(regions) + f"<p class='mnote'>{e(footer)}</p></div></details>")


def pv_to_digest_stories(stories: list[PvStory]) -> list[dict]:
    """Convert to news_digest.py's story dict so its RSS builder emits them unchanged."""
    out = []
    for s in stories:
        sm = s.summary
        its = [{"title": i.title, "link": i.link, "source": i.source, "weight": 1.0,
                "ts_obj": i.published} for i in s.items]
        claim = PV_CLAIM_LABEL.get(sm.get("claim_type", ""), "")
        out.append({
            "id": "pv-" + s.key, "headline": sm["title"], "category": f"PV Tube · {s.region}",
            "sources": sorted({i.source for i in s.items}),
            "corr": "corroborated" if s.badge == "🟢" else "trusted_single",
            "items": its, "newest": max(i.published for i in s.items),
            "main_point": sm["main_point"], "highlights": list(sm["bullets"]),
            "matched": ["PV Tube", s.region] + ([claim] if claim else []), "ai": s.mode == "ai",
        })
    return out


# ---------------------------------------------------------------- entry point
def pv_resolve_config(full: dict) -> dict:
    """PV Tube settings + LLM settings inherited from the digest's `settings` block (one provider, one key)."""
    cfg = dict(full.get("pv_tube", full))
    st = full.get("settings", {})
    provider = cfg.get("llm_provider", st.get("llm_provider", "gemini"))
    cfg["provider"] = provider
    if provider == "anthropic":
        cfg.setdefault("model", st.get("anthropic_model", "claude-haiku-4-5-20251001"))
        cfg.setdefault("api_key_env", "ANTHROPIC_API_KEY")
    else:
        cfg["model"] = cfg.get("model") or st.get("gemini_model", "gemini-3.1-flash-lite")
        cfg.setdefault("api_key_env", "GEMINI_API_KEY")
    cfg["price_in"] = cfg.get("price_input_per_mtok", st.get("price_input_per_mtok", 1.0))
    cfg["price_out"] = cfg.get("price_output_per_mtok", st.get("price_output_per_mtok", 5.0))
    cfg.setdefault("max_output_tokens", 2500)
    return cfg


def build_pv_tube(full_config: dict, state: dict | None = None, state_path: str | None = None,
                  api_key: str | None = None, global_left_usd: float | None = None) -> PvResult:
    """Pass the whole parsed feeds.yaml.
    state: pass news_digest.py's state dict → PV data lives under state["pv_tube"] and is saved with
           state.json by the digest (no extra file, no workflow change). Without it, a file is used.
    Never raises: on any unexpected error returns a minimal section so the digest still builds."""
    cfg = pv_resolve_config(full_config)
    cfg["_global_left"] = global_left_usd
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    now = datetime.now(timezone.utc)
    today, month = now.astimezone(tz).date().isoformat(), now.astimezone(tz).strftime("%Y-%m")
    if api_key is None:
        api_key = os.environ.get(cfg["api_key_env"]) or (
            os.environ.get("GOOGLE_API_KEY") if cfg["provider"] == "gemini" else None)
    if state is not None:
        st = _pv_init_state(state.setdefault("pv_tube", {}))
    else:
        state_path = state_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "pv_tube_state.json")
        st = pv_load_state(state_path)
    try:
        if not cfg.get("enabled", True):
            return PvResult("", set(), 0.0, {"disabled": True})
        items, stats = pv_fetch_all(cfg, st, now)
        stories = pv_build_stories(items, cfg, st, today)
        cost, notes = pv_summarise(stories, cfg, st, month, today, api_key)
        for s in stories:
            for i in s.items:
                st["shown"].setdefault(_pv_norm_url(i.link), today)

        healthy = sum(1 for s in cfg["sources"] if st["health"].get(s["name"], {}).get("fails", 1) == 0)
        spent, cap = st["ledger"].get(month, 0.0), cfg.get("monthly_cost_cap_usd", 0.30)
        footer = (f"feeds {healthy}/{len(cfg['sources'])} ok · filtered out {stats['shady']} sponsored/PR, "
                  f"{stats['offdomain']} off-domain · PV AI spend ${spent:.4f}/${cap:.2f}")
        warn = pv_alerts(cfg, st, month)
        md = (f"### PV Tube\n- model: {st.get('model_seen', cfg['model'])}\n- stories: {len(stories)} "
              f"({sum(s.mode == 'ai' for s in stories)} AI, {sum(s.mode == 'extractive' for s in stories)} extractive)"
              f"\n- run cost: ${cost:.5f}; month: ${spent:.4f} / ${cap:.2f}\n- stats: {stats}\n"
              + "".join(f"- note: {n}\n" for n in notes) + "".join(f"- ⚠️ {w}\n" for w in warn))
        _pv_emit(warn, md)
        for n in notes:
            print(f"[pv] {n}")
        used = {i.link for s in stories for i in s.items}
        line = (f"PV Tube: {len(stories)} stories · feeds {healthy}/{len(cfg['sources'])} ok · "
                f"PV AI ${spent:.4f}/${cap:.2f}" + ("".join(f" · ⚠️ {w}" for w in warn)))
        return PvResult(pv_render(stories, footer, cfg), used, cost,
                            {"stats": stats, "notes": notes, "alerts": warn},
                            pv_to_digest_stories(stories), line)
    except Exception as ex:                                            # isolation boundary
        msg = f"PV Tube skipped this run: {type(ex).__name__}: {str(ex)[:150]}"
        _pv_emit([msg], f"### PV Tube\n- ⚠️ {msg}\n")
        page = ("<details class='cat pv' open><summary><span class='cn'>☀️ PV Tube</span></summary>"
                "<div class='arts'><p class='note'>PV Tube is temporarily unavailable; "
                "the rest of the digest is unaffected.</p></div></details>")
        return PvResult(page, set(), 0.0, {"error": msg}, [], f"⚠️ {msg}")
    finally:
        if state is not None:
            pv_prune_state(st, today)
        else:
            pv_save_state(state_path, st, today)


# ---------------------------------------------------------------- one-off feed validator
def pv_validate_feeds(cfg: dict) -> int:
    """python news_digest.py --validate-pv : checks every PV feed URL (no AI call, no cost)."""
    now, bad = datetime.now(timezone.utc), 0
    print(f"{'source':28} {'status':8} {'items':>5} {'<7d':>4} {'kw':>4} {'shady':>5}  latest")
    for src in cfg["sources"]:
        _, entries, err = _pv_fetch_one(src, (5, 20))
        if err or not entries:
            bad += 1
            print(f"{src['name'][:28]:28} {'FAIL':8} {err or 'no entries'}")
            continue
        recent = kw = shady = 0
        latest = None
        for e in entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t:
                pub = datetime(*t[:6], tzinfo=timezone.utc)
                latest = max(latest or pub, pub)
                recent += (now - pub) < timedelta(days=7)
            ti, body = _pv_clean(e.get("title", "")), _pv_clean(e.get("summary", ""))
            kw += pv_matches_keywords(f"{ti} {body}")
            shady += pv_is_shady(ti, body, e.get("link", ""), [c.get("term", "") for c in e.get("tags", []) or []])
        status = "OK" if recent else "STALE"
        bad += status != "OK"
        print(f"{src['name'][:28]:28} {status:8} {len(entries):>5} {recent:>4} {kw:>4} {shady:>5}  "
              f"{latest.date() if latest else '-'}")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    state = load_state()
    if state.get("month") != current_month():
        state = {"month": current_month(), "spent_usd": 0.0, "requests": 0,
                 "pv_tube": state.get("pv_tube", {})}          # keep PV Tube's "already shown" log

    # ---- PV Tube: runs first so its stories aren't repeated below, shares the monthly cap ----
    pv = None
    if build_pv_tube and cfg.get("pv_tube", {}).get("enabled", False):
        try:
            left = 0.0 if cfg["settings"]["llm_provider"] == "none" else \
                max(cfg["settings"]["monthly_cost_cap_usd"] - state["spent_usd"], 0.0)
            pv = build_pv_tube(cfg, state=state, global_left_usd=left)
            state["spent_usd"] = round(state["spent_usd"] + pv.cost_usd, 6)
            if pv.cost_usd:
                state["requests"] = state.get("requests", 0) + 1
            print(f"[pv] {len(pv.digest_stories)} stories · run ${pv.cost_usd:.5f}")
        except Exception as e:
            pv = None
            print(f"[pv] skipped: {e}", file=sys.stderr)

    items, failures = fetch_sources(cfg)
    if pv:
        items = [i for i in items if i["link"] not in pv.used_urls]
    print(f"[fetch] {len(items)} items, {len(failures)} feed failures")
    clusters = cluster_items(items, cfg["settings"]["cluster_similarity"])
    stories = rank_and_select(clusters, cfg)
    print(f"[rank] {len(clusters)} clusters -> {len(stories)} stories")
    report = summarize(stories, cfg, state)
    if pv:
        report["cost_this_run"] = round(report["cost_this_run"] + pv.cost_usd, 6)
        report["pv_line"] = pv.report_line
    try:
        markets = fetch_market_caps(cfg, state)
        print(f"[markets] {sum(len(r['rows']) for r in markets)} tickers across {len(markets)} regions")
    except Exception as e:
        markets = []
        print(f"[markets] skipped: {e}", file=sys.stderr)
    save_state(state)
    print(f"[cost] run ${report['cost_this_run']:.5f} · month ${report['month_spent']:.4f}"
          f"/${report['cap']:.2f} · ai={report['ai_used']}")
    OUT_DIR.mkdir(exist_ok=True)
    pv_stories = pv.digest_stories if pv else []
    pv_html = pv.html if pv else ""
    (OUT_DIR / "feed.xml").write_text(build_rss(pv_stories + stories, cfg, report, markets), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(build_html(stories, cfg, report, failures, markets, pv_html),
                                        encoding="utf-8")
    print(f"[out] wrote {OUT_DIR/'feed.xml'} and {OUT_DIR/'index.html'}")


if __name__ == "__main__":
    if "--validate-pv" in sys.argv:                 # check PV feed URLs, no AI call, no cost
        sys.exit(pv_validate_feeds(pv_resolve_config(load_config())))
    main()
