#!/usr/bin/env python3
"""Objectifs de cours des analystes : historique pour mesurer, a l'horizon de
12 mois, l'ecart entre le cours et l'objectif (scores.py, onglet Statistiques
> Scores).

Deux sources :
  - Yahoo Finance, releve quotidien : objectif moyen, median, haut et bas,
    nombre d'analystes, note moyenne (1 = achat fort, 5 = vente) et
    repartition des recommandations. Yahoo ne garde aucun historique pour les
    actions francaises : il se construit au fil des releves (un releve garde
    s'il change) ;
  - Zonebourse (donnees S&P Global Market Intelligence) : objectif moyen jour
    par jour sur environ 5 ans, et fourchette haut-bas, lus dans le graphique
    << Evolution de l'objectif de cours >> de la page Consensus (image SVG
    dont on convertit les coordonnees en dates et en euros grace aux lignes de
    la grille ; precision d'environ 0,5 % du cours). Lu une fois le
    1er octobre 2026 pour tout le referentiel ; le releve Yahoo quotidien
    prend le relais. A relancer a la main seulement pour une entreprise
    ajoutee au referentiel (--zonebourse --ticker) ; une lecture couvre les
    5 dernieres annees, les valeurs plus anciennes deja connues sont gardees.

Zonebourse refuse les scripts qui se presentent comme un navigateur ; il
repond a un client en ligne de commande (en-tete de curl). Une requete toutes
les quelques secondes, a la main seulement : usage personnel uniquement.

Ecrit objectifs.json. Une source en erreur n'arrete rien : le fichier
precedent est garde.

Usage :
  python objectifs.py                  releve Yahoo du jour
  python objectifs.py --zonebourse --ticker CAP.PA
                                       historique Zonebourse d'une entreprise
                                       (sans --ticker : tout le referentiel,
                                       environ 45 minutes)
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
import urllib.parse
import urllib.request

from collecte import _yahoo_get
from consensus import _raw, session

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "objectifs.json")
ZB = "https://www.zonebourse.com"
ZB_UA = "curl/8.5.0"
ZB_PAUSE = 3  # secondes entre deux requetes Zonebourse
MODULES = "financialData,recommendationTrend"
MOIS = {"janv": 1, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "aout": 8, "sept": 9,
        "oct": 10, "nov": 11, "dec": 12}


# ---------------------------------------------------------------------------
# Yahoo : releve du jour
# ---------------------------------------------------------------------------

def lire_yahoo(ticker, opener, crumb):
    """[moyen, median, haut, bas, n_analystes, note_moyenne, [achat fort,
    achat, conserver, vendre, vente forte]] ou None si Yahoo n'a rien."""
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
           f"?modules={MODULES}&crumb={urllib.parse.quote(crumb)}")
    r = json.loads(_yahoo_get(url, opener))["quoteSummary"]["result"][0]
    fd = r.get("financialData") or {}
    moyen = _raw(fd, "targetMeanPrice")
    if not moyen:
        return None
    trend = [x for x in (r.get("recommendationTrend") or {}).get("trend", []) if x.get("period") == "0m"]
    reco = ([trend[0].get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")] if trend else None)
    return [round(moyen, 2), _arr(_raw(fd, "targetMedianPrice")), _arr(_raw(fd, "targetHighPrice")),
            _arr(_raw(fd, "targetLowPrice")), _raw(fd, "numberOfAnalystOpinions"),
            _arr(_raw(fd, "recommendationMean")), reco]


def _arr(x):
    return round(x, 2) if isinstance(x, (int, float)) else None


def releve_yahoo(doc, tickers, jour):
    try:
        opener, crumb = session()
    except Exception as e:
        print(f"Objectifs indisponibles (Yahoo) : {e}", file=sys.stderr)
        return
    erreurs, nouveaux = {}, 0
    with cf.ThreadPoolExecutor(4) as ex:
        futurs = {ex.submit(lire_yahoo, t, opener, crumb): t for t in tickers}
        for fu in cf.as_completed(futurs):
            t = futurs[fu]
            try:
                v = fu.result()
            except Exception as e:
                erreurs[t] = str(e)[:120]
                continue
            if v is None:
                continue
            rel = doc["entreprises"].setdefault(t, {}).setdefault("yahoo", [])
            if rel and rel[-1][0] == jour:
                rel.pop()
            if rel and rel[-1][1:] == v:
                continue  # inchange : le releve precedent vaut toujours
            rel.append([jour] + v)
            nouveaux += 1
    doc["maj_yahoo"] = jour
    print(f"objectifs.json (Yahoo) : {nouveaux} releve(s) nouveau(x)"
          + (f" ; illisibles : {sorted(erreurs)}" if erreurs else "") + ".")


# ---------------------------------------------------------------------------
# Zonebourse : historique de l'objectif moyen
# ---------------------------------------------------------------------------

def zb_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": ZB_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def zb_page(isin):
    """Chemin Zonebourse de l'action (ex. CAPGEMINI-SE-4624) d'apres la
    recherche par ISIN : premier resultat de la liste des instruments."""
    b = zb_get(f"{ZB}/recherche/?q={isin}")
    m = re.search(r'class="link link--blue txt-inline ml-5" href="/cours/action/([A-Za-z0-9-]+)/', b)
    return m.group(1) if m else None


def _nombres(d):
    v = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", d)]
    return list(zip(v[0::2], v[1::2]))


def _mois(libelle):
    """'Janv. 22' -> (2022, 1)."""
    s = html.unescape(libelle).lower().strip()
    s = s.translate(str.maketrans("éèêûô", "eeeuo"))
    m = re.match(r"([a-z]+)\.?\s+(\d{2})$", s)
    if not m or m.group(1)[:4] not in {k[:4] for k in MOIS}:
        return None
    mois = next(v for k, v in MOIS.items() if k[:4] == m.group(1)[:4])
    return 2000 + int(m.group(2)), mois


def lire_svg(svg):
    """Graphique Highcharts << Evolution de l'objectif de cours >> ->
    (devise, {jour: moyen}, {jour: [haut, bas]}).

    Axe des dates : lignes verticales de la grille, une par libelle de mois
    (<< Janv. 22 >>...). Axe des objectifs : lignes horizontales du panneau du
    haut, une par libelle (150, 200...) ; la courbe << Objectif de cours Moyen
    >> (serie 0) et la fourchette haut-bas (serie 1) sont dans ce panneau."""
    devise = re.search(r">Objectif de cours \(([A-Z]{3})\)<", svg)
    # Grille : groupes dans l'ordre du document (x d'abord, puis y : panneau
    # du haut, panneau du bas).
    grilles = re.findall(r'<g[^>]*class="highcharts-grid highcharts-([xy])axis-grid"[^>]*>(.*?)</g>', svg, re.S)
    def debuts(g):
        return [p[0] for p in (_nombres(d) for d in re.findall(r' d="([^"]+)"', g)) if p]

    gx = sorted({x for a, g in grilles if a == "x" for x, _ in debuts(g)})
    gy_haut = next((sorted({y for _, y in debuts(g)}) for a, g in grilles if a == "y"), [])
    # Libelles des axes : (texte, x, y, masque) par groupe, dans l'ordre du
    # document (axe des valeurs : panneau du haut d'abord).
    groupes = []
    for grp, contenu in re.findall(r'<g[^>]*class="highcharts-axis-labels highcharts-([xy])axis-labels[^"]*"[^>]*>'
                                   r'(.*?)</g>', svg, re.S):
        libs = []
        for attrs, texte in re.findall(r"<text([^>]*)>([^<]*)</text>", contenu):
            x = re.search(r'\bx="([\d.]+)"', attrs)
            y = re.search(r'\by="([\d.]+)"', attrs)
            libs.append((texte, float(x.group(1)) if x else None, float(y.group(1)) if y else None,
                         "hidden" in attrs))
        groupes.append((grp, libs))
    libx = [l for grp, libs in groupes if grp == "x" for l in libs if not l[3]]
    liby = next((libs for grp, libs in groupes if grp == "y"), [])
    # Dates : chaque libelle de mois correspond a la ligne de grille la plus
    # proche a sa gauche (le libelle est decale de quelques pixels).
    points_x = []
    for texte, x, _, _ in libx:
        m = _mois(texte)
        if m is None or x is None:
            continue
        g = max((v for v in gx if v <= x + 1), default=None)
        if g is not None and x - g < 10:
            points_x.append((g, dt.date(m[0], m[1], 1).toordinal()))
    # Valeurs : une ligne de grille par graduation, y compris la premiere
    # (masquee, sans position) ; la plus basse valeur sur la ligne la plus
    # basse. Sinon, chaque graduation visible sur la ligne juste au-dessus
    # de son texte.
    valeurs = []
    for texte, _, y, masque in liby:
        try:
            valeurs.append((_graduation(texte), y, masque))
        except ValueError:
            continue
    if len(valeurs) == len(gy_haut):
        points_y = list(zip(sorted(gy_haut, reverse=True), sorted(v for v, _, _ in valeurs)))
    else:
        points_y = []
        for v, y, masque in valeurs:
            g = None if masque or y is None else max((w for w in gy_haut if w <= y), default=None)
            if g is not None and y - g < 10:
                points_y.append((g, v))
    if len(points_x) < 2 or len(points_y) < 2:
        raise ValueError("axes illisibles")
    ax, bx = _droite(points_x)
    ay, by = _droite(points_y)

    def series(n):
        m = re.search(r'<g[^>]*class="highcharts-series highcharts-series-%d [^"]*"[^>]*'
                      r'transform="translate\(([\d.-]+),([\d.-]+)\)[^"]*"[^>]*>(.*?)</g>' % n, svg, re.S)
        if not m:
            return None, []
        dx, dy = float(m.group(1)), float(m.group(2))
        return (dx, dy), re.findall(r'<path([^>]*)>', m.group(3))

    def jour(x):
        return dt.date.fromordinal(round(ax + bx * x)).isoformat()

    def valeur(y):
        return round(ay + by * y, 2)

    moyen, fourchette = {}, {}
    (dec, chemins) = series(0)
    for attrs in chemins:
        if 'class="highcharts-graph"' in attrs:
            dx, dy = dec
            for x, y in _nombres(re.search(r' d="([^"]+)"', attrs).group(1)):
                moyen[jour(x + dx)] = valeur(y + dy)
            break
    (dec, chemins) = series(1)
    for attrs in chemins:
        if 'class="highcharts-area"' in attrs:
            dx, dy = dec
            pts = _nombres(re.search(r' d="([^"]+)"', attrs).group(1))
            # Aller le long d'une borne, retour le long de l'autre.
            i = next((k for k in range(1, len(pts)) if pts[k][0] < pts[k - 1][0] - 1e-6), len(pts))
            aller = {jour(x + dx): valeur(y + dy) for x, y in pts[:i]}
            retour = {jour(x + dx): valeur(y + dy) for x, y in pts[i:]}
            fourchette = {j: [max(aller[j], retour[j]), min(aller[j], retour[j])] for j in aller if j in retour}
            break
    if not moyen:
        raise ValueError("courbe de l'objectif moyen introuvable")
    return devise.group(1) if devise else None, moyen, fourchette


def _graduation(texte):
    """'150' -> 150 ; '2k.' -> 2000 ; '1,5M.' -> 1500000."""
    t = html.unescape(texte).replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".").rstrip(".")
    facteur = {"k": 1e3, "M": 1e6}.get(t[-1:], 1)
    return float(t[:-1] if facteur != 1 else t) * facteur


def _droite(points):
    """Moindres carres v = a + b * pixel."""
    n = len(points)
    mx = sum(p for p, _ in points) / n
    mv = sum(v for _, v in points) / n
    b = sum((p - mx) * (v - mv) for p, v in points) / sum((p - mx) ** 2 for p, _ in points)
    return mv - b * mx, b


def _compresser(serie):
    """{jour: valeur} -> [[jour, valeur]] en ne gardant que les changements."""
    out = []
    for j in sorted(serie):
        if not out or out[-1][1] != serie[j]:
            out.append([j, serie[j]])
    return out


def _etendre(liste):
    """[[jour, valeur]] (changements) -> {jour: valeur} pour les seuls jours
    listes (suffit a la fusion : on garde les jours anterieurs a la lecture)."""
    return {j: v for j, v in liste}


def releve_zonebourse(doc, entreprises, jour):
    ok, erreurs = 0, {}
    for e in entreprises:
        t = e["ticker"]
        fiche = doc["entreprises"].setdefault(t, {})
        try:
            if not fiche.get("zonebourse"):
                fiche["zonebourse"] = zb_page(e["isin"])
                time.sleep(ZB_PAUSE)
                if not fiche["zonebourse"]:
                    raise ValueError("introuvable dans la recherche")
            page = zb_get(f"{ZB}/cours/action/{fiche['zonebourse']}/consensus/")
            time.sleep(ZB_PAUSE)
            # La page a plusieurs graphiques sous le meme nom de fichier :
            # celui qui suit le titre << Evolution de l'Objectif de Cours >>
            # (objectif moyen et fourchette haut-bas).
            titre = re.search(r"Evolution de l(?:'|&#039;|&#39;|’)Objectif de Cours", page)
            # Version la plus large (la plus precise) : l'<img> du <picture>.
            m = re.search(r'<img src="(https://cdn\.zonebourse\.com/static/hes/[^"\s]+)"',
                          page[titre.end():]) if titre else None
            if not m:
                raise ValueError("pas de graphique d'objectif (pas de consensus ?)")
            svg = zb_get(html.unescape(m.group(1)))
            time.sleep(ZB_PAUSE)
            devise, moyen, fourchette = lire_svg(svg)
            # Un pixel vaut un jour et demi environ : la derniere date lue
            # peut tomber apres aujourd'hui. On decale alors toute la serie.
            exces = (dt.date.fromisoformat(max(moyen)) - dt.date.fromisoformat(jour)).days
            if exces > 0:
                def recule(j):
                    return (dt.date.fromisoformat(j) - dt.timedelta(days=exces)).isoformat()
                moyen = {recule(j): v for j, v in moyen.items()}
                fourchette = {recule(j): v for j, v in fourchette.items()}
        except Exception as ex:
            erreurs[t] = str(ex)[:120]
            continue
        # Fusion : la lecture remplace tout ce qu'elle couvre ; les jours plus
        # anciens deja connus sont gardes.
        debut = min(moyen)
        ancien = {j: v for j, v in fiche.get("moyen", []) if j < debut}
        ancien_f = {j: v for j, *v in fiche.get("fourchette", []) if j < debut}
        fiche["devise"] = devise
        fiche["moyen"] = _compresser({**ancien, **moyen})
        fiche["fourchette"] = [[j] + v for j, v in _compresser({**ancien_f, **fourchette})]
        fiche["zonebourse_lu"] = jour
        ok += 1
        print(f"  {t} : {fiche['zonebourse']}, {min(moyen)} -> {max(moyen)}, "
              f"objectif moyen {moyen[max(moyen)]} {devise}", file=sys.stderr)
    doc["maj_zonebourse"] = jour
    print(f"objectifs.json (Zonebourse) : {ok} entreprise(s) lue(s)"
          + (f" ; illisibles : {erreurs}" if erreurs else "") + ".")


# ---------------------------------------------------------------------------

def charger():
    if not os.path.exists(SORTIE):
        return {"version": 1, "entreprises": {}}
    with open(SORTIE, encoding="utf-8") as f:
        return json.load(f)


def ecrire(doc):
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "source": "Yahoo Finance (releve quotidien), Zonebourse / S&P Global '
                'Market Intelligence (historique de l\'objectif moyen)",\n')
        f.write(f' "maj_yahoo": {json.dumps(doc.get("maj_yahoo"))},\n')
        f.write(f' "maj_zonebourse": {json.dumps(doc.get("maj_zonebourse"))},\n "entreprises": {{\n')
        f.write(",\n".join(f'  {json.dumps(t)}: {json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                           for t, v in sorted(doc["entreprises"].items())))
        f.write('\n }\n}\n')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zonebourse", action="store_true", help="lit l'historique Zonebourse")
    p.add_argument("--ticker", action="append", help="limite Zonebourse a ces entreprises")
    args = p.parse_args()
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        entreprises = json.load(f)["entreprises"]
    doc = charger()
    jour = dt.date.today().isoformat()
    if doc.get("maj_yahoo") != jour:
        releve_yahoo(doc, [e["ticker"] for e in entreprises], jour)
    else:
        print(f"objectifs.json (Yahoo) : deja releve aujourd'hui ({jour}).")
    dernier = doc.get("maj_zonebourse")
    if args.zonebourse:
        cibles = [e for e in entreprises if not args.ticker or e["ticker"] in args.ticker]
        releve_zonebourse(doc, cibles, jour)
        if args.ticker:
            doc["maj_zonebourse"] = dernier  # releve partiel : garde la date du releve complet
    ecrire(doc)


if __name__ == "__main__":
    main()
