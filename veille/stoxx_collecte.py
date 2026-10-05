#!/usr/bin/env python3
"""Publications de resultats des entreprises du STOXX Europe 600 qui ne font
pas deja partie du marche francais de l'application (CAC 40 et SBF 120), depuis
2015 : onglet Statistiques > Communiques > STOXX 600.

Aucune source gratuite ne couvre toute l'Europe : on assemble les meilleures,
et chaque publication garde sa source.

  - Yahoo Finance (calendrier des resultats, via yfinance) : date, BPA estime
    par les analystes avant la publication et BPA publie. C'est le seul
    historique gratuit du consensus ; il manque pour environ 40 % des
    entreprises (Royaume-Uni, Suisse, Pays-Bas, Belgique surtout). Les heures
    de Yahoo sont celles de New York, souvent a 00 h UTC quand l'heure est
    inconnue : la date UTC est la bonne date.
  - Investegate (Royaume-Uni) : toutes les annonces reglementees RNS de la
    categorie « Results and Trading Reports », avec l'heure exacte.
  - Nasdaq Nordic (Suede, Danemark, Finlande) : annonces reglementees des
    categories de rapports financiers, avec l'heure exacte.

Les donnees brutes sont gardees dans stoxx/ ; `--fusion` ecrit
resultats_stoxx.json (une liste de publications par entreprise), lu par
etude_stoxx.py.

Usage :
  python stoxx_collecte.py --yahoo       calendrier Yahoo (yfinance requis ; reprise possible)
  python stoxx_collecte.py --uk          annonces Investegate (--complement : 2015-2018 seulement, ajoutees a l'existant)
  python stoxx_collecte.py --nordique    annonces Nasdaq Nordic
  python stoxx_collecte.py --fusion      resultats_stoxx.json
  python stoxx_collecte.py --recent      mise a jour quotidienne (veille de 13 h 12) : annonces des
                                         RECENT derniers jours des trois sources, puis --fusion
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

ICI = os.path.dirname(os.path.abspath(__file__))
DOSSIER = os.path.join(ICI, "stoxx")
SORTIE = os.path.join(ICI, "resultats_stoxx.json")
DEPUIS = "2015-01-01"
RECENT = 14  # jours relus par --recent
FIN_COMPLEMENT = "2018-12-31"  # --uk --complement : seulement les annonces avant l'ancien debut (2019)
_fin = [None]  # date de fin de la recherche Investegate (aujourd'hui par defaut)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
# Doublons de valeurs deja suivies sur le marche francais (autre place de cotation).
DEJA_FRANCAIS = {"STMMI.MI"}
# Noms exacts des entreprises sur Nasdaq Nordic quand le nom de l'indice ne suffit pas.
NOMS_NORDIQUES = {
    "NSIS-B.CO": "Novonesis (Novozymes A/S)", "WRT1V.HE": "Wärtsilä", "ORSTED.CO": "Ørsted A/S",
    "INDU-C.ST": "Industrivärden, AB", "RILBA.CO": "Ringkjøbing Landbobank A/S",
    "LUND-B.ST": "Lundbergföretagen AB, L E", "FLS.CO": "FLSmidth & Co. A/S", "HUH1V.HE": "Huhtamäki Oyj",
    "CARL-B.CO": "Carlsberg A/S", "MAERSK-B.CO": "A.P. Møller - Mærsk A/S",
    "ERIC-B.ST": "Ericsson, Telefonab. L M", "VOLV-B.ST": "Volvo, AB", "INVE-B.ST": "Investor AB",
}


def lire_json(chemin, defaut=None):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def ecrire_json(chemin, doc):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def univers():
    """Entreprises du STOXX 600 hors marche francais (CAC 40 et SBF 120) : liste
    de dicts ticker, nom, pays, secteur."""
    ref = {e["ticker"] for e in lire_json(os.path.join(ICI, "referentiel.json"))["entreprises"]}
    res = []
    for e in lire_json(os.path.join(ICI, "univers_objectifs.json"))["entreprises"]:
        if "STOXX 600" in e["indices"] and e["ticker"] not in ref and e["ticker"] not in DEJA_FRANCAIS:
            res.append(e)
    return res


# ---------------------------------------------------------------------------
# Heures
# ---------------------------------------------------------------------------

def _dernier_dimanche(annee, mois):
    d = dt.datetime(annee, mois + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def decalage_europe(local_naif, base):
    """Heures a retirer a une heure locale pour avoir l'heure UTC : `base` = 1
    (Paris, Francfort...) ou 0 (Londres) en hiver, +1 en ete (meme calendrier
    de l'heure d'ete dans toute l'Europe)."""
    a = local_naif.year
    debut = _dernier_dimanche(a, 3).replace(hour=2)
    fin = _dernier_dimanche(a, 10).replace(hour=3)
    return base + (1 if debut <= local_naif < fin else 0)


def en_utc(local_naif, base):
    return (local_naif - dt.timedelta(hours=decalage_europe(local_naif, base))).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------

def collecter_yahoo():
    import yfinance as yf  # seul module hors bibliotheque standard (collecte unique, a la main)
    chemin = os.path.join(DOSSIER, "yahoo.json")
    doc = lire_json(chemin, {})
    todo = [e for e in univers() if e["ticker"] not in doc or isinstance(doc[e["ticker"]], dict)]
    print(f"Yahoo : {len(todo)} entreprise(s) à lire…", file=sys.stderr)

    def lire(e):
        t = e["ticker"]
        err = ""
        for _ in range(3):
            try:
                df = yf.Ticker(t).get_earnings_dates(limit=100)
                lignes = []
                if df is not None and not df.empty:
                    for ts, r in df.iterrows():
                        lignes.append([ts.isoformat(), *[None if r[c] != r[c] else float(r[c])
                                                         for c in ("EPS Estimate", "Reported EPS", "Surprise(%)")]])
                return t, lignes
            except Exception as ex:
                err = str(ex)[:100]
                time.sleep(2)
        return t, {"erreur": err}

    with cf.ThreadPoolExecutor(5) as ex:
        for i, (t, r) in enumerate(ex.map(lire, todo), 1):
            doc[t] = r
            if i % 50 == 0:
                ecrire_json(chemin, doc)
                print(f"  {i}/{len(todo)}", file=sys.stderr)
    ecrire_json(chemin, doc)
    sans = [t for t, r in doc.items() if not r or isinstance(r, dict)]
    print(f"stoxx/yahoo.json : {len(doc)} entreprises, {len(sans)} sans calendrier.")


# ---------------------------------------------------------------------------
# Investegate (Royaume-Uni)
# ---------------------------------------------------------------------------

INV_UA = dict(UA, **{"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.investegate.co.uk/advanced-search"})


def _inv_page(mot, recherche, page, depuis=DEPUIS):
    q = urllib.parse.urlencode({"search_for": recherche, "search_word": mot, "date_from": depuis,
                                "date_to": _fin[0] or dt.date.today().isoformat(), "categories[]": 2,
                                "exclude_navs": "true", "page": page})
    req = urllib.request.Request("https://www.investegate.co.uk/advanced-search/draw?" + q, headers=INV_UA)
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return f.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(2)
    return ""


def _inv_lignes(b):
    res = []
    for r in re.findall(r"<tr.*?</tr>", b, re.S):
        m = re.search(r"<td>(\d\d \w{3} \d{4}) (\d\d:\d\d [AP]M)</td>", r)
        a = re.search(r'href="(https://www.investegate.co.uk/announcement/[^"]+)">([^<]+)</a>', r)
        c = re.search(r'company/([A-Z0-9.]+)">([^<]+)</a>\s*</div>\s*</div>', r)
        if m and a:
            t = dt.datetime.strptime(m.group(1) + " " + m.group(2), "%d %b %Y %I:%M %p")
            res.append({"t": t.isoformat(), "titre": html.unescape(a.group(2)).strip(), "url": a.group(1),
                        "co": html.unescape(c.group(2)) if c else ""})
    return res


def _epic(co):
    m = re.search(r"\(([A-Z0-9.]+)\)\s*$", co or "")
    return re.sub(r"\.$", "", m.group(1)) if m else None


def collecter_uk(complement=False):
    chemin = os.path.join(DOSSIER, "uk.json")
    doc = lire_json(chemin, {})
    if complement:
        _fin[0] = FIN_COMPLEMENT
        uk = [e for e in univers() if e["ticker"].endswith(".L") and doc.get(e["ticker"], {}).get("annonces")
              and not doc[e["ticker"]].get("complement")]
    else:
        uk = [e for e in univers() if e["ticker"].endswith(".L") and not doc.get(e["ticker"], {}).get("annonces")]

    def lire(e):
        tidm = re.sub(r"\.$", "", e["ticker"][:-2].replace("-", "."))
        nom = re.sub(r"[^A-Za-z0-9&\- ]", " ", e["nom"]).split()
        essais = [(" ".join(nom[:2]), 1), (" ".join(nom[:1]), 1), (tidm, 2)]
        if len(tidm) <= 3:
            essais.insert(0, (tidm, 1))
        if complement:  # meme recherche que celle qui a trouve les annonces recentes
            essais = [(doc[e["ticker"]]["recherche"], 2 if doc[e["ticker"]]["recherche"] == tidm else 1)]
        for mot, recherche in essais:
            if len(mot) < 2:
                continue
            tot = []
            for page in range(1, 60):
                ls = _inv_lignes(_inv_page(mot, recherche, page))
                if not ls:
                    break
                tot += [x for x in ls if _epic(x["co"]) == tidm]
                if page >= 4 and not tot:
                    break
            if tot:
                return e["ticker"], {"recherche": mot, "annonces": tot}
        return e["ticker"], {"recherche": None, "annonces": []}

    with cf.ThreadPoolExecutor(4) as ex:
        for i, (t, r) in enumerate(ex.map(lire, uk), 1):
            if complement:
                connues = {x["url"] for x in doc[t]["annonces"]}
                doc[t]["annonces"] += [x for x in r["annonces"] if x["url"] not in connues]
                doc[t]["complement"] = FIN_COMPLEMENT
            else:
                doc[t] = r
            if i % 10 == 0:
                ecrire_json(chemin, doc)
                print(f"  {i}/{len(uk)}", file=sys.stderr)
    ecrire_json(chemin, doc)
    vides = [t for t, r in doc.items() if not r["annonces"]]
    print(f"stoxx/uk.json : {len(doc)} entreprises, sans annonce : {vides}")


# ---------------------------------------------------------------------------
# Nasdaq Nordic (Suede, Danemark, Finlande)
# ---------------------------------------------------------------------------

CATEGORIES_NORDIQUES = ["Interim report (Q1 and Q3)", "Half Year financial report", "Financial Statement Release",
                        "Quarterly report", "Interim Management statement"]


def _nasdaq(**kw):
    p = {"countResults": "true", "globalGroup": "exchangeNotice", "displayLanguage": "en", "timeZone": "CET",
         "dateMask": "yyyy-MM-dd HH:mm:ss", "limit": 100, "start": 0, "globalName": "NordicAllMarkets"}
    p.update(kw)
    req = urllib.request.Request("https://api.news.eu.nasdaq.com/news/query.action?" + urllib.parse.urlencode(p),
                                 headers=UA)
    for essai in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return json.loads(f.read())["results"].get("item") or []
        except Exception:
            time.sleep(2)
    return None


def _nom_nasdaq(e, connus):
    if e["ticker"] in NOMS_NORDIQUES:
        return NOMS_NORDIQUES[e["ticker"]]
    return connus.get(e["ticker"])


def collecter_nordique():
    chemin = os.path.join(DOSSIER, "nordique.json")
    doc = lire_json(chemin, {})
    nord = [e for e in univers() if e["ticker"].rsplit(".", 1)[-1] in ("ST", "CO", "HE")]
    # Noms exacts : on cherche les entreprises du STOXX parmi celles qui ont publie un
    # rapport semestriel (le nom de l'indice differe : « Volvo Class B » / « Volvo, AB »).
    stop = re.compile(r"\b(ab|a/s|as|oyj|oy|plc|publ|abp|asa|ltd|limited|group|holding|holdings|corporation|corp|"
                      r"inc|class|series|b|a|c|ser|shs|pref|the|de|sa|nv|se)\b")

    def norm(s):
        s = re.sub(r"\([^)]*\)", "", s.lower())
        s = re.sub(r"[^a-z0-9åäöøæü ]", " ", s)
        return " ".join(stop.sub(" ", s).split())

    noms = {}
    for i in _nasdaq(cnscategory="Half Year financial report") or []:
        noms.setdefault(norm(i["company"]), i["company"])
    for s in range(100, 8000, 100):
        its = _nasdaq(cnscategory="Half Year financial report", start=s)
        if not its:
            break
        for i in its:
            noms.setdefault(norm(i["company"]), i["company"])
    connus = {}
    for e in nord:
        k = norm(e["nom"])
        if k in noms:
            connus[e["ticker"]] = noms[k]
        else:
            m = [v for kk, v in noms.items() if k and len(kk) >= 4 and (kk.startswith(k) or k.startswith(kk))]
            if m:
                connus[e["ticker"]] = m[0]

    def lire(e):
        nom = _nom_nasdaq(e, connus)
        if not nom:
            return e["ticker"], {"societe": None, "annonces": []}
        out = []
        for cat in CATEGORIES_NORDIQUES:
            for s in range(0, 2000, 100):
                its = _nasdaq(company=nom, cnscategory=cat, start=s)
                if not its:
                    break
                out += [{"t": i["releaseTime"], "titre": i["headline"], "categorie": cat, "langue": i["language"],
                         "url": i.get("messageUrl")} for i in its if i["releaseTime"] >= DEPUIS]
                if its[-1]["releaseTime"] < DEPUIS or len(its) < 100:
                    break
        return e["ticker"], {"societe": nom, "annonces": out}

    with cf.ThreadPoolExecutor(4) as ex:
        for t, r in ex.map(lire, nord):
            doc[t] = r
    ecrire_json(chemin, doc)
    vides = [t for t, r in doc.items() if not r["annonces"]]
    print(f"stoxx/nordique.json : {len(doc)} entreprises, sans annonce : {vides}")


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

# Annonces britanniques de resultats : titres reconnus (les titres des entreprises varient
# beaucoup, une annonce en dehors de ces formules n'est comptee que si Yahoo la confirme).
UK_RESULTATS = re.compile(r"\b(final results?|preliminary results?|interim results?|half[- ]year (?:report|results?|"
                          r"financial report)|half year (?:report|results?)|full[- ]year (?:results?|financial report)|"
                          r"year[- ]end results|results for the (?:year|six|half|period|twelve|nine|three|quarter)|"
                          r"annual results|fy ?\d\d (?:preliminary )?results|results announcement|interim management "
                          r"statement|(?:first|second|third|fourth|1st|2nd|3rd|4th) quarter results|q[1-4] "
                          r"(?:\d{4} )?results|\d{4} (?:interim |final |full[- ]year |half[- ]year )?results|"
                          r"results? for (?:fy|h[12]|q[1-4])|\bresults\b)", re.I)
UK_EXCLUS = re.compile(r"notice of|result(?:s)? of (?:the )?(?:agm|general|annual|meeting|placing|retail|open offer|tender|"
                       r"capital|scheme|court)|\bagm\b|annual general|general meeting|webcast|conference call|dial[- ]in|"
                       r"date of|publication of|posting|timing of|payments? to gov|production report|trading (?:update|"
                       r"statement)|pre-?close|report (?:and|&) accounts|annual report|annual financial report|"
                       r"financial statements|form 20|form 8|director|holding\(s\)|dealing|capital markets|"
                       r"investor (?:day|presentation)|statement re|transaction in|total voting|notification",
                       re.I)
UK_PERIODE = [("annuels", r"final|preliminary|full[- ]year|year[- ]end|year ended|annual results|\bfy\b|"
                          r"fourth quarter"),
              ("semestriels", r"interim|half[- ]year|half year|six months|\bh[12]\b|2nd quarter|second quarter"),
              ("trimestriels", r"quarter|\bq[1-4]\b|management statement")]


def periode_titre(titre):
    t = (titre or "").lower()
    for p, motif in UK_PERIODE:
        if re.search(motif, t):
            return p
    return None


def publications_uk(doc):
    """Annonces de resultats Investegate, une par jour (heure de Londres -> UTC).
    Un communique en plusieurs parties (« Final Results - Part 1 of 8 ») garde
    toutes ses parties dans `urls` (les perspectives sont rarement dans la premiere)."""
    jours = {}
    for x in doc.get("annonces", []):
        if UK_EXCLUS.search(x["titre"]) or not UK_RESULTATS.search(x["titre"]):
            continue
        t = dt.datetime.fromisoformat(x["t"])
        jours.setdefault(t.date().isoformat(), []).append((t, x))
    res = []
    for cle, liste in sorted(jours.items()):
        liste.sort(key=lambda p: (p[0], p[1]["titre"]))
        # Premiere annonce du jour (hors partie 2 et suivantes) pour le titre et l'heure.
        t, x = next(((t, x) for t, x in liste if not re.search(r"part [2-9]", x["titre"], re.I)), liste[0])
        urls = []
        for _, y in liste:
            if y["url"] not in urls:
                urls.append(y["url"])
        res.append({"publie_le": en_utc(t, 0), "heure": True, "titre": x["titre"],
                    "periode": periode_titre(x["titre"]), "source": "investegate", "url": x["url"],
                    "urls": urls[:12]})
    return res


def publications_nordiques(doc):
    """Rapports financiers Nasdaq Nordic, en anglais, une publication par jour (heure CET/CEST -> UTC)."""
    jours = {}
    for x in doc.get("annonces", []):
        if x.get("langue") != "en" and not any(y for y in doc["annonces"]
                                               if y["t"][:10] == x["t"][:10] and y.get("langue") == "en"):
            pass  # a defaut d'anglais, la version locale suffit
        t = dt.datetime.fromisoformat(x["t"])
        cle = t.date().isoformat()
        if cle not in jours or (x.get("langue") == "en" and jours[cle].get("langue") != "en"):
            jours[cle] = x
    res = []
    for cle, x in sorted(jours.items()):
        cat = x["categorie"]
        periode = ("trimestriels" if "Q1 and Q3" in cat or "Quarterly" in cat or "Management" in cat
                   else "semestriels" if "Half Year" in cat else "annuels")
        res.append({"publie_le": en_utc(dt.datetime.fromisoformat(x["t"]), 1), "heure": True,
                    "titre": x["titre"], "periode": periode, "source": "nasdaq", "url": x.get("url")})
    return res


def publications_yahoo(lignes, aujourd_hui):
    """Publications passees d'un calendrier Yahoo : BPA publie connu. L'heure de
    Yahoo (New York) n'est fiable que si l'instant UTC n'est pas une heure de
    nuit (00 h a 05 h UTC : date seule)."""
    res, vus = [], []
    for iso, est, pub, surprise in sorted(lignes or [], key=lambda x: x[0]):
        if pub is None:
            continue
        t = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc).replace(tzinfo=None)
        jour = t.date().isoformat()
        if jour < DEPUIS or jour > aujourd_hui:
            continue
        connue = t.hour >= 5
        if any(abs((dt.date.fromisoformat(jour) - dt.date.fromisoformat(v)).days) <= 3 for v in vus):
            continue
        vus.append(jour)
        res.append({"publie_le": t.strftime("%Y-%m-%dT%H:%M:%SZ") if connue else jour + "T00:00:00Z",
                    "heure": connue, "titre": None, "periode": None, "source": "yahoo",
                    "bpa_estime": est, "bpa_publie": pub})
    return res


