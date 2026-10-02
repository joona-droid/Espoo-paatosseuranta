"""Offline-testit. Aja: python -m pytest -q"""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import espoo_seuranta as es  # noqa: E402

FX = ROOT / "tests" / "fixtures"


def soup(name):
    return BeautifulSoup((FX / name).read_bytes(), "html.parser")


RULES = es.Rules.load(ROOT / "avainsanat.yaml")


def test_handlers_parsing_and_encoding():
    bodies = {b.name: b for b in es.parse_handlers(soup("handlers.html"))}
    assert "Kaupunginhallitus" in bodies  # ISO-8859-1 dekoodautuu oikein
    tl = bodies["Tekninen lautakunta"]
    assert tl.body_id == "160" and tl.latest_meeting_id == "20261727"
    assert tl.latest_doctype == "Esityslista" and tl.latest_date == dt.date(2026, 9, 23)
    assert "Testitoimielin" not in bodies


def test_meeting_parsing():
    m = es.parse_meeting(soup("meeting.html"), "20261727")
    assert m.body == "Tekninen lautakunta" and m.doctype == "Esityslista"
    assert m.date == dt.date(2026, 9, 23)
    assert [i.number for i in m.items] == ["1", "4", "7", "10"]  # numeerinen järjestys
    assert m.pdf_url.endswith("20261727.PDF")


def test_item_parsing():
    d = es.parse_item(soup("item4.html"))
    assert d.asianumero == "2771/10.03.01/2026"
    assert d.text.startswith("Asianumero") and "Päätöshistoria" not in d.text
    assert d.attachments and d.attachments[0].startswith("Liite 1")


def test_session_guard():
    assert es.page_matches(soup("item7.html"), "meetingitem", "20261727-7")
    assert not es.page_matches(soup("item7.html"), "meetingitem", "20261727-4")
    assert es.page_matches(soup("meeting.html"), "meeting", "20261727")


def test_keilaniemi_and_tapiola_not_rules():
    d = es.parse_item(soup("item7.html"))
    score, hits = RULES.score("Lausuntoja, päätöksiä ja kirjelmiä", d.text)
    assert {h["saanto"] for h in hits} == {"Kaupunginosa 10 (tunnukset)"}  # vain korttelinumero
    assert 0 < score < RULES.threshold
    assert RULES.score("Vuokrasopimus Keilaniemessä", "")[0] == 0
    assert RULES.score("Tapiolan ostoskeskus", "Tapiolan keskusta")[0] == 0


def test_scoring_title_bonus_and_irrelevant():
    s, _ = RULES.score("Otaniemen Tietotien katusuunnitelma", "")
    assert s == 4  # 3 + otsikkobonus
    d = es.parse_item(soup("item4.html"))
    s2, hits2 = RULES.score("Merivirran eteläosan katu- ja puistosuunnitelman hyväksyminen", d.text)
    assert 0 < s2 < RULES.threshold  # vain heikko "pyöräilyn"-osuma → tarkista/LLM


def test_skip_boilerplate():
    assert RULES.skip_title("Kokouksen laillisuuden ja päätösvaltaisuuden toteaminen")
    assert not RULES.skip_title("Otaniemen Tietotien katusuunnitelma")


def test_officials():
    rows = es.parse_official_list(soup("officials.html"))
    assert len(rows) == 2
    assert rows[0]["official"] == "Suunnittelupäällikkö" and rows[0]["date"] == dt.date(2026, 9, 18)
    s, _ = RULES.score(rows[0]["title"], "")
    assert s >= RULES.threshold  # "Otaniemessä" + "49-10-"


def test_decide():
    assert es.decide(4, 3, None) == "suora"
    assert es.decide(1, 3, None) == "tarkista"
    assert es.decide(1, 3, {"luokka": "ei"}) == "ei"
    assert es.decide(0, 3, {"luokka": "epäsuora"}) == "epäsuora"


