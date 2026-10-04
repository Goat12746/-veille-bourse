#!/usr/bin/env python3
"""Publications de resultats des entreprises americaines suivies (S&P 500 et
Nasdaq-100 d'univers_objectifs.json) depuis 2015, d'apres la SEC (EDGAR),
base de l'etude des resultats americains (etude_usa.py, onglet Statistiques >
Communiques > Etats-Unis).

  - publication : depot 8-K, item 2.02 (Results of Operations and Financial
    Condition), qui accompagne le communique de resultats ; heure exacte
    d'acceptation par la SEC (heure de New York, lue dans l'en-tete du depot :
    celle des index JSON est decalee pour certains deposants). Un seul depot
    par periode comptable (le premier) ;
  - periode publiee : le dernier trimestre (ou exercice) clos avant la
    publication, d'apres les comptes deposes ensuite (10-Q, 10-K, XBRL) ;
    une publication recente reste sans periode tant que ses comptes ne sont
    pas deposes (quelques semaines plus tard).
Les resultats ne sont juges que face au consensus des analystes (etude_usa.py),
jamais face a l'an dernier.

Ecrit resultats_usa.json. Mise a jour : seules les entreprises ayant depose
un 8-K, 10-Q ou 10-K depuis la derniere mise a jour sont relues (index
quotidiens de la SEC).

Usage :
  python edgar.py              mise a jour
  python edgar.py --complet    relit toutes les entreprises
Bibliotheque standard uniquement. La SEC demande un User-Agent avec une
adresse de contact et au plus 10 requetes par seconde.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "resultats_usa.json")
UA = "veille-bourse paczekgau@gmail.com"
DEPUIS = "2015-01-01"
FORMES_COMPTES = ("10-Q", "10-K", "10-Q/A", "10-K/A")
DELAI_TRIMESTRE, DELAI_EXERCICE = 75, 100  # jours max entre la fin de la periode et la publication
EN_ATTENTE = 120  # jours : publication recente sans comptes encore deposes

# Postes XBRL, par ordre de preference (les societes changent parfois de
# poste : la comparaison se fait sur un poste present les deux annees).
CA = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "RevenuesNetOfInterestExpense",
      "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet",
      "InterestAndDividendIncomeOperating"]
RESULTAT = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]

_verrou = threading.Lock()
_dernier = [0.0]


def sec_get(url, essais=4):
    """Requete SEC : au plus ~8 par seconde, nouvelles tentatives sur 429/5xx."""
    for i in range(essais):
        with _verrou:
            attente = _dernier[0] + 0.125 - time.time()
            if attente > 0:
                time.sleep(attente)
            _dernier[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404 or (e.code not in (403, 429) and e.code < 500) or i == essais - 1:
                raise
        except (urllib.error.URLError, OSError):
            if i == essais - 1:
                raise
        time.sleep(2 * (i + 1))


# ---------------------------------------------------------------------------
# Depots
# ---------------------------------------------------------------------------

def _dimanche(annee, mois, rang):
    """rang-ieme dimanche du mois (1 = premier)."""
    d = dt.date(annee, mois, 1)
    return d + dt.timedelta(days=(6 - d.weekday()) % 7 + 7 * (rang - 1))


def decalage_new_york(local):
    """Decalage UTC de New York (heures) a une heure locale de New York."""
    ete = (dt.datetime.combine(_dimanche(local.year, 3, 2), dt.time(2)) <= local
           < dt.datetime.combine(_dimanche(local.year, 11, 1), dt.time(2)))
    return -4 if ete else -5


def acceptation(cik, numero):
    """Heure d'acceptation du depot (heure de New York, ISO avec fuseau)."""
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{numero.replace('-', '')}/{numero}.hdr.sgml"
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", sec_get(url).decode("latin-1"))
    local = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    return local.isoformat() + f"{decalage_new_york(local):+03d}:00"


def tickers_cik():
    data = json.loads(sec_get("https://www.sec.gov/files/company_tickers.json"))
    return {v["ticker"].upper(): v["cik_str"] for v in data.values()}


