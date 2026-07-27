#!/usr/bin/env python3
"""
Daily Briefing — personal, fact-aware, interest-routed news digest.

Flow: fetch curated RSS -> window+dedupe -> cluster across outlets (corroboration)
-> route each story to a category by matched interests -> gate/verify
-> summarize (constrained to fetched text) -> cost-capped -> emit RSS + collapsible HTML.

Categories are TOPIC-based (Business / Technology / Health), driven by interest
keywords. Anything major that matches none goes to Breaking News (headline reserve).
Health is gated: a story only enters Health if the cluster includes a research outlet.
"""

import os, re, sys, json, html, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[llm] request failed, using raw summaries: {e}", file=sys.stderr)
        return None, 0, 0
    u = data.get("usageMetadata", {})
    try:
        parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        return parsed, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
    except Exception as e:
        print(f"[llm] bad JSON, using raw summaries: {e}", file=sys.stderr)
        return None, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)


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


def build_html(stories, cfg, report, failures, markets=None):
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
<main>{build_markets_panel(markets)}{''.join(blocks)}{fail}</main></body></html>"""


# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    state = load_state()
    if state.get("month") != current_month():
        state = {"month": current_month(), "spent_usd": 0.0, "requests": 0}
    items, failures = fetch_sources(cfg)
    print(f"[fetch] {len(items)} items, {len(failures)} feed failures")
    clusters = cluster_items(items, cfg["settings"]["cluster_similarity"])
    stories = rank_and_select(clusters, cfg)
    print(f"[rank] {len(clusters)} clusters -> {len(stories)} stories")
    report = summarize(stories, cfg, state)
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
    (OUT_DIR / "feed.xml").write_text(build_rss(stories, cfg, report, markets), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(build_html(stories, cfg, report, failures, markets), encoding="utf-8")
    print(f"[out] wrote {OUT_DIR/'feed.xml'} and {OUT_DIR/'index.html'}")


if __name__ == "__main__":
    main()
