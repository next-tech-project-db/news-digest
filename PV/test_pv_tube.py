import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pv_tube as pv  # noqa: E402

NOW = datetime.now(timezone.utc)


def rss(items, host):
    body = "".join(
        f"<item><title>{t}</title><link>https://{host}/{slug}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=h))}</pubDate>"
        f"<description><![CDATA[{d}]]></description>{''.join(f'<category>{c}</category>' for c in cats)}</item>"
        for t, slug, h, d, cats in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


FEEDS = {
    "https://www.pv-magazine.com/feed/": rss([
        ("Poland adds 1.2 GW of PV in first half", "pl-h1", 3,
         "Poland installed 1.2 GW of new photovoltaic capacity in the first half, grid operator PSE said. Prosumer installations fell 15%.", []),
        ("Sponsored: Meet our new inverter line", "promo", 2, "Sponsored content about inverters.", ["Sponsored"]),
        ("Chinese module prices drop to $0.08/W", "cn-prices", 5,
         "Solar module prices in China fell to $0.08/W this week, according to InfoLink. TOPCon modules led the decline.", []),
        ("Australia hits record rooftop solar", "au", 4, "Rooftop solar power output reached a record in Australia.", []),
        ("Off-domain syndicated PV item", "x", 3, "PV text", []),
    ], "pv-magazine.com"),
    "https://www.gramwzielone.pl/rss": rss([
        ("W I półroczu przybyło 1,2 GW fotowoltaiki w Polsce", "gw-h1", 6,
         "Według PSE w pierwszym półroczu w Polsce przybyło 1,2 GW mocy w fotowoltaice. Liczba mikroinstalacji spadła o 15%.", []),
        ("Artykuł sponsorowany: falowniki hybrydowe", "sp", 3, "Materiał partnera o falownikach.", []),
    ], "gramwzielone.pl"),
    "https://pv-magazine-usa.com/feed/": rss([
        ("Texas battery storage fleet passes 10 GW", "tx-bess", 8,
         "ERCOT data show battery energy storage capacity in Texas passed 10 GW this month.", []),
    ], "pv-magazine-usa.com"),
    "https://www.utilitydive.com/feeds/news/": rss([
        ("Texas grid now counts over 10 GW of battery storage", "ercot-10gw", 10,
         "Battery storage capacity on the ERCOT grid in Texas has surpassed 10 GW, ERCOT said.", []),
        ("FERC approves new transmission rule", "ferc", 5, "FERC approved a transmission planning rule.", []),
    ], "utilitydive.com"),
}


class Resp:
    def __init__(self, content=b"", status=200, js=None, headers=None):
        self.content, self.status_code, self._js, self.headers = content, status, js, headers or {}
        self.text = json.dumps(js) if js else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise pv.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._js


@pytest.fixture
def cfg():
    root = os.path.join(os.path.dirname(__file__), "..")
    path = next(p for p in (os.path.join(root, "feeds.yaml"), os.path.join(root, "feeds.pv_tube.yaml"))
                if os.path.exists(p) and "pv_tube" in (yaml.safe_load(open(p)) or {}))
    c = yaml.safe_load(open(path))["pv_tube"]
    c["sources"] = [s for s in c["sources"] if s["url"] in FEEDS] + [
        {"name": "Dead feed", "url": "https://dead.example/feed", "owner": "dead", "region": "EU", "trust_single": True}]
    return c


def fake_get(url, **kw):
    if url in FEEDS:
        return Resp(FEEDS[url])
    return Resp(b"", 404)


def make_post(response_builder, calls):
    def fake_post(url, headers, json, timeout):
        calls.append(json)
        prompt = json["messages"][0]["content"]
        ids = [l.split("id=")[1].strip() for l in prompt.splitlines() if l.startswith("STORY id=")]
        return Resp(js={"content": [{"type": "text", "text": response_builder(ids, prompt)}],
                        "usage": {"input_tokens": 3000, "output_tokens": 800}})
    return fake_post


def good_builder(ids, prompt):
    out = []
    for i in ids:
        block = prompt.split(f"STORY id={i}")[1].split("=====")[0]
        if "Poland" in block or "Polsce" in block:
            out.append({"id": i, "title": "Poland added 1.2 GW of solar in the first half",
                        "main_point": "Poland installed 1.2 GW of new PV capacity in the first half, according to PSE.",
                        "bullets": ["Prosumer installations fell 15% [S1].",
                                    "Growth reached 7 GW according to analysts.",          # hallucinated number
                                    "Figures come from grid operator PSE [S1]."],
                        "claim_type": "official_data"})
        elif "Texas" in block:
            out.append({"id": i, "title": "Texas battery fleet passes 10 GW",
                        "main_point": "Battery storage on the ERCOT grid passed 10 GW.",
                        "bullets": ["ERCOT data confirm the milestone [S1].",
                                    "Tesla supplied most of the systems."],                # hallucinated entity
                        "claim_type": "official_data"})
        else:
            out.append({"id": i, "title": "Chinese module prices fell to $0.08/W",
                        "main_point": "Module prices in China dropped to $0.08/W, InfoLink data show.",
                        "bullets": ["TOPCon modules led the decline [S1]."], "claim_type": "reported"})
    return "```json\n" + json.dumps(out) + "\n```"


def test_full_run(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv.requests, "post", make_post(good_builder, calls))
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)
    state = tmp_path / "s.json"
    r = pv.build_pv_tube(cfg, str(state), api_key="k")
    h = r.html
    assert len(calls) == 1                                           # one batched call
    assert "Sponsored" not in h and "sponsorowany" not in h           # shady filtered
    assert "Australia" not in h                                       # out-of-scope region dropped
    assert "Off-domain" not in h
    assert "FERC approves" not in h                                   # no PV keyword
    assert "7 GW" not in h                                            # numeric hallucination removed
    assert "Tesla" not in h                                           # entity hallucination removed
    assert "Poland added 1.2 GW" in h and "W I półroczu" not in h.split("<summary>",1)[1].split("</summary>")[0]
    assert h.count("🟢") >= 3                                         # PL (EN+PL owners) and TX corroborated (+legend)
    assert "🟡" in h                                                  # China price story single-source
    assert h.index("🇪🇺 EU") < h.index("🌏 Asia") < h.index("🇺🇸 US")
    assert "Chinese module prices" in h.split("🌏 Asia")[1].split("🇺🇸 US")[0]
    assert "📊 official data" in h
    assert r.cost_usd == pytest.approx((3000 * 1 + 800 * 5) / 1e6)
    s = json.loads(state.read_text())
    assert s["health"]["Dead feed"]["fails"] == 1

    # same-day rerun: served from cache, zero AI calls, same stories
    r2 = pv.build_pv_tube(cfg, str(state), api_key="k")
    assert len(calls) == 1 and r2.cost_usd == 0 and "Texas battery" in r2.html


