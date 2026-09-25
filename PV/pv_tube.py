"""
PV Tube — photovoltaics / energy storage / inverter news, split EU · Asia · US.

Design (same principles as the rest of the digest):
  * Allowlisted feeds only; each feed declares its OWNER so corroboration counts
    independent newsrooms, not domains (pv magazine Intl/USA/India = one owner,
    PV Tech + Energy-Storage.news = one owner).
  * 🟢 = 2+ independent owners, 🟡 = single trusted outlet, everything else hidden.
  * Sponsored / partner / press-release / webinar content is dropped before any AI call.
  * One batched Claude Haiku call per run, constrained to fetched text; every number
    and (for English sources) every proper noun in the output is checked against the
    source text. A failing bullet is dropped; a failing item falls back to an
    extractive summary (📝). Nothing unverified is shown as verified.
  * Hard cost bound: worst-case cost is computed BEFORE the call and the call is
    skipped if it could breach the monthly sub-cap.
  * Never breaks the main digest: build_pv_tube() always returns a renderable section.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests

REGIONS = ("EU", "Asia", "US")
REGION_LABEL = {"EU": "🇪🇺 EU", "Asia": "🌏 Asia", "US": "🇺🇸 US"}
UA = "Mozilla/5.0 (compatible; personal-news-digest/1.0; +https://github.com/)"
API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------- keyword filter
# Polish stems cover inflection: fotowoltaika/-i/-ce, falownik/-a/-ów, magazyn(y/ów) energii ...
KEYWORD_RX = [
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
SHADY_RX = re.compile(
    r"sponsored|advertorial|partner content|promoted|in association with|brought to you by|"
    r"\bwebinar\b|press release|paid post|"
    r"artykuł sponsorowany|materiał (partnera|promocyjny|sponsorowany)|\breklama\b|\bpromocja\b",
    re.I,
)
SHADY_URL_RX = re.compile(r"/(sponsored|partner|partners|advertorial|promo|webinars?|press-releases?)/", re.I)

# ---------------------------------------------------------------- region classifier (case-sensitive)
def _rx(words):
    return [re.compile(w) for w in words]

GEO = {
    "EU": _rx([
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
    "EU_WIDER": _rx([
        r"\bUK\b", r"\bBritain\b", r"\bBritish\b", r"\bUnited Kingdom\b", r"\bEngland\b", r"\bScotland\b",
        r"\bWielk\w* Brytani\w*", r"\bNorw\w*", r"\bSwitzerland\b", r"\bSwiss\b", r"\bSzwajcari\w*",
        r"\bUkrain\w*",
    ]),
    "Asia": _rx([
        r"\bAsia\w*", r"\bAzj\w*", r"\bASEAN\b", r"\bChin(a|ese|y|ach|ie|ą|ski\w*)\b", r"\bBeijing\b",
        r"\bShanghai\b", r"\bIndi(a|an|e|i|ach|ą|ami)\b", r"\bDelhi\b", r"\bJapan\w*", r"\bJaponi\w*",
        r"\bKore\w*", r"\bTaiwan\w*", r"\bTajwan\w*", r"\bViet\s?nam\w*", r"\bWietnam\w*",
        r"\bIndonesi\w*", r"\bPhilippin\w*", r"\bFilipin\w*", r"\bThai\w*", r"\bTajlandi\w*",
        r"\bMalaysi\w*", r"\bMalezj\w*", r"\bSingapore\b", r"\bSingapur\w*", r"\bPakistan\w*",
        r"\bBangladesh\w*", r"\bSri Lanka\w*", r"\bKazakh\w*", r"\bKazach\w*", r"\bUzbek\w*",
        r"\bTokyo\b", r"\bTokio\b",
    ]),
    "ASIA_GULF": _rx([
        r"\bSaudi\w*", r"\bArabi\w* Saudyjsk\w*", r"\bUAE\b", r"\bEmirat\w*", r"\bDubai\b",
        r"\bAbu Dhabi\b", r"\bOman\w*", r"\bQatar\w*", r"\bKatar\w*", r"\bIsrael\w*", r"\bIzrael\w*",
    ]),
    "US": _rx([
        r"\bU\.S\.(?=\W|$)", r"\bUS\b", r"\bUSA\b", r"\bUnited States\b", r"\bAmeric\w*", r"\bAmeryk\w*",
        r"\bStan\w* Zjednoczon\w*", r"\bFERC\b", r"\bDOE\b", r"\bEIA\b", r"\bSEIA\b", r"\bNREL\b",
        r"\bInflation Reduction Act\b", r"\bCAISO\b", r"\bERCOT\b", r"\bPJM\b", r"\bMISO\b", r"\bNYISO\b",
        r"\bCalifornia\w*", r"\bTexas\b", r"\bArizona\b", r"\bNevada\b", r"\bFlorida\b", r"\bNew York\b",
        r"\bMassachusetts\b", r"\bIllinois\b", r"\bIndiana\b", r"\bOhio\b", r"\bColorado\b", r"\bUtah\b",
        r"\bNorth Carolina\b", r"\bVirginia\b", r"\bNew Jersey\b", r"\bNew Mexico\b", r"\bOregon\b",
        r"\bMichigan\b", r"\bMinnesota\b", r"\bPennsylvania\b",
    ]),
}

STOP = set("""the a an and or of to in on for with by from at as is are was were be been this that
its it's their new says said will could would into over after about more than up out has have had
not but also per via i w z na do się oraz dla od po przez jest są że to ten ta jak które który która
""".split())


# ---------------------------------------------------------------- data classes
@dataclass
class Item:
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
class Story:
    items: list
    region: str
    badge: str                     # 🟢 / 🟡
    key: str
    summary: dict | None = None    # {"title","main_point","bullets","claim_type"}
    mode: str = "ai"               # "ai" | "extractive"


@dataclass
class PVTubeResult:
    html: str
    used_urls: set
    cost_usd: float
    status: dict


# ---------------------------------------------------------------- helpers
def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_url(u: str) -> str:
    p = urlparse(u)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def _domain_ok(link: str, allowed: list[str]) -> bool:
    host = urlparse(link).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allowed)


def matches_keywords(text: str) -> bool:
    return any(rx.search(text) for rx in KEYWORD_RX)


def is_shady(title: str, text: str, link: str, categories: list[str]) -> bool:
    blob = " ".join([title, " ".join(categories)])
    if SHADY_RX.search(blob) or SHADY_URL_RX.search(link):
        return True
    # Sponsored markers often sit only at the very start of the body
    return bool(SHADY_RX.search(text[:160]))


def classify_region(title: str, text: str, default: str | None, cfg: dict) -> str | None:
    scores = {r: 0 for r in REGIONS}
    groups = {"EU": ["EU"] + (["EU_WIDER"] if cfg.get("eu_include_wider_europe", True) else []),
              "Asia": ["Asia"] + (["ASIA_GULF"] if cfg.get("asia_include_gulf", False) else []),
              "US": ["US"]}
    first_pos = {}
    for region, keys in groups.items():
        for k in keys:
            for rx in GEO[k]:
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


def _tok(text: str) -> dict:
    words = re.findall(r"[a-ząćęłńóśźż0-9]{3,}", text.lower())
    tf = {}
    for w in words:
        if w not in STOP:
            tf[w] = tf.get(w, 0) + 1
    return tf


_UNIT_RX = re.compile(r"(\d[\d\s\u00a0.,]*\d|\d)\s?(GWh|MWh|kWh|TWh|GW|MW|kW|%|zł|PLN|EUR|€|USD|\$|/W|W)(?![A-Za-z])")


def _fingerprint(text: str) -> set[str]:
    """Language-independent story fingerprint: numbers with units ('1,2 GW' == '1.2 GW')."""
    fp = set()
    for num, unit in _UNIT_RX.findall(text):
        vals = _numbers(num)
        if vals:
            unit = {"zł": "PLN", "€": "EUR", "$": "USD"}.get(unit, unit)
            fp.add(f"{next(iter(vals))}{unit}")
    return fp


def _similar(a: "Item", b: "Item", thr: float) -> bool:
    cos = _cos(a.tokens, b.tokens)
    if cos >= thr:
        return True
    shared = len(a.fp & b.fp)                 # cross-language match (PL ↔ EN) via shared figures
    return shared >= 2 or (shared == 1 and cos >= 0.10)


def _cos(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


# ---------------------------------------------------------------- numeric / entity grounding
_NUM_RX = re.compile(r"\d[\d\s\u00a0\u202f.,]*\d|\d")


def _numbers(text: str) -> set[str]:
    """Normalise EN (1,200.5) and PL (1 200,5) formats to a canonical form."""
    out = set()
    for raw in _NUM_RX.findall(text):
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


_ENTITY_OK = {"EU", "US", "U.S.", "UK", "PV", "BESS", "GW", "MW", "GWh", "MWh", "kW", "kWh", "TWh",
              "AC", "DC", "CEO", "Asia", "Europe", "European", "Chinese", "American", "AI", "The", "A", "An",
              "In", "On", "At", "It", "Its", "This", "That", "These", "Those", "However", "According",
              "Meanwhile", "Also", "While", "After", "Before", "Despite", "Both", "Each", "Most", "Some",
              "Many", "Several", "Overall", "Total", "Figures", "Data", "Sources", "Source", "Analysts",
              "Officials"}


def grounded(claim: str, source_text: str, check_entities: bool) -> bool:
    src_nums = _numbers(source_text)
    # strip [S1]-style refs before checking numbers
    claim_nums = _numbers(re.sub(r"\[S\d+\]", "", claim))
    if not claim_nums <= src_nums:
        return False
    if check_entities:
        low = source_text.lower()
        words = claim.split()
        for i, raw in enumerate(words):
            w = re.sub(r"(['’]s)$", "", raw.strip(".,;:()\"'’“”"))
            # sentence-initial words ARE checked (that's where "Tesla supplied..." hides);
            # ordinary words pass because the model paraphrases source vocabulary.
            if len(w) < 3 or not w[0].isupper() or w in _ENTITY_OK:
                continue
            if not all(part.lower() in low for part in w.split("-") if len(part) >= 3):
                return False
    return True


# ---------------------------------------------------------------- state
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k in ("ledger", "cache", "shown", "health", "alerts"):
        st.setdefault(k, {})
    return st


def save_state(path: str, st: dict, today: str) -> None:
    cutoff = (datetime.fromisoformat(today) - timedelta(days=10)).date().isoformat()
    st["cache"] = {k: v for k, v in st["cache"].items() if v.get("date", "") >= cutoff}
    st["shown"] = {k: v for k, v in st["shown"].items() if v >= cutoff}
    months = sorted(st["ledger"])[-3:]
    st["ledger"] = {m: st["ledger"][m] for m in months}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)          # atomic: a crash can't leave a half-written ledger


# ---------------------------------------------------------------- fetch
def _fetch_one(src: dict, timeout: tuple) -> tuple[dict, list, str | None]:
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=timeout)
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


def fetch_all(cfg: dict, state: dict, now: datetime) -> tuple[list[Item], dict]:
    window = timedelta(hours=cfg.get("window_hours", 36))
    items, stats = [], {"fetched": 0, "keyword": 0, "shady": 0, "offdomain": 0, "noregion": 0}
    timeout = (5, cfg.get("fetch_timeout_s", 15))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda s: _fetch_one(s, timeout), cfg["sources"]))
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
            title = _clean(e.get("title", ""))
            body = _clean((e.get("content") or [{}])[0].get("value", "") or e.get("summary", ""))
            link = e.get("link", "")
            cats = [c.get("term", "") for c in e.get("tags", []) or []]
            if not title or not link:
                continue
            if not _domain_ok(link, allowed):
                stats["offdomain"] += 1
                continue
            if is_shady(title, body, link, cats):
                stats["shady"] += 1
                continue
            if not matches_keywords(f"{title} {body[:2000]} {' '.join(cats)}"):
                continue
            stats["keyword"] += 1
            region = classify_region(title, body, src.get("region"), cfg)
            if region not in REGIONS:
                stats["noregion"] += 1
                continue
            it = Item(src["name"], src["owner"], src.get("lang", "en"), bool(src.get("trust_single")),
                      src.get("region"), title, link, body[:4000], pub, region)
            it.tokens = _tok(f"{title} {title} {body[:1200]}")
            it.fp = _fingerprint(f"{title} {body[:1500]}")
            items.append(it)
    return items, stats


# ---------------------------------------------------------------- cluster + corroborate + rank
def build_stories(items: list[Item], cfg: dict, state: dict, today: str) -> list[Story]:
    thr = cfg.get("cluster_similarity", 0.28)
    items = sorted(items, key=lambda i: i.published, reverse=True)
    clusters: list[list[Item]] = []
    for it in items:
        for c in clusters:
            if c[0].region == it.region and _similar(c[0], it, thr):
                c.append(it)
                break
        else:
            clusters.append([it])

    stories = []
    for c in clusters:
        # de-duplicate the same URL syndicated twice
        seen, uniq = set(), []
        for it in c:
            if _norm_url(it.link) not in seen:
                seen.add(_norm_url(it.link))
                uniq.append(it)
        owners = {i.owner for i in uniq}
        if len(owners) >= 2:
            badge = "🟢"
        elif any(i.trust_single for i in uniq):
            badge = "🟡"
        else:
            continue
        urls = sorted(_norm_url(i.link) for i in uniq)
        # anti-fatigue: skip if every URL was already shown on an EARLIER day
        if all(state["shown"].get(u, today) < today for u in urls):
            continue
        key = hashlib.sha1("|".join(urls).encode()).hexdigest()[:16]
        stories.append(Story(uniq, uniq[0].region, badge, key))

    def score(s: Story):
        age_h = (datetime.now(timezone.utc) - s.items[0].published).total_seconds() / 3600
        return (2 if s.badge == "🟢" else 0) + len({i.owner for i in s.items}) - age_h / 24

    per = cfg.get("max_per_region", 3)
    out = []
    for r in REGIONS:
        out += sorted([s for s in stories if s.region == r], key=score, reverse=True)[:per]
    return out


# ---------------------------------------------------------------- summarise
SYSTEM_PROMPT = """You summarise solar PV, energy-storage and inverter news for a daily digest.
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


