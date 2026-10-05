#!/usr/bin/env python3
"""Consensus du chiffre d'affaires (et du BPA, a defaut de Yahoo) : page
Google Finance de chaque entreprise, onglet "Earnings".

Google Finance donne, pour le dernier rapport trimestriel, le BPA et le
chiffre d'affaires publies face a l'estimation des analystes ("Revenue / Est.",
"EPS / Est."), y compris pour beaucoup de valeurs europeennes ou Yahoo n'a que
le consensus annuel. Seul le dernier rapport est dans la page : l'historique se
construit au fil des releves.

Une page n'est lue que pour les entreprises dont la publication de resultats
est recente ou attendue (12 derniers jours : derniere publication mesuree ou
prochaine date connue des etudes) et pas encore relevee ; --tous les lit
toutes.

Couverture constatee (octobre 2026) : Etats-Unis, France, Allemagne, Italie,
Espagne, Pologne ; souvent rien au Royaume-Uni, dans les pays nordiques, en
Suisse, au Benelux, en Autriche et au Portugal.

Ecrit consensus_google.json :
  {ticker: {"symbole": "RXL:EPA" (null : introuvable, "essai" : jour du dernier
   essai), "rapports": [[date, periode, devise, bpa_publie, bpa_estime,
   ca_publie, ca_estime, releve_le]]}}
lu par etude_amf.py (enrichir) pour les publications depuis
etude_avant.DEBUT_SUIVI.

Usage :
  python google_finance.py            publications recentes
  python google_finance.py --tous     toutes les entreprises
  python google_finance.py AAPL RXL.PA  ces entreprises
Bibliotheque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "consensus_google.json")
ETUDES = (("statistiques.json", "historique_amf"), ("communiques_usa.json", None), ("communiques_stoxx.json", None))
FENETRE = 12  # jours
ESSAI_INTROUVABLE = 30  # jours avant de rechercher a nouveau un symbole introuvable
MAX_PAGES = 600
# Suffixe Yahoo -> place de cotation Google.
PLACES = {"PA": "EPA", "AS": "AMS", "BR": "EBR", "DE": "ETR", "L": "LON", "ST": "STO", "SW": "SWX", "MI": "BIT",
          "MC": "BME", "CO": "CPH", "OL": "OSL", "HE": "HEL", "WA": "WSE", "AT": "ATH", "VI": "VIE", "IR": "ISE",
          "LS": "ELI"}
PLACES_USA = ("NASDAQ", "NYSE", "NYSEAMERICAN", "BATS")
ENTETES = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
MOIS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                    "Dec"), 1)}
MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def symboles(ticker):
    """Symboles Google possibles d'un ticker Yahoo, du plus probable au moins."""
    base, _, suffixe = ticker.rpartition(".")
    if not base:  # Etats-Unis : BRK-B -> BRK.B
        return [f"{ticker.replace('-', '.')}:{p}" for p in PLACES_USA]
    place = PLACES.get(suffixe)
    if place is None:
        return []
    if suffixe == "L":  # BT-A.L -> BT.A:LON
        return [f"{base.replace('-', '.')}:{place}"]
    return [f"{base}:{place}"]  # HM-B.ST -> HM-B:STO


def lire_page(symbole):
    """Texte de l'onglet Earnings ("|" entre les elements), "" sans cet
    onglet (symbole inconnu), None si la page n'a pas pu etre lue."""
    req = urllib.request.Request(f"https://www.google.com/finance/quote/{symbole}?hl=en", headers=ENTETES)
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                s = r.read().decode("utf-8", "ignore")
            break
        except Exception:
            if essai == 2:
                return None
            time.sleep(5 * (essai + 1))
    i = s.find('aria-labelledby="earnings"')
    if i < 0:
        return ""
    t = re.sub(r"<[^>]+>", "|", s[i:i + 4000])
    t = html.unescape(t).replace(" ", " ").replace("\xa0", " ")
    return re.sub(r"\s*\|[\s|]*", "|", t)


def nombre(x):
    """'$2.02', '109.42B', '7,499.77', '€3.07', '-' -> float ou None."""
    x = re.sub(r"[^\d.,KMBT-]", "", x.strip()).replace(",", "")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([KMBT]?)", x)
    return float(m.group(1)) * MULT[m.group(2)] if m else None


