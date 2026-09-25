# Espoon päätösseuranta (prototyyppi)

Skripti lukee Espoon Dynasty-palvelusta toimielinten **esityslistat** (ja halutessa
pöytäkirjat ja viranhaltijapäätökset), pisteyttää jokaisen kokousasian avainsanoilla
ja valinnaisesti kielimallilla, ja tekee koosteen Aaltoa, AYY:tä, Otaniemeä ja
opiskelijoita koskevista asioista.

## Käyttöönotto

```bash
pip install -r requirements.txt
python -m pytest -q                                   # offline-testit
python espoo_seuranta.py --kuiva -v --ei-llm          # ensimmäinen kokeilu, ei tallenna mitään
python espoo_seuranta.py --kuiva -v --toimielin Kaupunginhallitus --syva --paivat 60
```

Kun kuivaharjoitus näyttää järkevältä, aja ilman `--kuiva`-lippua. Tila tallentuu
tiedostoon `data/seuranta.db`, jolloin samaa asiaa ei ilmoiteta kahdesti. Kooste
tallentuu kansioon `raportit/`.

### Liput

| Lippu | Merkitys |
|---|---|
| `--toimielin X` | vain toimielimet, joiden nimessä on X (voi toistaa) |
| `--kokous ID` | käsittele yksi kokous (id Dynastyn osoitteesta) |
| `--syva` | hae toimielinten kokouslistat, ei vain etusivun uusinta kokousta |
| `--paivat N` | aikaikkuna taaksepäin (oletus 21) |
| `--poytakirjat` | käsittele myös pöytäkirjat (aiemmin liputettujen asioiden pöytäkirjat haetaan aina) |
| `--viranhaltijat` | skannaa viranhaltijapäätösten 100 uusinta otsikkoa |
| `--ei-llm` | pelkkä avainsanahaku |
| `--kuiva` | ei tallennusta eikä Telegram-viestiä |

### Ympäristömuuttujat

- `ANTHROPIC_API_KEY` – kytkee kielimalliluokittelun päälle
- `SEURANTA_MALLI` – oletus `claude-haiku-4-5-20251001`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` – kooste Telegramiin

## Miten liputus toimii

1. **Avainsanat** (`avainsanat.yaml`): sanojen alkuosia regexinä, jotta taivutusmuodot
   osuvat. Mukana myös Otaniemen kaupunginosan tunnukset (`49-10-…`, `korttelin 10xxx`),
   jotka nappaavat asiat, joissa paikkaa ei mainita nimeltä.
2. **Kielimalli** arvioi jokaisen asian (paitsi rutiinikohdat kuten laillisuuden
   toteaminen) luokkiin suora / epäsuora / ei ja perustelee arvionsa.
3. **Lopputulos**: suora, jos avainsanapisteet ylittävät kynnyksen tai malli sanoo
   suora. Ilman kielimallia heikot osumat menevät "tarkista"-listalle.

Sääntöjä voi muokata ilman ohjelmointia: lisää kuvio tai uusi sääntö YAML-tiedostoon.
Säännön voi rajata tiettyihin toimielimiin (`toimielimet:`), ja hakasulkeissa oleva
kuvio (`['seura', 'tila']`) osuu vain, jos kaikki sanat löytyvät samasta asiasta.

## Telegram-viesti

- Jokaisesta osumasta näytetään otsikko linkkinä, toimielin, osuneet avainsanat ja
  lause, jossa osuma on (osuma lihavoituna). Tarkista-listan asiat yhdellä rivillä.
- Jos uutta ei ole, botti lähettää kerran päivässä viestin
  "Ei mitään ilmoitettavaa Espoon päätöksenteosta tänään."
- Viestin alla on painikkeet 📋 Avainsanat ja 🗂 Raportit. GitHub Actionsissa osoitteet
  muodostetaan automaattisesti reposta; muualla ne voi asettaa muuttujilla
  `AVAINSANAT_URL` ja `RAPORTIT_URL`.

## Ajastus GitHub Actionsilla

Tiedosto `.github/workflows/seuranta.yml` ajaa skriptin arkisin kahdesti ja
tallentaa tilan takaisin repoon. Lisää repositorion asetuksiin salaisuudet
`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` ja `TELEGRAM_CHAT_ID`.

## Tunnetut rajoitukset

- **Jäsentimiä ei ole vielä ajettu oikeaa palvelinta vastaan.** Ne on rakennettu
  Dynastyn sivurakenteen pohjalta ja testattu sitä jäljittelevillä fixtureilla.
  Ensimmäinen `--kuiva -v` -ajo kertoo nopeasti, jos jokin kohta pitää säätää.
- **Dynasty on istuntotilallinen**: sama istunto voi palauttaa eri sivun kuin
  pyydettiin (esim. "seuraavan asian"). Skripti tarkistaa jokaisen sivun ja
  nollaa istunnon tarvittaessa.
- Viranhaltijapäätöksistä luetaan vain otsikko (koko teksti on PDF:nä).
- Kaavoituskuulutukset ja Otakantaa.fi puuttuvat vielä; ne ovat seuraava lisäys.
- Skripti pitää sekunnin tauon pyyntöjen välillä, ettei kuormita kaupungin palvelinta.