class FakeNet:
    pages = {("meeting_handlers", ""): "handlers.html", ("meeting", "20261727"): "meeting.html",
             ("meetingitem", "20261727-4"): "item4.html", ("meetingitem", "20261727-7"): "item7.html",
             ("official_search", ""): "officials.html"}

    def get(self, page, id_="", **kw):
        name = self.pages.get((page, id_))
        if not name:
            raise RuntimeError(f"ei fixturea: {page} {id_}")
        return soup(name)


def test_full_run_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(es.dt, "date", type("D", (dt.date,), {"today": staticmethod(lambda: dt.date(2026, 9, 22))}))
    args = SimpleNamespace(kokous=None, toimielin=None, paivat=21, poytakirjat=False, syva=False,
                           viranhaltijat=True, ei_llm=True, kuiva=False, viive=0)
    store = es.Store(tmp_path / "s.db")
    mon = es.Monitor(args, RULES, store)
    mon.net = FakeNet()
    mon.run()
    cats = {r.item_id: r.category for r in mon.results}
    assert cats["20261727-7"] == "tarkista"       # Keilaniemi, kortteli 10051 → kevyt
    assert cats["20261727-4"] == "tarkista"       # heikko osuma
    assert cats["20261727-10"] == "suora"         # sivun haku epäonnistui → otsikko riittää
    assert cats["2026374767"] == "suora"          # viranhaltijapäätös Otaniemessä
    assert "2026374634" not in cats
    digest = es.build_digest(mon.results, mon.outcomes, RULES)
    assert "Suoraan koskevat" in digest and "Otaniem" in digest
    # toinen ajo: ei uusia
    mon2 = es.Monitor(args, RULES, store)
    mon2.net = FakeNet()
    mon2.run()
    assert mon2.results == []


def test_ignore_body_name_and_school_boilerplate():
    body = "Kaupunginhallituksen työllisyys- ja kotoutumisjaosto"
    s, _ = RULES.score(body + "n kokousaikataulu vuosille 2027 ja 2028", body + " 21.09.2026", body)
    assert s == 0
    s2, _ = RULES.score("Oikaisuvaatimus lehtorin valinnasta",
                        "kelpoinen henkilö, joka on suorittanut ylemmän korkeakoulututkinnon; "
                        "tarve huomioida oppilaiden ja opiskelijoiden erilaiset tarpeet")
    assert s2 == 0
    s3, _ = RULES.score("Liikunnan maksujen tarkistaminen", "lapset/nuoret alle 18 v., opiskelijat ja työttömät 22,03 €/kausi")
    assert s3 == 2   # oikea opiskelijamaininta säilyy


def test_body_restricted_and_combined_rules():
    L = "Liikunta- ja hyvinvointilautakunta"
    s, h = RULES.score("Avustukset", "urheiluseurojen käyttöön tulevat tilat", L)
    assert [x["saanto"] for x in h] == ["Seurat ja harrastustilat"]
    assert RULES.score("Kokous", "seuraava kokous pidetään tilassa 3", L)[0] == 0   # "seuraava" ei osu
    assert RULES.score("Avustukset", "urheiluseurojen tilat", "Tekninen lautakunta")[0] == 0
    assert RULES.score("Otahallin peruskorjaus", "", L)[0] >= RULES.threshold
    s2, _ = RULES.score("Työttömyyden kehitys", "",
                        "Kaupunginhallituksen elinkeino- ja kilpailukykyjaosto")
    assert s2 >= RULES.threshold


def test_context_sentence_and_no_news():
    text = ("Investointikehykseen on lisätty uusi päiväkoti. Otaniemen uuden päiväkodin "
            "aikataulu on siirtynyt n. 2 vuotta. Tapiolan koulu jatkuu.")
    r = es.Result("1-1", "Esityslista", "Kasvun ja oppimisen lautakunta", dt.date(2026, 9, 23),
                  "Talousarvion kehys", "https://x")
    r.score, r.matches = RULES.score(r.title, text, r.body)
    r.category = "suora"
    msg = es.build_telegram([r], [], RULES)
    assert "“<b>Otaniemen</b> uuden päiväkodin aikataulu on siirtynyt n. 2 vuotta.”" in msg
    assert es.NO_NEWS in es.build_telegram([], [], RULES)