def rapport(texte):
    """Dernier rapport de la page : [date, periode, devise, bpa_publie,
    bpa_estime, ca_publie, ca_estime] ou None."""
    m = re.search(r"\|Last report\|(\w{3}) (\d{1,2}), (\d{4})\|Fiscal period\|([^|]+)\|", texte)
    if not m or m.group(1) not in MOIS:
        return None
    date = dt.date(int(m.group(3)), MOIS[m.group(1)], int(m.group(2))).isoformat()
    res = [date, m.group(4).strip(), None, None, None, None, None]
    for cle, i in (("EPS", 3), ("Revenue", 5)):
        # "|$2.02|/|$1.89|", "|109.42B|/ 108.96B|" ou "|-|/ -|"
        v = re.search(cle + r" / Est\. \((\w+)\)\|([^|/]*)\|\s*/\s*\|?([^|]*)\|", texte)
        if v:
            res[2] = res[2] or v.group(1)
            res[i], res[i + 1] = nombre(v.group(2)), nombre(v.group(3))
    return res


def charger():
    if not os.path.exists(SORTIE):
        return {"version": 1, "source": "Google Finance", "maj": None, "entreprises": {}}
    with open(SORTIE, encoding="utf-8") as f:
        return json.load(f)


def candidats(doc, aujourd_hui):
    """Entreprises des etudes dont une publication est recente ou attendue et
    pas encore relevee."""
    debut = (aujourd_hui - dt.timedelta(days=FENETRE)).isoformat()
    fin = aujourd_hui.isoformat()
    res = []
    for fichier, cle in ETUDES:
        chemin = os.path.join(ICI, fichier)
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as f:
            etude = json.load(f)
        for e in (etude.get(cle) if cle else etude or {}).get("entreprises", []):
            dates = [(e.get("prochaine") or {}).get("date")]
            evs = (e.get("resultats") or {}).get("evenements") or []
            dates += [x["jour"] for x in evs[:1]]
            dates = [d for d in dates if d and debut <= d <= fin]
            if not dates:
                continue
            deja = [r[0] for r in (doc["entreprises"].get(e["ticker"]) or {}).get("rapports", [])
                    if r[5] is not None or r[3] is not None]
            if any(abs((dt.date.fromisoformat(r) - dt.date.fromisoformat(d)).days) <= 4
                   for r in deja for d in dates):
                continue
            res.append(e["ticker"])
    return sorted(set(res))


def relever(ticker, fiche, aujourd_hui):
    """Lit la page d'une entreprise ; met a jour sa fiche. Renvoie le rapport
    lu (ou None)."""
    if fiche.get("symbole") is None and fiche.get("essai"):
        if (aujourd_hui - dt.date.fromisoformat(fiche["essai"])).days < ESSAI_INTROUVABLE:
            return None
    essais = [fiche["symbole"]] if fiche.get("symbole") else symboles(ticker)
    for s in essais:
        texte = lire_page(s)
        if texte is None:
            return None  # page illisible : on reessaiera
        if texte == "":
            continue
        fiche["symbole"] = s
        fiche.pop("essai", None)
        r = rapport(texte)
        if r is None:
            return None
        rapports = fiche.setdefault("rapports", [])
        ligne = r + [aujourd_hui.isoformat()]
        for i, x in enumerate(rapports):
            if x[0] == r[0]:
                # Meme rapport : on garde le premier releve de l'estimation
                # (celle d'avant la publication), on complete le publie.
                rapports[i] = [x[k] if x[k] is not None and k in (4, 6) else ligne[k] for k in range(7)] + [x[7]]
                return r
        rapports.append(ligne)
        rapports.sort()
        return r
    fiche["symbole"] = None
    fiche["essai"] = aujourd_hui.isoformat()
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--tous", action="store_true")
    args = ap.parse_args()
    aujourd_hui = dt.date.today()
    doc = charger()
    if args.tickers:
        liste = args.tickers
    elif args.tous:
        liste = sorted({e["ticker"] for fichier, cle in ETUDES if os.path.exists(os.path.join(ICI, fichier))
                        for e in ((lambda j: j.get(cle) if cle else j)(json.load(open(os.path.join(ICI, fichier),
                                                                                       encoding="utf-8")))
                                  or {}).get("entreprises", [])})
    else:
        liste = candidats(doc, aujourd_hui)
    liste = liste[:MAX_PAGES]
    fiches = {t: doc["entreprises"].setdefault(t, {"symbole": None}) for t in liste}
    lus = 0
    with cf.ThreadPoolExecutor(4) as ex:
        for t, r in zip(liste, ex.map(lambda t: relever(t, fiches[t], aujourd_hui), liste)):
            lus += r is not None
    doc["maj"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = SORTIE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, SORTIE)
    introuvables = sum(1 for t in liste if fiches[t].get("symbole") is None)
    print(f"consensus_google.json : {len(liste)} entreprises lues, {lus} rapports, {introuvables} sans page "
          f"Google Finance.", file=sys.stderr)


if __name__ == "__main__":
    main()