def _source_block(story: Story, max_sources: int, chars: int) -> tuple[str, str, list]:
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


def _call_claude(prompt: str, cfg: dict, api_key: str) -> tuple[str | None, dict, str | None]:
    body = {"model": cfg.get("model", "claude-haiku-4-5-20251001"),
            "max_tokens": cfg.get("max_output_tokens", 2500), "temperature": 0,
            "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(2):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=90)
        except requests.RequestException as e:
            err = f"{type(e).__name__}"
            if attempt == 0:
                time.sleep(5)
                continue
            return None, {}, err
        if r.status_code in (429, 500, 502, 503, 529) and attempt == 0:
            time.sleep(min(float(r.headers.get("retry-after", "10") or 10), 20))
            continue
        if r.status_code != 200:
            return None, {}, f"HTTP {r.status_code}: {r.text[:150]}"
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text, data.get("usage", {}), None
    return None, {}, "retries exhausted"


def _parse_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _extractive(story: Story) -> dict:
    it = story.items[0]
    sentences = re.split(r"(?<=[.!?])\s+", it.text)
    main = next((s for s in sentences if len(s) > 40), it.text[:220])[:300]
    return {"title": it.title, "main_point": main, "bullets": [], "claim_type": "reported"}


def summarise(stories: list[Story], cfg: dict, state: dict, month: str, today: str,
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
    blocks = {s.key: _source_block(s, ms, chars) for s in todo}
    prompt = "\n\n=====\n\n".join(f"STORY id={k}\n{b[0]}" for k, b in blocks.items())

    p_in, p_out = cfg.get("price_in_per_mtok", 1.0), cfg.get("price_out_per_mtok", 5.0)
    est_in_tokens = (len(SYSTEM_PROMPT) + len(prompt)) / 2.5          # conservative for Polish text
    worst = 2 * (est_in_tokens * p_in + cfg.get("max_output_tokens", 2500) * p_out) / 1e6  # 2 = one retry
    spent = state["ledger"].get(month, 0.0)
    cap = cfg.get("monthly_cost_cap_usd", 0.75)

    text = None
    if not api_key:
        notes.append("no ANTHROPIC_API_KEY — extractive mode")
    elif spent + worst > cap:
        notes.append(f"budget guard: ${spent:.2f} spent + ${worst:.3f} worst case > ${cap:.2f} cap — extractive mode")
    else:
        text, usage, err = _call_claude(prompt, cfg, api_key)
        if usage:
            cost = (usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out) / 1e6
            state["ledger"][month] = round(spent + cost, 6)
        if err:
            notes.append(f"AI call failed ({err}) — extractive mode")

    by_id = {str(d.get("id")): d for d in _parse_json_array(text) if isinstance(d, dict)}
    dropped_bullets = 0
    for s in todo:
        d, src_text = by_id.get(s.key), blocks[s.key][1]
        check_ent = all(i.lang == "en" for i in s.items)
        ok = (d and isinstance(d.get("title"), str) and isinstance(d.get("main_point"), str)
              and isinstance(d.get("bullets"), list)
              and grounded(d["title"], src_text, False)          # headlines: numbers only
              and grounded(d["main_point"], src_text, check_ent))
        if ok:
            bullets = [b for b in d["bullets"][:6] if isinstance(b, str) and grounded(b, src_text, check_ent)]
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
            s.summary, s.mode = _extractive(s), "extractive"
        # Cache whenever the model actually answered (incl. grounding failures → no paid retry loop).
        # If the call never happened (budget/API down), don't cache: the next run may still use AI.
        if text is not None:
            state["cache"][s.key] = {"summary": s.summary, "mode": s.mode, "date": today}
    if dropped_bullets:
        notes.append(f"{dropped_bullets} bullet(s) removed by grounding check")
    return cost, notes


# ---------------------------------------------------------------- alerts (deduplicated → no alert fatigue)
def alerts(cfg: dict, state: dict, month: str) -> list[str]:
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


def _emit(msgs: list[str], summary_md: str) -> None:
    for m in msgs:
        print(f"::warning title=PV Tube::{m}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary_md + "\n")


# ---------------------------------------------------------------- render
CLAIM_LABEL = {"official_data": "📊 official data", "company_claim": "🏷️ company claim", "reported": ""}


def render(stories: list[Story], footer: str, cfg: dict) -> str:
    e = html.escape
    out = ['<section id="pv-tube" class="section pv-tube">',
           '<h2>☀️ PV Tube</h2>',
           '<p class="section-sub">Photovoltaics · energy storage · inverters</p>']
    for r in REGIONS:
        out.append(f'<h3 class="region">{REGION_LABEL[r]}</h3>')
        rs = [s for s in stories if s.region == r]
        if not rs:
            out.append(f'<p class="empty">No new verified PV news in the last {cfg.get("window_hours", 36)} h.</p>')
            continue
        for s in rs:
            sm = s.summary
            tags = [s.badge]
            if s.mode == "extractive":
                tags.append("📝")
            label = CLAIM_LABEL.get(sm.get("claim_type", ""), "")
            out.append('<details class="article">')
            out.append(f'<summary><span class="badge">{" ".join(tags)}</span> {e(sm["title"])}</summary>')
            out.append(f'<p class="main-point">{e(sm["main_point"])}'
                       + (f' <span class="claim">{label}</span>' if label else "") + "</p>")
            if sm["bullets"]:
                out.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in sm["bullets"]) + "</ul>")
            links = " · ".join(
                f'<a href="{e(i.link, quote=True)}" rel="noopener noreferrer">{e(i.source)}</a>'
                + (" 🇵🇱" if i.lang == "pl" else "") for i in s.items)
            out.append(f'<p class="sources">Sources: {links}</p>')
            out.append("</details>")
    out.append(f'<p class="pv-status">{e(footer)}</p>')
    out.append("</section>")
    return "\n".join(out)


# ---------------------------------------------------------------- entry point
def build_pv_tube(cfg: dict, state_path: str = "data/pv_tube_state.json",
                  api_key: str | None = None) -> PVTubeResult:
    """Never raises: on any unexpected error returns a minimal section so the digest still builds."""
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    now = datetime.now(timezone.utc)
    today, month = now.astimezone(tz).date().isoformat(), now.astimezone(tz).strftime("%Y-%m")
    api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
    state = load_state(state_path)
    try:
        if not cfg.get("enabled", True):
            return PVTubeResult("", set(), 0.0, {"disabled": True})
        items, stats = fetch_all(cfg, state, now)
        stories = build_stories(items, cfg, state, today)
        cost, notes = summarise(stories, cfg, state, month, today, api_key)
        for s in stories:
            for i in s.items:
                state["shown"].setdefault(_norm_url(i.link), today)

        healthy = sum(1 for s in cfg["sources"] if state["health"].get(s["name"], {}).get("fails", 1) == 0)
        spent, cap = state["ledger"].get(month, 0.0), cfg.get("monthly_cost_cap_usd", 0.75)
        footer = (f"🟢 2+ independent outlets · 🟡 single trusted outlet · 📝 extractive (no AI) · "
                  f"Feeds {healthy}/{len(cfg['sources'])} healthy · "
                  f"Filtered out: {stats['shady']} sponsored/PR, {stats['offdomain']} off-domain · "
                  f"AI spend {month}: ${spent:.2f} / ${cap:.2f}")
        warn = alerts(cfg, state, month)
        md = (f"### PV Tube\n- stories: {len(stories)} ({sum(s.mode == 'ai' for s in stories)} AI, "
              f"{sum(s.mode == 'extractive' for s in stories)} extractive)\n- run cost: ${cost:.4f}; "
              f"month: ${spent:.3f} / ${cap:.2f}\n- stats: {stats}\n"
              + "".join(f"- note: {n}\n" for n in notes) + "".join(f"- ⚠️ {w}\n" for w in warn))
        _emit(warn, md)
        page = render(stories, footer, cfg)
        used = {i.link for s in stories for i in s.items}
        return PVTubeResult(page, used, cost, {"stats": stats, "notes": notes, "alerts": warn})
    except Exception as ex:                                            # isolation boundary
        msg = f"PV Tube skipped this run: {type(ex).__name__}: {str(ex)[:150]}"
        _emit([msg], f"### PV Tube\n- ⚠️ {msg}\n")
        page = ('<section id="pv-tube" class="section pv-tube"><h2>☀️ PV Tube</h2>'
                '<p class="empty">PV Tube is temporarily unavailable; the rest of the digest is unaffected.</p></section>')
        return PVTubeResult(page, set(), 0.0, {"error": msg})
    finally:
        save_state(state_path, state, today)


# ---------------------------------------------------------------- one-off feed validator
def validate_feeds(cfg: dict) -> int:
    """python pv_tube.py --validate : run once in GitHub Actions after adding/changing sources."""
    now, bad = datetime.now(timezone.utc), 0
    print(f"{'source':28} {'status':8} {'items':>5} {'<7d':>4} {'kw':>4} {'shady':>5}  latest")
    for src in cfg["sources"]:
        _, entries, err = _fetch_one(src, (5, 20))
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
            ti, body = _clean(e.get("title", "")), _clean(e.get("summary", ""))
            kw += matches_keywords(f"{ti} {body}")
            shady += is_shady(ti, body, e.get("link", ""), [c.get("term", "") for c in e.get("tags", []) or []])
        status = "OK" if recent else "STALE"
        bad += status != "OK"
        print(f"{src['name'][:28]:28} {status:8} {len(entries):>5} {recent:>4} {kw:>4} {shady:>5}  "
              f"{latest.date() if latest else '-'}")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    import yaml
    with open(os.environ.get("FEEDS_YAML", "feeds.yaml"), encoding="utf-8") as f:
        conf = yaml.safe_load(f)["pv_tube"]
    if "--validate" in sys.argv:
        sys.exit(validate_feeds(conf))
    res = build_pv_tube(conf)
    print(res.html)