def prochaine_yahoo(lignes, aujourd_hui):
    futures = []
    for iso, est, pub, surprise in lignes or []:
        if pub is not None:
            continue
        jour = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc).date().isoformat()
        if jour >= aujourd_hui:
            futures.append(jour)
    return min(futures) if futures else None


def fusionner():
    aujourd_hui = dt.date.today().isoformat()
    yahoo = lire_json(os.path.join(DOSSIER, "yahoo.json"), {})
    uk = lire_json(os.path.join(DOSSIER, "uk.json"), {})
    nord = lire_json(os.path.join(DOSSIER, "nordique.json"), {})
    entreprises, sources = {}, {"yahoo": 0, "investegate": 0, "nasdaq": 0}
    for e in univers():
        t = e["ticker"]
        lignes = yahoo.get(t)
        lignes = lignes if isinstance(lignes, list) else []
        exactes = []
        if t in uk:
            exactes = publications_uk(uk[t])
        elif t in nord:
            exactes = publications_nordiques(nord[t])
        pubs = list(exactes)
        for y in publications_yahoo(lignes, aujourd_hui):
            jy = dt.date.fromisoformat(y["publie_le"][:10])
            proche = next((p for p in pubs if abs((dt.date.fromisoformat(p["publie_le"][:10]) - jy).days) <= 3), None)
            if proche is not None:
                # La date et l'heure exactes priment ; le BPA vient de Yahoo.
                proche["bpa_estime"], proche["bpa_publie"] = y["bpa_estime"], y["bpa_publie"]
                proche["source"] += "+yahoo"
            else:
                pubs.append(y)
        pubs.sort(key=lambda p: p["publie_le"])
        for p in pubs:
            p["id"] = f"{t}|{p['publie_le'][:10]}"
            sources[p["source"].split("+")[0]] += 1
        if pubs or lignes:
            entreprises[t] = {"nom": e["nom"], "pays": e["pays"], "secteur": e.get("secteur"),
                              "indices": e["indices"], "prochaine": prochaine_yahoo(lignes, aujourd_hui),
                              "publications": pubs}
    doc = {"version": 1, "depuis": DEPUIS, "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "entreprises": entreprises}
    ecrire_json(SORTIE, doc)
    n = sum(len(f["publications"]) for f in entreprises.values())
    avec_bpa = sum(1 for f in entreprises.values() for p in f["publications"] if p.get("bpa_estime") is not None)
    print(f"resultats_stoxx.json : {n} publications, {len(entreprises)} entreprises (sur {len(univers())}), "
          f"{avec_bpa} avec le BPA estimé ; sources : {sources}.")


def collecter_recent():
    """Mise a jour quotidienne : seulement les annonces recentes, ajoutees aux
    collectes completes (stoxx/*.json)."""
    aujourd_hui = dt.date.today()
    debut = (aujourd_hui - dt.timedelta(days=RECENT)).isoformat()
    fin = (aujourd_hui + dt.timedelta(days=3)).isoformat()
    tous = {e["ticker"]: e for e in univers()}

    # Yahoo : entreprises dont une date de resultats (passee ou attendue) tombe
    # dans la fenetre ; leur calendrier recent remplace les memes dates.
    chemin = os.path.join(DOSSIER, "yahoo.json")
    doc = lire_json(chemin, {})
    todo = [t for t, l in doc.items() if t in tous and isinstance(l, list)
            and any(debut <= x[0][:10] <= fin for x in l)]
    try:
        import yfinance as yf
    except ImportError:
        yf = None
        print("Yahoo : yfinance absent (pip install yfinance), calendrier non relu.", file=sys.stderr)

    def lire_yahoo(t):
        for _ in range(3):
            try:
                df = yf.Ticker(t).get_earnings_dates(limit=12)
                if df is None or df.empty:
                    return t, []
                return t, [[ts.isoformat(), *[None if r[c] != r[c] else float(r[c])
                                              for c in ("EPS Estimate", "Reported EPS", "Surprise(%)")]]
                           for ts, r in df.iterrows()]
            except Exception:
                time.sleep(2)
        return t, None

    if yf is not None:
        with cf.ThreadPoolExecutor(5) as ex:
            for t, lignes in ex.map(lire_yahoo, todo):
                if lignes:
                    jours = {x[0][:10] for x in lignes}
                    doc[t] = sorted([x for x in doc[t] if x[0][:10] not in jours] + lignes, key=lambda x: x[0],
                                    reverse=True)
        ecrire_json(chemin, doc)
        print(f"stoxx/yahoo.json : {len(todo)} calendrier(s) relu(s).", file=sys.stderr)

    # Investegate : annonces de resultats recentes, meme recherche que la collecte.
    chemin = os.path.join(DOSSIER, "uk.json")
    uk = lire_json(chemin, {})
    cibles = [t for t, f in uk.items() if t in tous and f.get("recherche")]

    def lire_uk(t):
        tidm = re.sub(r"\.$", "", t[:-2].replace("-", "."))
        mot = uk[t]["recherche"]
        tot = []
        for page in range(1, 4):
            ls = _inv_lignes(_inv_page(mot, 2 if mot == tidm else 1, page, debut))
            tot += [x for x in ls if _epic(x["co"]) == tidm]
            if len(ls) < 20:
                break
        return t, tot

    nouvelles = 0
    with cf.ThreadPoolExecutor(4) as ex:
        for t, annonces in ex.map(lire_uk, cibles):
            connues = {x["url"] for x in uk[t]["annonces"]}
            ajout = [x for x in annonces if x["url"] not in connues]
            uk[t]["annonces"] += ajout
            nouvelles += len(ajout)
    ecrire_json(chemin, uk)
    print(f"stoxx/uk.json : {nouvelles} annonce(s) ajoutée(s).", file=sys.stderr)

    # Nasdaq Nordic : premiere page de chaque categorie de rapport.
    chemin = os.path.join(DOSSIER, "nordique.json")
    nord = lire_json(chemin, {})
    cibles = [t for t, f in nord.items() if t in tous and f.get("societe")]

    def lire_nord(t):
        out = []
        for cat in CATEGORIES_NORDIQUES:
            its = _nasdaq(company=nord[t]["societe"], cnscategory=cat) or []
            out += [{"t": i["releaseTime"], "titre": i["headline"], "categorie": cat, "langue": i["language"],
                     "url": i.get("messageUrl")} for i in its if i["releaseTime"] >= debut]
        return t, out

    nouvelles = 0
    with cf.ThreadPoolExecutor(4) as ex:
        for t, annonces in ex.map(lire_nord, cibles):
            connues = {x["url"] for x in nord[t]["annonces"]}
            ajout = [x for x in annonces if x["url"] not in connues]
            nord[t]["annonces"] += ajout
            nouvelles += len(ajout)
    ecrire_json(chemin, nord)
    print(f"stoxx/nordique.json : {nouvelles} annonce(s) ajoutée(s).", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yahoo", action="store_true")
    p.add_argument("--uk", action="store_true")
    p.add_argument("--nordique", action="store_true")
    p.add_argument("--fusion", action="store_true")
    p.add_argument("--recent", action="store_true", help="mise a jour quotidienne, puis fusion")
    p.add_argument("--complement", action="store_true", help="avec --uk : annonces de 2015 a 2018")
    a = p.parse_args()
    if a.recent:
        collecter_recent()
    if a.yahoo:
        collecter_yahoo()
    if a.uk:
        collecter_uk(a.complement)
    if a.nordique:
        collecter_nordique()
    if a.fusion or not (a.yahoo or a.uk or a.nordique):
        fusionner()


if __name__ == "__main__":
    main()