def depots(cik):
    """8-K item 2.02 deposes depuis DEPUIS : [(numero, date de depot, document)]."""
    doc = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    blocs = [doc["filings"]["recent"]]
    for f in doc["filings"].get("files", []):
        if f.get("filingTo", "9999") >= DEPUIS:
            blocs.append(json.loads(sec_get(f"https://data.sec.gov/submissions/{f['name']}")))
    res = {}
    for b in blocs:
        for i, forme in enumerate(b["form"]):
            if forme == "8-K" and "2.02" in (b["items"][i] or "") and b["filingDate"][i] >= DEPUIS:
                res[b["accessionNumber"][i]] = (b["filingDate"][i], b["primaryDocument"][i])
    return sorted(((n, j, d) for n, (j, d) in res.items()), key=lambda x: (x[1], x[0]))


# ---------------------------------------------------------------------------
# Comptes (XBRL)
# ---------------------------------------------------------------------------

def _jours(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


class Comptes:
    """Valeurs XBRL d'une societe :
    {poste: {(debut, fin): [(depose, numero, valeur, fy, fp)]}}."""

    def __init__(self, faits):
        self.v = {}
        for poste in CA + RESULTAT:
            unites = ((faits.get("facts", {}).get("us-gaap", {}).get(poste) or {}).get("units") or {})
            for f in unites.get("USD", []):
                if "start" not in f:
                    continue
                self.v.setdefault(poste, {}).setdefault((f["start"], f["end"]), []).append(
                    (f.get("filed", ""), f.get("accn"), f["val"], f.get("fy"), f.get("fp")))
        for p in self.v.values():
            for k in p:
                p[k].sort(key=lambda f: f[0])

    def fins(self):
        """Fins de periode connues : {fin: "trimestre" | "exercice"}."""
        res = {}
        for p in self.v.values():
            for (debut, fin) in p:
                n = _jours(debut, fin)
                if 80 <= n <= 100:
                    res[fin] = "trimestre"
                elif 350 <= n <= 380:
                    res.setdefault(fin, "exercice")
        return res

    def _valeur(self, poste, debut, fin, numero=None):
        """Valeur publiee pour la periode : celle du depot `numero` s'il la
        donne (comparatif de l'an dernier dans les comptes de l'annee), sinon
        la premiere publiee."""
        faits = self.v.get(poste, {}).get((debut, fin))
        if not faits:
            return None
        meme = [f for f in faits if f[1] == numero]
        return (meme or faits)[0]

    def _periode(self, poste, fin, duree):
        """(debut, fin) d'une periode du poste finissant a `fin` et durant
        environ `duree` jours (exercices de 52 ou 53 semaines compris)."""
        for (debut, f) in self.v.get(poste, {}):
            if f == fin and abs(_jours(debut, f) - duree) <= 12:
                return debut, f
        return None

    def valeur(self, poste, fin, numero=None, exercice=False):
        """Trimestre finissant a `fin` : direct, ou exercice moins neuf mois
        (4e trimestre) ; avec `exercice`, l'exercice entier.
        (valeur, numero du depot, fy, fp) ou None."""
        ex = self._periode(poste, fin, 365)
        if exercice:
            if not ex:
                return None
            f = self._valeur(poste, *ex, numero)
            return f[2], f[1], f[3], "FY"
        p = self._periode(poste, fin, 91)
        if p:
            f = self._valeur(poste, *p, numero)
            return f[2], f[1], f[3], f[4]
        if not ex:
            return None
        neuf = next(((d, f) for (d, f) in self.v.get(poste, {}) if d == ex[0] and 255 <= _jours(d, f) <= 290),
                    None)
        if not neuf:
            return None
        a, b = self._valeur(poste, *ex, numero), self._valeur(poste, *neuf)
        return a[2] - b[2], a[1], a[3], "Q4"

    def variation(self, postes, fin):
        """Periode finissant a `fin` face a la meme periode un an plus tot (meme
        poste). Le trimestre d'abord ; a defaut (4e trimestre sans les neuf
        mois), l'exercice. (variation %, fy, fp) ou None."""
        an_dernier = (dt.date.fromisoformat(fin) - dt.timedelta(days=364)).isoformat()
        for exercice in (False, True):
            for poste in postes:
                cur = self.valeur(poste, fin, exercice=exercice)
                if cur is None:
                    continue
                fin_av = next((f for (_, f) in self.v.get(poste, {}) if abs(_jours(f, an_dernier)) <= 10), None)
                prev = self.valeur(poste, fin_av, cur[1], exercice) if fin_av else None
                if prev is None or not prev[0]:
                    continue
                return round((cur[0] - prev[0]) / abs(prev[0]) * 100, 1), cur[2], cur[3]
        return None


# Champs des versions precedentes (sens face a l'an dernier), retires.
OBSOLETES = ("activite", "rentabilite", "ca_pct", "resultat_pct", "sens")


def titre(p):
    """Titre affiche : "Résultats T3 2026"."""
    if not p.get("fin"):
        return "Résultats (comptes pas encore déposés)"
    t, ex = p.get("trimestre"), p.get("exercice")
    quand = (f"annuels {ex}" if t == "FY" and ex else f"T{t[1]} {ex}" if t and t[0] == "Q" and ex
             else f"au {p['fin']}")
    return f"Résultats {quand}"


def publications(cik, liste, comptes, aujourd_hui, heures):
    """Publications de resultats : periode publiee. `heures` :
    heures d'acceptation deja connues (numero -> heure)."""
    fins = comptes.fins()
    prises, res = set(), []
    for numero, jour, document in liste:
        avant = [f for f in fins if _jours(f, jour) >= 5]
        fin = max(avant) if avant else None
        pub = {"id": numero,
               "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{numero.replace('-', '')}/{document}"}
        if fin and _jours(fin, jour) <= (DELAI_EXERCICE if fins[fin] == "exercice" else DELAI_TRIMESTRE):
            if fin in prises:
                continue  # autre depot 2.02 sur la meme periode (resultats preliminaires, rectificatif...)
            prises.add(fin)
            ca, rn = comptes.variation(CA, fin), comptes.variation(RESULTAT, fin)
            fy, fp = next(((x[1], x[2]) for x in (ca, rn) if x and x[1]), (None, None))
            pub.update({"fin": fin, "periode": "annuels" if fp in ("Q4", "FY") else "trimestriels",
                        "exercice": fy, "trimestre": fp})
        elif _jours(jour, aujourd_hui) > EN_ATTENTE:
            continue  # ancien depot sans periode reconnue
        pub["publie_le"] = heures.get(numero) or acceptation(cik, numero)
        pub["titre"] = titre(pub)
        res.append({k: v for k, v in pub.items() if v is not None})
    return res


# ---------------------------------------------------------------------------
# Mise a jour
# ---------------------------------------------------------------------------

def univers():
    with open(os.path.join(ICI, "univers_objectifs.json"), encoding="utf-8") as f:
        return [e for e in json.load(f)["entreprises"] if e["zone"] == "usa"]


def charger():
    if not os.path.exists(SORTIE):
        return {"version": 1, "depuis": DEPUIS, "maj": None, "a_relire": [], "entreprises": {}}
    with open(SORTIE, encoding="utf-8") as f:
        doc = json.load(f)
    for e in doc.get("entreprises", {}).values():  # champs des versions precedentes
        for p in e.get("publications", []):
            if any(k in p for k in OBSOLETES):
                for k in OBSOLETES:
                    p.pop(k, None)
                p["titre"] = titre(p)
    return doc


def ecrire(doc):
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "source": "SEC EDGAR (8-K item 2.02, comptes XBRL)",\n')
        f.write(f' "depuis": {json.dumps(doc["depuis"])},\n "maj": {json.dumps(doc["maj"])},\n')
        f.write(f' "a_relire": {json.dumps(doc.get("a_relire", []))},\n "entreprises": {{\n')
        f.write(",\n".join(f'  {json.dumps(t)}: {json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                           for t, v in sorted(doc["entreprises"].items())))
        f.write('\n }\n}\n')


def deposants_depuis(jour):
    """CIK ayant depose un 8-K, 10-Q ou 10-K depuis `jour` inclus (index
    quotidiens de la SEC)."""
    ciks = set()
    d = dt.date.fromisoformat(jour)
    while d <= dt.date.today():
        if d.weekday() < 5:
            url = (f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{(d.month - 1) // 3 + 1}/"
                   f"form.{d.strftime('%Y%m%d')}.idx")
            try:
                texte = sec_get(url).decode("latin-1")
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404):  # jour ferie ou index pas encore publie
                    raise
                texte = ""
            # Colonnes : forme, nom (avec des espaces), CIK, date, fichier.
            for ligne in texte.splitlines():
                morceaux = ligne.split()
                if ligne[:12].strip() in ("8-K",) + FORMES_COMPTES and len(morceaux) >= 4 \
                        and morceaux[-3].isdigit():
                    ciks.add(int(morceaux[-3]))
        d += dt.timedelta(days=1)
    return ciks


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--complet", action="store_true", help="relit toutes les entreprises")
    args = p.parse_args()
    doc = charger()
    doc["depuis"] = DEPUIS
    aujourd_hui = dt.date.today().isoformat()
    cik_de = tickers_cik()

    # Une ligne par societe (GOOGL et GOOG, FOXA et FOX... : un seul CIK).
    entreprises, vus, inconnus = [], set(), []
    for e in univers():
        cik = cik_de.get(e["ticker"].upper().replace(".", "-"))
        if cik is None:
            inconnus.append(e["ticker"])
        elif cik not in vus:
            vus.add(cik)
            entreprises.append((e, cik))

    if args.complet or not doc.get("maj"):
        a_lire = entreprises
    else:
        nouveaux = deposants_depuis(doc["maj"])
        a_relire = set(doc.get("a_relire", []))
        a_lire = [(e, c) for e, c in entreprises
                  if c in nouveaux or e["ticker"] in a_relire or e["ticker"] not in doc["entreprises"]]
    print(f"EDGAR : {len(a_lire)} entreprise(s) à relire sur {len(entreprises)}…", file=sys.stderr)

    def lire(e, cik):
        avant = (doc["entreprises"].get(e["ticker"]) or {}).get("publications", [])
        heures = {x["id"]: x["publie_le"] for x in avant}
        liste = depots(cik)
        try:
            faits = json.loads(sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
            faits = {}
        return {"cik": cik, "nom": e["nom"],
                "publications": publications(cik, liste, Comptes(faits), aujourd_hui, heures)}

    erreurs = {}
    with cf.ThreadPoolExecutor(4) as ex:
        futurs = {ex.submit(lire, e, c): e["ticker"] for e, c in a_lire}
        for i, fu in enumerate(cf.as_completed(futurs), 1):
            t = futurs[fu]
            try:
                doc["entreprises"][t] = fu.result()
            except Exception as err:
                erreurs[t] = str(err)[:150]
            if i % 50 == 0:
                print(f"  {i}/{len(a_lire)}", file=sys.stderr)
                ecrire(doc)  # reprise possible si le calcul est interrompu
    gardes = {e["ticker"] for e, _ in entreprises}
    doc["entreprises"] = {t: v for t, v in doc["entreprises"].items() if t in gardes}
    # Entreprises illisibles : relues au prochain passage.
    doc["maj"] = aujourd_hui
    doc["a_relire"] = sorted(erreurs)
    ecrire(doc)
    pubs = [x for v in doc["entreprises"].values() for x in v["publications"]]
    datees = sum(1 for x in pubs if x.get("fin"))
    print(f"resultats_usa.json : {len(doc['entreprises'])} entreprises, {len(pubs)} publications de résultats, "
          f"{datees} avec période (comptes déposés)"
          + (f" ; tickers inconnus de la SEC : {inconnus}" if inconnus else "")
          + (f" ; illisibles : {erreurs}" if erreurs else "") + ".")


if __name__ == "__main__":
    main()
