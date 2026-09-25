#!/usr/bin/env python3
"""
Espoon päätösseuranta – prototyyppi
===================================

Lukee Espoon Dynasty-palvelusta (espoo.oncloudos.com) toimielinten esityslistat
ja kokousasiat, pisteyttää ne avainsanasäännöillä ja valinnaisesti kielimallilla,
ja tuottaa koosteen Aaltoa, AYY:tä, Otaniemeä ja opiskelijoita koskevista asioista.

Pikakäyttö:
    pip install -r requirements.txt
    python espoo_seuranta.py --kuiva -v                      # ei tallennusta, tulostus ruudulle
    python espoo_seuranta.py --toimielin Kaupunginhallitus   # vain KH
    python espoo_seuranta.py --kokous 20261727               # yksittäinen kokous id:llä
    python espoo_seuranta.py --viranhaltijat                 # lisäksi viranhaltijapäätösten otsikot

Ympäristömuuttujat (kaikki valinnaisia):
    ANTHROPIC_API_KEY      kielimalliluokittelu päälle
    SEURANTA_MALLI         oletus: claude-haiku-4-5-20251001
    TELEGRAM_BOT_TOKEN     koosteen lähetys Telegramiin
    TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("seuranta")

HOST = "https://espoo.oncloudos.com"
BASE = f"{HOST}/cgi/DREQUEST.PHP"
UA = "AYY-paatosseuranta/0.1 (Aalto-yliopiston ylioppilaskunta; prototyyppi)"
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DOCTYPE_RE = re.compile(r"(Esityslista|Pöytäkirja|Föredragningslista|Protokoll)", re.I)
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Tietorakenteet
# ---------------------------------------------------------------------------
@dataclass
class Body:
    name: str
    body_id: str
    latest_meeting_id: str | None = None
    latest_doctype: str | None = None
    latest_date: dt.date | None = None


@dataclass
class MeetingRef:
    meeting_id: str
    doctype: str | None
    date: dt.date | None


@dataclass
class ItemRef:
    item_id: str   # esim. "20261727-4"
    number: str    # asianumero kokouksessa, esim. "4"
    title: str


@dataclass
class Meeting:
    meeting_id: str
    body: str
    doctype: str
    date: dt.date | None
    items: list[ItemRef]
    pdf_url: str | None = None


@dataclass
class ItemDetail:
    text: str
    attachments: list[str]
    asianumero: str | None


@dataclass
class Result:
    item_id: str
    doctype: str
    body: str
    date: dt.date | None
    title: str
    url: str
    score: int = 0
    matches: list[dict] = field(default_factory=list)
    llm: dict | None = None
    category: str = "ei"   # suora / epäsuora / tarkista / ei


# ---------------------------------------------------------------------------
# Apufunktiot
# ---------------------------------------------------------------------------
SENT_END = re.compile(r"[.!?](?=\s+[A-ZÅÄÖ])|\n")


def sentence_at(text: str, start: int, end: int, maxlen: int = 200) -> tuple[str, str, str]:
    """Palauttaa osuman sisältävän lauseen kolmena osana (ennen, osuma, jälkeen).
    Liian pitkä lause lyhennetään osuman ympäriltä."""
    left = 0
    for m in SENT_END.finditer(text):
        if m.end() > start:
            break
        left = m.end()
    m = SENT_END.search(text, end)
    right = m.end() if m else len(text)
    pre = re.sub(r"\s+", " ", text[left:start]).lstrip(" -–•")
    hit = re.sub(r"\s+", " ", text[start:end])
    post = re.sub(r"\s+", " ", text[end:right]).rstrip()
    room = max(40, maxlen - len(hit))
    if len(pre) + len(post) > room:
        keep_pre = min(len(pre), room // 2)
        keep_post = room - keep_pre
        if len(post) < keep_post:
            keep_pre = room - len(post)
        if len(pre) > keep_pre:
            pre = "…" + pre[len(pre) - keep_pre:].split(" ", 1)[-1] + " "
        if len(post) > keep_post:
            post = post[:keep_post].rsplit(" ", 1)[0] + "…"
    return pre, hit, post


def fi_date(d: dt.date | None) -> str:
    return f"{d.day}.{d.month}.{d.year}" if d else ""


def parse_date(text: str | None) -> dt.date | None:
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def qs_id(href: str) -> str | None:
    q = parse_qs(urlparse(href).query)
    return (q.get("id") or [None])[0]


def qs_page(href: str) -> str | None:
    q = parse_qs(urlparse(href).query)
    return (q.get("page") or [None])[0]


def clean_text(raw: str) -> str:
    lines = [ln.replace("\xa0", " ").strip() for ln in raw.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(re.sub(r"\s+", " ", ln))
    return "\n".join(out).strip()


def row_of(tag):
    tr = tag.find_parent("tr")
    return tr if tr is not None else tag.parent


def item_url(item_id: str) -> str:
    return f"{BASE}?page=meetingitem&id={item_id}"


def meeting_url(meeting_id: str) -> str:
    return f"{BASE}?page=meeting&id={meeting_id}"


# ---------------------------------------------------------------------------
# Jäsentimet (puhtaita funktioita – testattavissa ilman verkkoa)
# ---------------------------------------------------------------------------
def parse_handlers(soup: BeautifulSoup) -> list[Body]:
    """Toimielinlistaus (page=meeting_handlers)."""
    bodies: dict[str, Body] = {}
    for a in soup.find_all("a", href=True):
        if qs_page(a["href"]) != "meetings":
            continue
        bid = qs_id(a["href"])
        name = a.get_text(" ", strip=True)
        if not bid or not name:
            continue
        body = Body(name=name, body_id=bid)
        row = row_of(a)
        for a2 in row.find_all("a", href=True):
            if qs_page(a2["href"]) == "meeting":
                body.latest_meeting_id = qs_id(a2["href"])
                body.latest_doctype = a2.get_text(" ", strip=True) or None
                break
        body.latest_date = parse_date(row.get_text(" ", strip=True))
        bodies[bid] = body
    return list(bodies.values())


def parse_meeting_list(soup: BeautifulSoup) -> list[MeetingRef]:
    """Yhden toimielimen kokouslistaus (page=meetings&id=...)."""
    refs: dict[str, MeetingRef] = {}
    for a in soup.find_all("a", href=True):
        if qs_page(a["href"]) != "meeting":
            continue
        mid = qs_id(a["href"])
        if not mid or mid in refs:
            continue
        row_text = row_of(a).get_text(" ", strip=True)
        m = DOCTYPE_RE.search(a.get_text(" ", strip=True)) or DOCTYPE_RE.search(row_text)
        refs[mid] = MeetingRef(mid, m.group(1).capitalize() if m else None, parse_date(row_text))
    return list(refs.values())


def parse_meeting(soup: BeautifulSoup, meeting_id: str) -> Meeting:
    """Kokoussivu (page=meeting&id=...): otsikko ja kokousasiat."""
    h1 = soup.find("h1")
    head = h1.get_text(" ", strip=True) if h1 else ""
    m = DOCTYPE_RE.search(head)
    doctype = m.group(1).capitalize() if m else "Tuntematon"
    body = head[: m.start()].strip() if m else head
    date = parse_date(head[m.end():] if m else head)

    items: dict[str, ItemRef] = {}
    prefix = f"{meeting_id}-"
    for a in soup.find_all("a", href=True):
        if qs_page(a["href"]) != "meetingitem":
            continue
        iid = qs_id(a["href"]) or ""
        if not iid.startswith(prefix) or iid in items:
            continue
        items[iid] = ItemRef(iid, iid[len(prefix):], a.get_text(" ", strip=True))

    pdf = None
    for a in soup.find_all("a", href=True):
        if a["href"].upper().endswith(f"/KOKOUS/{meeting_id}.PDF"):
            pdf = urljoin(HOST, a["href"])
            break
    ordered = sorted(items.values(), key=lambda r: int(r.number) if r.number.isdigit() else 9999)
    return Meeting(meeting_id, body, doctype, date, ordered, pdf)


def parse_item(soup: BeautifulSoup) -> ItemDetail:
    """Kokousasian sivu (page=meetingitem&id=...): varsinainen teksti."""
    full = clean_text(soup.get_text("\n"))
    start = full.find("Kokousasian teksti")
    text = full[start + len("Kokousasian teksti"):] if start >= 0 else full
    for stop in ("Päätöshistoria", "Beslutshistoria"):
        i = text.find(stop)
        if i >= 0:
            text = text[:i]
            break
    else:
        i = text.find("Navigointi")
        if i >= 0:
            text = text[:i]
    attachments = []
    for a in soup.find_all("a", href=True):
        if "/kokous/" in a["href"].lower():
            t = a.get_text(" ", strip=True)
            if t.lower().startswith(("liite", "bilaga")):
                attachments.append(t)
    m = re.search(r"Asianumero\s+([\d/.]+)", text)
    return ItemDetail(text.strip(), attachments, m.group(1) if m else None)


def parse_official_list(soup: BeautifulSoup) -> list[dict]:
    """Viranhaltijapäätösten listaus (page=official_search)."""
    out = []
    for a in soup.find_all("a", href=True):
        if qs_page(a["href"]) != "official_decision":
            continue
        row = row_of(a)
        cells = row.find_all("td")
        who = cells[0].get_text(" ", strip=True) if cells else ""
        out.append({
            "id": qs_id(a["href"]),
            "title": a.get_text(" ", strip=True),
            "official": DATE_RE.sub("", who).strip(),
            "date": parse_date(row.get_text(" ", strip=True)),
            "url": urljoin(BASE, a["href"]),
        })
    return out


def page_matches(soup: BeautifulSoup, page: str, id_: str) -> bool:
    """Dynasty on istuntotilallinen: tarkista, että saatiin pyydetty sivu."""
    if page == "meeting":
        return any((qs_id(a["href"]) or "").startswith(f"{id_}-")
                   for a in soup.find_all("a", href=True)) or \
            f"/kokous/{id_}.pdf" in str(soup).lower()
    if page == "meetingitem":
        return f"/kokous/{id_}.pdf".lower() in str(soup).lower()
    return True


# ---------------------------------------------------------------------------
# Verkko
# ---------------------------------------------------------------------------
class Dynasty:
    def __init__(self, delay: float = 1.0, timeout: int = 30):
        self.delay, self.timeout = delay, timeout
        self._new_session()

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def get(self, page: str, id_: str = "", **params) -> BeautifulSoup:
        params = {"page": page, "id": id_, **params}
        for attempt in range(3):
            time.sleep(self.delay)
            r = self.s.get(BASE, params=params, timeout=self.timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")  # merkistö <meta>-tagista
            if not id_ or page_matches(soup, page, id_):
                return soup
            log.warning("Dynasty palautti väärän sivun (%s %s), nollataan istunto", page, id_)
            self._new_session()
        raise RuntimeError(f"Sivua {page}&id={id_} ei saatu luotettavasti")


# ---------------------------------------------------------------------------
# Pisteytys
# ---------------------------------------------------------------------------
@dataclass
class Rules:
    threshold: int
    rules: list[tuple[str, int, list[list[re.Pattern]], list[str]]]
    skip: list[re.Pattern]
    body_order: list[str]
    ignore: list[re.Pattern] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Rules":
        raw = path.read_text(encoding="utf-8-sig").replace("\t", "    ")  # sarkaimet sallitaan
        try:
            cfg = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f"rivillä {mark.line + 1}" if mark else ""
            sys.exit(f"Virhe sääntötiedostossa {path.name} {where}: {getattr(e, 'problem', e)}\n"
                     "Tarkista sisennys (kaksi välilyöntiä ja viiva) ja että kuvio on "
                     "yksinkertaisissa lainausmerkeissä.")
        def group(k):   # 'sana' tai ['sana1', 'sana2'] (kaikkien pitää löytyä)
            ks = k if isinstance(k, list) else [k]
            return [re.compile(str(x), re.I) for x in ks]
        try:
            rules = [(r["nimi"], int(r["paino"]), [group(k) for k in r["kuviot"]],
                      [str(t).lower() for t in (r.get("toimielimet") or [])])
                     for r in cfg["saannot"]]
            for k in cfg.get("ohita_ilmaukset", []) + cfg.get("ohita_otsikot", []):
                re.compile(k)
        except re.error as e:
            sys.exit(f"Virheellinen kuvio sääntötiedostossa {path.name}: {e.pattern!r} – {e}")
        except (KeyError, TypeError, ValueError) as e:
            sys.exit(f"Sääntötiedostossa {path.name} puuttuu tai on väärin kenttä: {e}. "
                     "Jokaisella säännöllä pitää olla nimi, paino ja kuviot.")
        return cls(int(cfg.get("kynnys", 3)), rules,
                   [re.compile(p, re.I) for p in cfg.get("ohita_otsikot", [])],
                   cfg.get("toimielinten_jarjestys", []),
                   [re.compile(p, re.I) for p in cfg.get("ohita_ilmaukset", [])])

    def skip_title(self, title: str) -> bool:
        return any(p.search(title.strip()) for p in self.skip)

    def _mask(self, s: str, body: str | None) -> str:
        """Korvaa ohitettavat ilmaukset ja toimielimen oman nimen välilyönneillä
        (sama pituus, jotta otteiden kohdat säilyvät)."""
        blank = lambda m: " " * len(m.group(0))
        if body:
            s = re.sub(re.escape(body.lower()), blank, s)
        for p in self.ignore:
            s = p.sub(blank, s)
        return s

    def score(self, title: str, text: str, body: str | None = None) -> tuple[int, list[dict]]:
        total, hits = 0, []
        t_low = self._mask(title.lower(), body)
        x_low = self._mask(text.lower(), body)
        body_low = (body or "").lower()
        for name, weight, groups, bodies in self.rules:
            if bodies and not any(b in body_low for b in bodies):
                continue   # sääntö koskee vain tiettyjä toimielimiä
            for grp in groups:
                t_hits = [p.search(t_low) for p in grp]
                x_hits = [p.search(x_low) for p in grp]
                if not all(t or x for t, x in zip(t_hits, x_hits)):
                    continue
                in_title = all(t_hits) and t_hits[0]
                m = in_title or t_hits[0] or x_hits[0]
                if not in_title and not t_hits[0]:
                    src, orig = x_low, text
                else:
                    src, orig = t_low, title
                    if not in_title:     # ensimmäinen sana otsikossa, loput tekstissä
                        in_title = None
                a, b = max(0, m.start() - 90), min(len(src), m.end() + 90)
                snippet = re.sub(r"\s+", " ", orig[a:b]).strip()
                total += weight + (1 if in_title else 0)
                ws = orig[:m.start()]
                w0 = len(ws) - len(re.split(r"[\s(),.;:/\"“”]", ws)[-1])
                w1 = m.end() + len(re.split(r"[\s(),.;:/\"“”]", orig[m.end():])[0])
                hits.append({"saanto": name, "osuma": m.group(0), "otsikossa": bool(in_title),
                             "sana": orig[w0:w1].strip(),
                             "lause": sentence_at(orig, w0, w1),
                             "ote": ("…" if a else "") + snippet + ("…" if b < len(src) else "")})
                break  # kukin sääntö lasketaan kerran
        return total, hits


# ---------------------------------------------------------------------------
# Kielimalli
# ---------------------------------------------------------------------------
LLM_SYSTEM = """Olet Aalto-yliopiston ylioppilaskunnan (AYY) edunvalvonta-analyytikko.
Arvioit Espoon kaupungin päätösasioita: voiko asia vaikuttaa suoraan tai epäsuorasti
Aalto-yliopistoon, AYY:hyn, Otaniemen kampusalueeseen (ml. Keilaniemi, Teekkarikylä)
tai Espoossa asuviin/opiskeleviin korkeakouluopiskelijoihin.

