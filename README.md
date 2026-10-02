# Espoon päätösseuranta V1.1

Skripti lukee Espoon Dynasty-palvelusta toimielinten **esityslistat**, pöytäkirjat ja
viranhaltijapäätökset, pisteyttää jokaisen asian avainsanoilla ja toimielimen tason
mukaan, ja lähettää Telegram-kanavalle koosteen Aaltoa, AYY:tä, Otaniemeä ja
opiskelijoita koskevista asioista. Ajo tapahtuu GitHub Actionsilla arkiaamuisin.

Muutokset versioittain: ks. [CHANGELOG.md](CHANGELOG.md).

## Tiedostot

| Tiedosto | Sisältö |
|---|---|
| `espoo_seuranta.py` | itse skripti |
| `avainsanat.yaml` | avainsanat, ohitettavat ilmaukset ja **toimielintasot** – muokattavissa ilman ohjelmointia |
| `data/seuranta.db` | tietokanta: jo nähdyt asiat ja niiden asianumerot |
| `raportit/` | päiväkohtaiset raportit lainauksineen |
| `.github/workflows/seuranta.yml` | ajastus |
| `tests/` | testit (`python -m pytest -q`) |

## Miten liputus toimii

1. **Avainsanat** (`saannot` tiedostossa `avainsanat.yaml`) antavat asialle pisteitä.
   Säännön voi rajata tiettyihin toimielimiin (`toimielimet:`), ja hakasulkeissa oleva
   kuvio (`['seura', 'tila']`) osuu vain, jos kaikki sanat löytyvät samasta asiasta.
2. **Toimielintaso** muuttaa pisteitä (`toimielintasot`):

   | Taso | Toimielimet | Vaikutus |
   |---|---|---|
   | A 🏛 | Valtuusto, Kaupunginhallitus | +1 |
   | B | Kaupunkisuunnittelu, KH:n jaostot, Tekninen, Liikunta ja hyvinvointi | ±0 |
   | C | Kasvu ja oppiminen, Kulttuuri ja nuoriso, Ympäristö ja rakennus | −1 |
   | D | Tarkastus, Keskusvaali, Svenska rum | vain suorat nimiosumat |

   Taso ei luo osumia tyhjästä: asia, jossa ei ole yhtään avainsanaa, ei nouse esiin.
3. **Asianumeroseuranta**: jos aiemmin liputettu asia (sama asianumero) tulee toisen
   toimielimen listalle, se nousee esiin automaattisesti – myös ilman avainsanoja – ja
   viestissä näkyy mistä se eteni (esim. kaupunkisuunnittelulautakunta → KH).
4. **Lopputulos**: suora, jos pisteet ≥ kynnys (3); muut osumat Tarkista-listalle.
   Kielimalliluokittelu on valmiina, mutta pois päältä (`--ei-llm`).

## Telegram-viesti

- Viestissä ei ole otsikkoriviä: ensimmäinen rivi (🔴 Suoraan koskevat … tai
  "Ei mitään ilmoitettavaa…") näkyy suoraan ilmoituksessa ja kanavalistassa.
- Otsikko linkkinä, toimielin, osuneet avainsanat ja lause, jossa osuma on.
- 🏛 = valtuusto tai kaupunginhallitus; ⬆ Eteni = asia on siirtynyt toimielimestä toiseen.
- Jos uutta ei ole: "Ei mitään ilmoitettavaa Espoon päätöksenteosta tänään."
- Painikkeet 📋 Avainsanat ja 🗂 Raportit (repo on julkinen, joten toimivat kaikille).

## Ajastus

Kerran päivässä arkiaamuisin klo 8 (talviaikana klo 7), ei viikonloppuisin eikä arkipyhinä.
Edellisen päivän aikana julkaistut asiat tulevat seuraavan aamun viestissä.
Käsin käynnistetty ajo (Actions → Run workflow) ajetaan aina.
Salaisuudet: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (ja myöhemmin `ANTHROPIC_API_KEY`).

## Paikallinen käyttö

```
python -m venv .venv
.venv\Scripts\activate            (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt pytest
python -m pytest -q
python espoo_seuranta.py --kuiva --ei-llm --syva --telegram-esikatselu
```

| Lippu | Merkitys |
|---|---|
| `--kuiva` | ei tallennusta eikä lähetystä |
| `--syva` | hae toimielinten kokouslistat, ei vain uusinta kokousta |
| `--viranhaltijat` | myös viranhaltijapäätösten otsikot |
| `--toimielin X` / `--kokous ID` | rajaa ajo |
| `--telegram-esikatselu` | tulosta Telegram-viesti ruudulle |
| `--telegram-id` / `--testiviesti` | Telegram-asetusten tarkistus |
| `--vain-arkipaivina` | ohita viikonloput ja arkipyhät |
| `--version` | näytä versio |

## Tunnetut rajoitukset

- Viranhaltijapäätöksistä luetaan vain otsikko, joten niille ei ole asianumeroa.
- Asianumeroseuranta kattaa asiat, jotka on liputettu suoriksi (tai kielimallin kanssa epäsuoriksi).
- Kaavoituskuulutukset ja Otakantaa.fi puuttuvat vielä.