def test_next_day_no_repeats(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv.requests, "post", make_post(good_builder, calls))
    state = tmp_path / "s.json"
    pv.build_pv_tube(cfg, str(state), api_key="k")
    s = json.loads(state.read_text())
    s["shown"] = {k: "2000-01-01" for k in s["shown"]}                 # pretend shown on an earlier day
    state.write_text(json.dumps(s))
    r = pv.build_pv_tube(cfg, str(state), api_key="k")
    assert "No new verified PV news" in r.html and "Texas battery" not in r.html


def test_budget_guard_and_alert_once(cfg, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv.requests, "post", make_post(good_builder, calls))
    state = tmp_path / "s.json"
    month = datetime.now(pv.ZoneInfo("Europe/Warsaw")).strftime("%Y-%m")
    state.write_text(json.dumps({"ledger": {month: 0.749}}))
    r = pv.build_pv_tube(cfg, str(state), api_key="k")
    assert calls == [] and "📝" in r.html                              # no call, extractive
    out = capsys.readouterr().out
    assert out.count("80% of cap") == 1
    pv.build_pv_tube(cfg, str(state), api_key="k")
    assert "80% of cap" not in capsys.readouterr().out                # deduplicated


def test_api_failure_and_garbage_fall_back(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)
    monkeypatch.setattr(pv.requests, "post", lambda *a, **k: Resp(status=529, js={"error": "overloaded"}))
    r = pv.build_pv_tube(cfg, str(tmp_path / "a.json"), api_key="k")
    assert "📝" in r.html and "unavailable" not in r.html
    monkeypatch.setattr(pv.requests, "post", make_post(lambda ids, p: "sorry, not JSON", []))
    r = pv.build_pv_tube(cfg, str(tmp_path / "b.json"), api_key="k")
    assert "📝" in r.html


def test_crash_isolated(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "fetch_all", lambda *a: 1 / 0)
    r = pv.build_pv_tube(cfg, str(tmp_path / "c.json"), api_key="k")
    assert "temporarily unavailable" in r.html


def test_xss_escaped(cfg, tmp_path, monkeypatch):
    evil = {"https://www.pv-magazine.com/feed/": rss([(
        "PV &amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt; in Poland", "evil", 1,
        "Photovoltaic news in Poland. <img src=x onerror=alert(2)>", [])],
        "pv-magazine.com")}
    monkeypatch.setattr(pv.requests, "get", lambda url, **k: Resp(evil.get(url, b""), 200 if url in evil else 404))
    r = pv.build_pv_tube(cfg, str(tmp_path / "x.json"), api_key=None)
    assert "<script" not in r.html and "<img" not in r.html and "PV Tube" in r.html
    assert "&lt;script&gt;" in r.html                                  # double-encoded payload shown as inert text


@pytest.mark.parametrize("text,ok", [
    ("Poland (1 200,5 MW)", True), ("1,200.5 MW", True), ("1.200,5 MW", True), ("1200.5", True), ("1300 MW", False)])
def test_number_normalisation(text, ok):
    assert pv.grounded(text, "Moc wyniosła 1 200,5 MW", False) is ok


@pytest.mark.parametrize("text,expected", [
    ("Poland's grid operator reports", "EU"), ("India tender for PV", "Asia"), ("Indiana solar project", "US"),
    ("U.S. tariffs on Chinese modules", "US"), ("Rekord fotowoltaiki w Polsce", "EU"), ("Saudi PV auction", None),
    ("Chiny zwiększają moce", "Asia"), ("UK battery storage", "EU")])
def test_regions(text, expected, cfg):
    assert pv.classify_region(text, "", None, cfg) == expected


@pytest.mark.parametrize("text,ok", [
    ("Nowe falowniki hybrydowe", True), ("Magazynów energii przybywa", True), ("PV market", True),
    ("Solar storm hits satellites", False), ("NPV of the deal", False), ("solar module prices", True)])
def test_keywords(text, ok):
    assert pv.matches_keywords(text) is ok
