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