def test_buttons_from_github_env(monkeypatch):
    monkeypatch.delenv("AVAINSANAT_URL", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "ayy/espoo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    urls = dict(es.telegram_buttons())
    assert urls["📋 Avainsanat"] == "https://github.com/ayy/espoo/blob/main/avainsanat.yaml"


def test_no_news_sent_once_per_day(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(es, "send_telegram", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(es.Monitor, "run", lambda self: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    db = str(tmp_path / "s.db")
    es.main(["--tietokanta", db])
    es.main(["--tietokanta", db])
    assert len(sent) == 1 and es.NO_NEWS in sent[0]


def test_finnish_holidays():
    h = es.finnish_holidays(2026)
    assert h[dt.date(2026, 4, 3)] == "pitkäperjantai"      # pääsiäinen 5.4.2026
    assert h[dt.date(2026, 4, 6)] == "2. pääsiäispäivä"
    assert h[dt.date(2026, 5, 14)] == "helatorstai"
    assert h[dt.date(2026, 6, 19)] == "juhannusaatto"
    assert es.easter(2027) == dt.date(2027, 3, 28)
    assert es.day_off_reason(dt.date(2026, 12, 7)) is None  # maanantai
    assert es.day_off_reason(dt.date(2026, 9, 26)) == "viikonloppu"


# ---------------------------------------------------------------------------
# V1.1: toimielintasot ja asianumeroseuranta
# ---------------------------------------------------------------------------
def test_tier_lookup():
    assert RULES.tier_of("Kaupunginhallitus") == "A"
    assert RULES.tier_of("Valtuusto") == "A"
    assert RULES.tier_of("Kaupunginhallituksen konserni- ja tilajaosto") == "B"
    assert RULES.tier_of("Kasvun ja oppimisen lautakunta") == "C"
    assert RULES.tier_of("Kasvun ja oppimisen lautakunnan nuorisoasiainjaosto") == "C"
    assert RULES.tier_of("Tarkastuslautakunta") == "D"
    assert RULES.tier_of("Uusi tuntematon lautakunta") == "B"          # oletustaso
    assert RULES.tier_of("Kaupunginjohtaja", official=True) == "B"      # viranhaltijataso


def _tiered(title, text, body):
    raw, hits = RULES.score(title, text, body)
    return RULES.apply_tier(raw, hits, RULES.tier_of(body))


def test_tier_effect_on_scores():
    txt = "Talousarviossa huomioidaan opiskelijat ja nuoret."
    assert _tiered("Talousarvio 2027", txt, "Kaupunginhallitus") == 3                 # A: suora
    assert _tiered("Talousarvio 2027", txt, "Tekninen lautakunta") == 2               # B: tarkista
    assert _tiered("Talousarvio 2027", txt, "Kasvun ja oppimisen lautakunta") == 1    # C: tarkista
    assert _tiered("Montessori", "Tapiolan ja Laajalahden koulut",
                   "Kasvun ja oppimisen lautakunta") == 0                              # C: heikko pois
    assert _tiered("Tilintarkastus", "opiskelijat mainitaan", "Tarkastuslautakunta") == 0  # D
    assert _tiered("Otaniemen kampus", "", "Tarkastuslautakunta") >= RULES.threshold  # D + vahva
    assert _tiered("Nolla", "ei osumia", "Kaupunginhallitus") == 0                    # ei tyhjästä


def test_v10_database_is_upgraded(tmp_path):
    import sqlite3
    db = tmp_path / "vanha.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE items (item_id TEXT, doctype TEXT, body TEXT,
        meeting_date TEXT, title TEXT, url TEXT, text_hash TEXT, score INTEGER, matches TEXT,
        llm TEXT, category TEXT, first_seen TEXT, notified INTEGER DEFAULT 0,
        PRIMARY KEY (item_id, doctype));
        INSERT INTO items VALUES ('1-1','Esityslista','X','2026-09-01','t','u','h',3,'[]',
        NULL,'suora','2026-09-01',1);""")
    con.commit(); con.close()
    store = es.Store(db)
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(items)")}
    assert {"asianumero", "tier"} <= cols
    assert store.seen("1-1", "Esityslista")                    # vanha data säilyy


def _page(html):
    return BeautifulSoup(html, "html.parser")


B = "https://espoo.oncloudos.com/cgi/DREQUEST.PHP"


def _meeting_html(mid, body, date, title):
    return (f"<h1>{body}  Esityslista {date}</h1>"
            f'<a href="https://espoo.oncloudos.com/kokous/{mid}.PDF">E</a>'
            f'<a href="{B}?page=meetingitem&id={mid}-3">{title}</a>')


def _item_html(mid, text):
    return (f'<a href="https://espoo.oncloudos.com/kokous/{mid}-3.PDF">K</a>'
            f"<p>Kokousasian teksti</p><p>Asianumero 1234/10.02.03/2026</p><p>{text}</p>"
            "<p>Päätöshistoria</p>")


class CaseNet:
    pages = {
        ("meeting", "100"): _meeting_html("100", "Kaupunkisuunnittelulautakunta", "16.09.2026",
                                          "Kampusalueen asemakaava"),
        ("meetingitem", "100-3"): _item_html("100", "Asemakaava koskee Otaniemen kampusta."),
        ("meeting", "200"): _meeting_html("200", "Kaupunginhallitus", "06.10.2026",
                                          "Asemakaavan hyväksyminen"),
        ("meetingitem", "200-3"): _item_html("200", "Kaupunginhallitus esittää valtuustolle "
                                                    "kaavan hyväksymistä."),
    }

    def get(self, page, id_="", **kw):
        return _page(self.pages[(page, id_)])


def test_case_number_tracking(tmp_path):
    store = es.Store(tmp_path / "s.db")

    def run(mid):
        args = SimpleNamespace(kokous=[mid], toimielin=None, paivat=21, poytakirjat=False,
                               syva=False, viranhaltijat=False, ei_llm=True, kuiva=False, viive=0)
        mon = es.Monitor(args, RULES, store)
        mon.net = CaseNet()
        mon.run()
        return mon.results

    first = run("100")
    assert first[0].category == "suora" and first[0].asianumero == "1234/10.02.03/2026"
    second = run("200")   # KH:n tekstissä ei yhtään avainsanaa
    assert len(second) == 1
    r = second[0]
    assert r.category == "suora" and r.tier == "A"
    assert r.advanced_from["body"] == "Kaupunkisuunnittelulautakunta"
    msg = es.build_telegram(second, [], RULES)
    assert "⬆ Eteni" in msg and "🏛" in msg


def test_backfill_case_numbers(tmp_path):
    store = es.Store(tmp_path / "s.db")
    store.db.execute("INSERT INTO items (item_id, doctype, body, category) "
                     "VALUES ('100-3','Esityslista','Kaupunkisuunnittelulautakunta','suora')")
    store.db.commit()
    args = SimpleNamespace(kokous=["200"], toimielin=None, paivat=21, poytakirjat=False,
                           syva=False, viranhaltijat=False, ei_llm=True, kuiva=False, viive=0)
    mon = es.Monitor(args, RULES, store)
    mon.net = CaseNet()
    mon.run()
    assert store.db.execute("SELECT asianumero FROM items WHERE item_id='100-3'").fetchone()[0] \
        == "1234/10.02.03/2026"
    assert mon.results and mon.results[0].advanced_from   # vanha rivi löytyy heti


def test_telegram_first_line_tells_status():
    assert es.build_telegram([], [], RULES).splitlines()[0] == es.NO_NEWS
    r = es.Result("1-1", "Esityslista", "Kaupunginhallitus", dt.date(2026, 10, 2),
                  "Otaniemen kampus", "https://x", score=4, category="suora")
    assert es.build_telegram([r], [], RULES).splitlines()[0].startswith("🔴")
