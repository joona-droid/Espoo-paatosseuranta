# Muutoshistoria

## V1.1 – 25.9.2026

**Uutta**
- **Toimielintasot A–D** (`toimielintasot` tiedostossa `avainsanat.yaml`): valtuusto ja
  kaupunginhallitus +1 piste, toissijaiset lautakunnat −1, valvonta- ja vaalielimet vain
  suorilla nimiosumilla. Tasot ja niiden toimielimet ovat muokattavissa YAML-tiedostossa.
- **Asianumeroseuranta**: aiemmin liputettu asia nousee esiin, kun se tulee toisen
  toimielimen käsittelyyn (⬆ Eteni), vaikka tekstissä ei olisi avainsanoja.
- Valtuuston ja kaupunginhallituksen asiat merkitään 🏛-merkillä ja järjestetään ensin.
- Raportteihin asianumero, toimielintaso ja versio.
- `--version`-lippu.
- Telegram-viestistä poistettu otsikkorivi "Espoon päätösseuranta pvm": ilmoituksen
  esikatselusta näkee heti, onko uutta.
- Ajo kerran päivässä arkiaamuisin (iltapäiväajo poistettu): iltapäivällä julkaistut
  esityslistat tulevat seuraavan aamun viestissä, jolloin niihin ehtii reagoida.

**Tietokanta**
- V1.0-tietokanta päivittyy automaattisesti (uudet sarakkeet `asianumero`, `tier`).
  Aiemmin liputettujen asioiden asianumerot haetaan ensimmäisellä ajolla.

## V1.0 – 25.9.2026

- Esityslistojen, pöytäkirjojen ja viranhaltijapäätösten seuranta Dynastysta.
- Avainsanasäännöt (`avainsanat.yaml`), toimielinkohtaiset säännöt ja JA-ehdot.
- Telegram-kooste: otsikkolinkki, avainsanat, osumalause, painikkeet
  (Avainsanat, Raportit) ja "ei mitään ilmoitettavaa" -viesti kerran päivässä.
- Ajastus GitHub Actionsilla arkisin kahdesti, ei arkipyhinä.
- Keilaniemi ja Tapiola poistettu avainsanoista; sosiaalipolitiikan sektorin sanat lisätty.