Suora = asia koskee nimettyä Aalto/AYY/Otaniemi-kohdetta tai nimenomaan opiskelijoita.
Epäsuora = vaikutus opiskelijoihin on uskottava mutta välillinen (esim. asuminen ja vuokrataso,
joukkoliikenne ja pyöräily Otaniemen suunnalla, kaavoitus lähialueilla, kansainväliset osaajat,
kotoutuminen, nuorten työllisyys, kulttuuri- ja liikuntatilat, kaupungin budjetti- ja
avustuslinjaukset, innovaatio- ja korkeakouluyhteistyö).
Ei = rutiiniasia tai vaikutus opiskelijoihin olisi vain teoreettinen.

Vastaa VAIN JSON-oliona ilman muuta tekstiä:
{"luokka": "suora" | "epäsuora" | "ei", "varmuus": 0.0-1.0,
 "perustelu": "enintään 2 lausetta suomeksi", "teemat": ["lyhyt teema", ...]}"""


def llm_classify(res: Result, text: str, model: str, api_key: str) -> dict | None:
    prompt = (f"Toimielin: {res.body}\nAsiakirja: {res.doctype} {res.date or ''}\n"
              f"Otsikko: {res.title}\n\nTeksti (lyhennetty):\n{text[:4000]}")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 300, "system": LLM_SYSTEM,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        raw = "".join(b.get("text", "") for b in r.json().get("content", []))
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        if data.get("luokka") not in ("suora", "epäsuora", "ei"):
            raise ValueError(data)
        return data
    except Exception as e:  # luokittelu ei saa kaataa ajoa
        log.warning("Kielimalliluokittelu epäonnistui (%s): %s", res.item_id, e)
        return None


def decide(score: int, threshold: int, llm: dict | None) -> str:
    if score >= threshold or (llm and llm.get("luokka") == "suora"):
        return "suora"
    if llm:
        return "epäsuora" if llm.get("luokka") == "epäsuora" else "ei"
    return "tarkista" if score > 0 else "ei"


# ---------------------------------------------------------------------------
# Tila (SQLite)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT, doctype TEXT, body TEXT, meeting_date TEXT, title TEXT, url TEXT,
    text_hash TEXT, score INTEGER, matches TEXT, llm TEXT, category TEXT,
    first_seen TEXT, notified INTEGER DEFAULT 0,
    PRIMARY KEY (item_id, doctype));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS officials (
    id TEXT PRIMARY KEY, title TEXT, official TEXT, date TEXT, url TEXT,
    score INTEGER, matches TEXT, llm TEXT, category TEXT, first_seen TEXT,
    notified INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path: Path | None):
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        self.db.executescript(SCHEMA)

    def seen(self, item_id: str, doctype: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE item_id=? AND doctype=?",
                               (item_id, doctype)).fetchone() is not None

    def seen_official(self, oid: str) -> bool:
        return self.db.execute("SELECT 1 FROM officials WHERE id=?", (oid,)).fetchone() is not None

    def flagged_before(self, item_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT category FROM items WHERE item_id=? AND category IN ('suora','epäsuora')",
            (item_id,)).fetchone()
        return {"category": row[0]} if row else None

    def save(self, r: Result, text: str):
        self.db.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (r.item_id, r.doctype, r.body, r.date.isoformat() if r.date else None, r.title,
             r.url, hashlib.sha1(text.encode()).hexdigest(), r.score,
             json.dumps(r.matches, ensure_ascii=False),
             json.dumps(r.llm, ensure_ascii=False) if r.llm else None,
             r.category, dt.datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def save_official(self, d: dict, r: Result):
        self.db.execute(
            "INSERT OR REPLACE INTO officials VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (d["id"], d["title"], d["official"], d["date"].isoformat() if d["date"] else None,
             d["url"], r.score, json.dumps(r.matches, ensure_ascii=False),
             json.dumps(r.llm, ensure_ascii=False) if r.llm else None, r.category,
             dt.datetime.now().isoformat(timespec="seconds")))
        self.db.commit()


# ---------------------------------------------------------------------------
# Ajo
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, args, rules: Rules, store: Store):
        self.a, self.rules, self.store = args, rules, store
        self.net = Dynasty(delay=args.viive)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY") if not args.ei_llm else None
        self.model = os.environ.get("SEURANTA_MALLI", "claude-haiku-4-5-20251001")
        self.results: list[Result] = []
        self.outcomes: list[Result] = []   # aiemmin liputetun asian pöytäkirja ilmestyi

    # -- kokoukset --------------------------------------------------------
    def meetings_to_scan(self) -> list[tuple[str, str]]:
        if self.a.kokous:
            return [(k, "?") for k in self.a.kokous]
        bodies = parse_handlers(self.net.get("meeting_handlers"))
        log.info("Toimielimiä löytyi %d", len(bodies))
        cutoff = dt.date.today() - dt.timedelta(days=self.a.paivat)
        wanted = {"Esityslista", "Föredragningslista"}
        if self.a.poytakirjat:
            wanted |= {"Pöytäkirja", "Protokoll"}
        todo: dict[str, str] = {}
        for n, b in enumerate(bodies, 1):
            if self.a.toimielin and not any(t.lower() in b.name.lower() for t in self.a.toimielin):
                continue
            if b.latest_date and b.latest_date < cutoff:
                continue  # toimielin ei ole kokoontunut aikaikkunassa (esim. lakkautettu)
            refs = [MeetingRef(b.latest_meeting_id, b.latest_doctype, b.latest_date)] \
                if b.latest_meeting_id else []
            if self.a.syva:
                log.info("  [%d/%d] haetaan kokouslista: %s", n, len(bodies), b.name)
                try:
                    refs += parse_meeting_list(self.net.get("meetings", b.body_id))
                except Exception as e:
                    log.warning("Kokouslista epäonnistui (%s): %s", b.name, e)
            for r in refs:
                if r.date and r.date < cutoff:
                    continue
                if r.doctype and r.doctype not in wanted:
                    # pöytäkirjat haetaan aina, jos niissä on aiemmin liputettuja asioita
                    if not self._has_flagged(r.meeting_id):
                        continue
                todo[r.meeting_id] = b.name
        for mid, name in todo.items():
            log.info("  valittu: %s (kokous %s)", name, mid)
        return list(todo.items())

    def _has_flagged(self, meeting_id: str) -> bool:
        return self.store.db.execute(
            "SELECT 1 FROM items WHERE item_id LIKE ? AND category IN ('suora','epäsuora')",
            (f"{meeting_id}-%",)).fetchone() is not None

    def scan_meeting(self, meeting_id: str):
        m = parse_meeting(self.net.get("meeting", meeting_id), meeting_id)
        log.info("%s – %s %s: %d asiaa", m.body, m.doctype, m.date, len(m.items))
        for k, it in enumerate(m.items, 1):
            log.info("    asia %d/%d: %s", k, len(m.items), it.title[:70])
            if self.store.seen(it.item_id, m.doctype) and not self.a.kuiva:
                continue
            res = Result(it.item_id, m.doctype, m.body, m.date, it.title, item_url(it.item_id))
            if self.rules.skip_title(it.title):
                res.category = "ei"
                self.store.save(res, "")
                continue
            try:
                detail = parse_item(self.net.get("meetingitem", it.item_id))
                text = detail.text + "\n" + "\n".join(detail.attachments)
            except Exception as e:
                log.warning("Asian %s haku epäonnistui: %s – käytetään otsikkoa", it.item_id, e)
                text = ""
            res.score, res.matches = self.rules.score(it.title, text, m.body)
            if self.api_key:
                res.llm = llm_classify(res, text or it.title, self.model, self.api_key)
            res.category = decide(res.score, self.rules.threshold, res.llm)
            prev = self.store.flagged_before(it.item_id)
            self.store.save(res, text)
            if res.category in ("suora", "epäsuora", "tarkista"):
                self.results.append(res)
            elif prev and m.doctype in ("Pöytäkirja", "Protokoll"):
                res.category = prev["category"]
                self.outcomes.append(res)

    # -- viranhaltijapäätökset -------------------------------------------
    def scan_officials(self):
        soup = self.net.get("official_search", "", alo="1", kas="", txt="", koh="1",
                            pvm="", siv="100", dir="1", jar="1")
        for d in parse_official_list(soup):
            if not d["id"] or (self.store.seen_official(d["id"]) and not self.a.kuiva):
                continue
            res = Result(d["id"], "Viranhaltijapäätös", d["official"], d["date"], d["title"], d["url"])
            res.score, res.matches = self.rules.score(d["title"], "")
            if self.api_key and res.score > 0:   # otsikko ilman osumia harvoin relevantti
                res.llm = llm_classify(res, d["title"], self.model, self.api_key)
            res.category = decide(res.score, self.rules.threshold, res.llm)
            self.store.save_official(d, res)
            if res.category != "ei":
                self.results.append(res)

    def run(self):
        todo = self.meetings_to_scan()
        log.info("Käydään läpi %d kokousta", len(todo))
        for j, (mid, name) in enumerate(todo, 1):
            log.info("Kokous %d/%d", j, len(todo))
            try:
                self.scan_meeting(mid)
            except Exception as e:
                log.error("Kokous %s (%s) epäonnistui: %s", mid, name, e)
        if self.a.viranhaltijat:
            try:
                self.scan_officials()
            except Exception as e:
                log.error("Viranhaltijapäätökset epäonnistuivat: %s", e)


# ---------------------------------------------------------------------------
# Raportointi
# ---------------------------------------------------------------------------
CAT_LABEL = {"suora": "🔴 Suoraan koskevat", "epäsuora": "🟡 Epäsuorasti koskevat",
             "tarkista": "⚪ Tarkista (heikko avainsanaosuma, ei kielimallia)"}


def build_digest(results: list[Result], outcomes: list[Result], rules: Rules) -> str:
    today = fi_date(dt.date.today())
    if not results and not outcomes:
        return f"Espoon päätösseuranta {today}: ei uusia liputettuja asioita."
    order = {n.lower(): i for i, n in enumerate(rules.body_order)}
    key = lambda r: (order.get(r.body.lower(), 99), r.date or dt.date.max, -r.score)
    lines = [f"# Espoon päätösseuranta {today}", ""]
    for cat in ("suora", "epäsuora", "tarkista"):
        group = sorted([r for r in results if r.category == cat], key=key)
        if not group:
            continue
        lines += [f"## {CAT_LABEL[cat]} ({len(group)})", ""]
        for r in group:
            when = fi_date(r.date)
            lines.append(f"**{r.body} – {r.doctype} {when}**  ")
            lines.append(f"{r.title}  ")
            if r.llm:
                lines.append(f"_Arvio:_ {r.llm.get('perustelu', '')}  ")
            if r.matches:
                hit = r.matches[0]
                lines.append(f"_Osumat:_ {', '.join(sorted({m['saanto'] for m in r.matches}))}"
                             f" – “{hit['ote']}”  ")
            lines += [r.url, ""]
    if outcomes:
        lines += ["## ✅ Aiemmin liputetut asiat, joista pöytäkirja julkaistu", ""]
        for r in outcomes:
            lines += [f"**{r.body}** – {r.title}  ", r.url, ""]
    return "\n".join(lines).strip() + "\n"


TG_LABEL = {"suora": "🔴 <b>Suoraan koskevat</b>", "epäsuora": "🟡 <b>Epäsuorasti koskevat</b>",
            "tarkista": "⚪ <b>Tarkista</b>"}


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short_title(t: str) -> str:
    return re.sub(r"^Viranhaltijapäätös\s+\d+/\d{4}\s*", "", t).strip()


def _dd(d: dt.date | None) -> str:
    return f"{d.day}.{d.month}." if d else ""


NO_NEWS = "Ei mitään ilmoitettavaa Espoon päätöksenteosta tänään."


def _words(r: Result, n: int = 3) -> str:
    words = []
    for h in r.matches:
        w = (h.get("sana") or h["osuma"]).strip(" ,.;:–-")
        if w and w.lower() not in (x.lower() for x in words):
            words.append(w)
    return ", ".join(words[:n])


def _context(r: Result) -> str | None:
    """Lause, jossa ensimmäinen EI-otsikko-osuma on (otsikko näkyy jo linkkinä)."""
    for h in r.matches:
        if h.get("otsikossa") or not h.get("lause"):
            continue
        pre, hit, post = h["lause"]
        return f"“{_esc(pre)}<b>{_esc(hit)}</b>{_esc(post)}”"
    return None


def build_telegram(results: list[Result], outcomes: list[Result], rules: Rules) -> str:
    """Tiivis ilmoitus: otsikko linkkinä, toimielin ja päivä, osuneet sanat ja osumalause."""
    head = f"<b>Espoon päätösseuranta {fi_date(dt.date.today())}</b>"
    if not results and not outcomes:
        return f"{head}\n\n{NO_NEWS}"
    order = {n.lower(): i for i, n in enumerate(rules.body_order)}
    key = lambda r: (r.doctype == "Viranhaltijapäätös", order.get(r.body.lower(), 99),
                     r.date or dt.date.max, -r.score)
    out = [head]
    for cat in ("suora", "epäsuora", "tarkista"):
        group = sorted([r for r in results if r.category == cat], key=key)
        if not group:
            continue
        out += ["", f"{TG_LABEL[cat]} ({len(group)})"]
        for r in group:
            link = f'<a href="{_esc(r.url)}">{_esc(_short_title(r.title))}</a>'
            kind = "viranhaltija" if r.doctype == "Viranhaltijapäätös" else r.doctype.lower()
            meta = f"{_esc(r.body)} · {kind} {_dd(r.date)}"
            words = _words(r)
            if cat == "tarkista":   # tarkistettavat tiiviisti: linkki + avainsanat
                out.append(f"• {link}" + (f" · <i>{_esc(words)}</i>" if words else ""))
                continue
            out += ["", link, meta + (f" · <i>{_esc(words)}</i>" if words else "")]
            if r.llm and r.llm.get("perustelu"):
                out.append(f"↳ {_esc(r.llm['perustelu'])}")
            else:
                ctx = _context(r)
                if ctx:
                    out.append(f"↳ {ctx}")
    if outcomes:
        out += ["", "✅ <b>Pöytäkirja julkaistu aiemmin liputetuista</b>"]
        out += [f'• <a href="{_esc(r.url)}">{_esc(r.title)}</a> <i>({_esc(r.body)})</i>' for r in outcomes]
    return "\n".join(out)


def telegram_buttons() -> list[tuple[str, str]]:
    """Painikkeet viestin alle. AVAINSANAT_URL ja RAPORTIT_URL voi asettaa itse;
    GitHub Actionsissa ne muodostetaan automaattisesti reposta."""
    kw, rep = os.environ.get("AVAINSANAT_URL"), os.environ.get("RAPORTIT_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        base = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        kw = kw or f"{base}/blob/{branch}/avainsanat.yaml"
        rep = rep or f"{base}/tree/{branch}/raportit"
    return [(t, u) for t, u in (("📋 Avainsanat", kw), ("🗂 Raportit", rep))
            if u and u.startswith("https://")]


def send_telegram(text: str, token: str, chat_id: str, html: bool = False):
    if html:
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) > 3900:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        chunks = [c.strip() for c in chunks if c.strip()]
        buttons = telegram_buttons()
        for i, c in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": c, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": t, "url": u} for t, u in buttons]]}
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json=payload, timeout=30).raise_for_status()
        return
    plain = re.sub(r"\*\*", "", text)
    plain = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", plain)
    plain = re.sub(r"^#+\s*", "", plain, flags=re.M)
    chunks, cur = [], ""
    for para in plain.split("\n\n"):
        if len(cur) + len(para) > 3800:
            chunks.append(cur)
            cur = ""
        cur += para + "\n\n"
    chunks.append(cur)
    for c in chunks:
        if c.strip():
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": c.strip(),
                                "disable_web_page_preview": True}, timeout=30).raise_for_status()


def telegram_id_helper() -> int:
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip().strip('"').strip("'")
    if not tok:
        sys.exit("TELEGRAM_BOT_TOKEN puuttuu. Aja ensin: set TELEGRAM_BOT_TOKEN=token_tähän")
    if tok.lower().startswith("bot"):
        tok = tok[3:]
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", tok):
        sys.exit(f"Token ei ole oikean muotoinen (pituus {len(tok)} merkkiä). Oikea muoto on "
                 "numeroita, kaksoispiste ja noin 35 merkkiä, esim. 123456789:AAH... "
                 "Kopioi token BotFatherin viestistä uudelleen – vain itse token.")
    base = f"https://api.telegram.org/bot{tok}"
    r = requests.get(f"{base}/getMe", timeout=20)
    if r.status_code in (401, 404):
        sys.exit(f"Telegram ei tunnista tokenia ({r.status_code}). Pyydä BotFatherilta uusi: "
                 "/mybots → valitse botti → API Token.")
    r.raise_for_status()
    me = r.json()["result"]
    print(f"Token toimii. Botti: @{me.get('username')} ({me.get('first_name')})")
    upd = requests.get(f"{base}/getUpdates", timeout=20).json().get("result", [])
    chats = {}
    for u in upd:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("title") or " ".join(
                x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("type")
    if not chats:
        print(f"Chattejä ei löytynyt. Avaa Telegramissa @{me.get('username')}, paina Start "
              "tai lähetä viesti, ja aja tämä komento uudelleen.")
        return 1
    print("Löydetyt chatit (käytä id:tä TELEGRAM_CHAT_ID:nä):")
    for cid, name in chats.items():
        print(f"  {cid}   {name}")
    return 0


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Espoon päätösseuranta (Dynasty)")
    p.add_argument("--toimielin", action="append", help="rajaa toimielimeen (osa nimestä), voi toistaa")
    p.add_argument("--kokous", action="append", help="käsittele vain tämä kokous-id, voi toistaa")
    p.add_argument("--paivat", type=int, default=21, help="aikaikkuna taaksepäin päivinä (oletus 21)")
    p.add_argument("--syva", action="store_true", help="hae myös toimielinten kokouslistat (ei vain uusinta)")
    p.add_argument("--poytakirjat", action="store_true", help="käsittele myös pöytäkirjat")
    p.add_argument("--viranhaltijat", action="store_true", help="skannaa myös viranhaltijapäätösten otsikot")
    p.add_argument("--ei-llm", action="store_true", help="älä käytä kielimallia")
    p.add_argument("--kuiva", action="store_true", help="ei tallennusta eikä lähetystä, tulosta ruudulle")
    p.add_argument("--viive", type=float, default=1.0, help="viive pyyntöjen välillä sekunteina")
    p.add_argument("--saannot", default=str(HERE / "avainsanat.yaml"))
    p.add_argument("--tietokanta", default=str(HERE / "data" / "seuranta.db"))
    p.add_argument("--telegram-id", action="store_true",
                   help="tarkista Telegram-token ja näytä chat-id:t, joihin botille on kirjoitettu")
    p.add_argument("--telegram-esikatselu", action="store_true",
                   help="tulosta Telegram-viesti ruudulle (toimii myös --kuiva-tilassa)")
    p.add_argument("--testiviesti", action="store_true",
                   help="lähetä vain testiviesti Telegramiin ja lopeta")
    p.add_argument("--tuloste", help="tallenna kooste myös tähän tiedostoon (myös --kuiva-tilassa)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # Windowsin konsoli ja emojit
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if a.telegram_id:
        return telegram_id_helper()

    if a.testiviesti:
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (tok and chat):
            sys.exit("Aseta ensin TELEGRAM_BOT_TOKEN ja TELEGRAM_CHAT_ID.")
        try:
            send_telegram("✅ Espoon päätösseuranta: yhteys Telegramiin toimii.", tok, chat)
        except requests.HTTPError as e:
            sys.exit(f"Telegram hylkäsi viestin: {e.response.status_code} {e.response.text}")
        print("Testiviesti lähetetty.")
        return 0

    rules = Rules.load(Path(a.saannot))
    if not a.kuiva:
        Path(a.tietokanta).parent.mkdir(parents=True, exist_ok=True)
    store = Store(None if a.kuiva else Path(a.tietokanta))
    mon = Monitor(a, rules, store)
    mon.run()

    digest = build_digest(mon.results, mon.outcomes, rules)
    print(digest)
    if a.telegram_esikatselu:
        print("----- Telegram-viesti -----")
        print(build_telegram(mon.results, mon.outcomes, rules))
        for t, u in telegram_buttons():
            print(f"[{t}] → {u}")
    if a.tuloste:
        Path(a.tuloste).write_text(digest, encoding="utf-8")
        print(f"Kooste tallennettu: {Path(a.tuloste).resolve()}")
    if a.kuiva:
        return 0
    out = HERE / "raportit" / f"{dt.date.today().isoformat()}.md"
    out.parent.mkdir(exist_ok=True)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    today = dt.date.today().isoformat()
    row = store.db.execute("SELECT value FROM meta WHERE key='viimeisin_viesti'").fetchone()
    sent_today = bool(row and row[0] == today)
    found = bool(mon.results or mon.outcomes)
    if found:
        with out.open("a", encoding="utf-8") as f:
            f.write(digest + "\n")
        store.db.execute("UPDATE items SET notified=1 WHERE notified=0")
        store.db.execute("UPDATE officials SET notified=1 WHERE notified=0")
    # Uudet asiat lähetetään aina; "ei mitään" -viesti enintään kerran päivässä.
    if tok and chat and (found or not sent_today):
        send_telegram(build_telegram(mon.results, mon.outcomes, rules), tok, chat, html=True)
        store.db.execute("INSERT OR REPLACE INTO meta VALUES ('viimeisin_viesti', ?)", (today,))
    store.db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
