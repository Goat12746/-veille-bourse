#!/usr/bin/env python3
"""Collecte de la veille reglementaire et financiere, SANS IA.

Recupere les textes recents des sources publiques, garde ceux qui
correspondent aux themes (mots-cles) ou aux entreprises du referentiel, et
ecrit sortie/candidats.json. L'analyse (sens, ampleur, pertinence) est faite
ensuite par Claude (voir CONSIGNES.md), puis fusionner.py publie
alertes.json pour l'application.

Sources :
  - AMF (info-financiere.gouv.fr) : communiques reglementes des societes ;
  - Assemblee nationale (open data) : amendements deposes ou votes, agenda ;
  - Google Actualites (RSS) : presse, par theme ;
  - Yahoo Finance : dates de resultats et de detachement du dividende
    (indicatives, a confirmer).

Usage : python collecte.py [--jours 3] [--sans-amendements] [--sans-calendrier]
Bibliotheque standard uniquement (pas de pip install).
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "sortie")
CACHE = os.path.join(ICI, ".cache")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
AN_OPENDATA = "https://data.assemblee-nationale.fr/static/openData/repository/17"
AMF_API = ("https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/"
           "datasets/flux-amf-new-prod/records")

# Sous-types AMF sans interet pour la veille (declarations recurrentes).
AMF_EXCLUS = {
    "Total du nombre de droits de vote et du capital",
    "Acquisition ou cession des actions de l'émetteur",
    "Descriptif des programmes de rachat",
    "Mise à disposition d'un document",
}
# Ordre du jour : examen ou vote des textes budgetaires, toujours signales
# dans l'agenda (les simples auditions ne le sont que si elles touchent un
# theme).
AGENDA_BUDGET = ["projet de loi de finances", "financement de la securite sociale",
                 "loi de finances rectificative", "loi de programmation"]
AGENDA_EXAMEN = ["examen de la premiere partie", "examen des articles", "examen du projet",
                 "suite de l'examen", "discussion du projet", "vote solennel", "explications de vote",
                 "commission mixte paritaire", "lecture definitive", "nouvelle lecture"]
# Points de l'ordre du jour susceptibles de peser sur un cours : ceux ou des
# votes ont lieu (examen en commission, discussion en seance, vote final).
AGENDA_DECISION = ["examen", "discussion", "vote", "commission mixte paritaire",
                   "lecture definitive", "nouvelle lecture"]
# Exclus : procedure sans decision (nomination, audition...), suites d'un
# debat deja signale, textes sans portee juridique (resolutions) et reunions
# de l'article 88 (tri des amendements juste avant la seance).
AGENDA_EXCLUS = ["nomination", "audition", "table ronde", "communication", "rapport d'information",
                 "echange de vues", "proposition de resolution", "article 88", "mission d'information",
                 "questions au gouvernement"]
TEXTE_MAX = 2500  # caracteres de texte transmis a l'analyse par candidat


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def normaliser(texte):
    """Minuscules, sans accents ni espaces multiples, apostrophes droites."""
    texte = texte.replace("’", "'").replace(" ", " ")
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texte)


def texte_brut(fragment_html):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment_html or ""))).strip()


def chaine(v):
    """Les champs vides de l'open data sont des objets {"@xsi:nil": true}."""
    return v if isinstance(v, str) else ""


def http_get(url, opener=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
        return r.read()


def telecharger_cache(url, nom):
    """Fichier volumineux (open data) : retelecharge seulement s'il a change."""
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, nom)
    entetes = {"User-Agent": UA}
    if os.path.exists(chemin):
        mtime = os.path.getmtime(chemin)
        entetes["If-Modified-Since"] = email.utils.formatdate(mtime, usegmt=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=entetes), timeout=600) as r:
            data = r.read()
        with open(chemin, "wb") as f:
            f.write(data)
    except urllib.error.HTTPError as e:
        if e.code != 304:
            raise
    return chemin


def extrait(texte, mots):
    """Premiere phrase du texte qui contient l'un des mots-cles."""
    for phrase in re.split(r"(?<=[.;!?])\s+", texte):
        n = normaliser(phrase)
        if any(m in n for m in mots):
            return phrase.strip()[:400]
    return texte[:300]


class Analyseur:
    """Themes, polarite et referentiel des entreprises."""

    def __init__(self):
        with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
            self.entreprises = json.load(f)["entreprises"]
        with open(os.path.join(ICI, "themes.json"), encoding="utf-8") as f:
            t = json.load(f)
        self.themes = t["themes"]
        for th in self.themes:
            # "a+b" : a ET b doivent figurer dans le texte.
            th["mots_norm"] = [[normaliser(p) for p in m.split("+")] for m in th["mots_cles"]]
        self.negatif = [normaliser(m) for m in t["polarite"]["negatif"]]
        self.positif = [normaliser(m) for m in t["polarite"]["positif"]]
        self.par_isin = {e["isin"]: e for e in self.entreprises}
        self.tous = [e["ticker"] for e in self.entreprises]
        # Noms d'entreprises cites explicitement dans un texte.
        self.noms = [(normaliser(e["nom"].split(" (")[0]), e["ticker"]) for e in self.entreprises]

    def themes_de(self, texte_norm):
        return [th for th in self.themes
                if any(all(p in texte_norm for p in m) for m in th["mots_norm"])]

    def entreprises_de(self, themes, texte_norm):
        tickers = []
        for th in themes:
            tickers += self.tous if th["entreprises"] == ["*"] else th["entreprises"]
        for nom, ticker in self.noms:
            if re.search(r"\b" + re.escape(nom) + r"\b", texte_norm):
                tickers.append(ticker)
        return sorted(set(tickers))

    def sens(self, texte_norm):
        n = sum(texte_norm.count(m) for m in self.negatif)
        p = sum(texte_norm.count(m) for m in self.positif)
        if n > p:
            return "negatif"
        if p > n:
            return "positif"
        return "incertain"

    def candidat(self, cid, source, date, titre, texte, url, meta, themes=None, tickers=None):
        norm = normaliser(titre + " " + texte)
        themes = themes if themes is not None else self.themes_de(norm)
        mots = [m[0] for th in themes for m in th["mots_norm"]]
        return {
            "id": cid,
            "source": source,
            "date": date,
            "titre": titre,
            "texte": texte[:TEXTE_MAX],
            "extrait": extrait(texte, mots) if mots else "",
            "url": url,
            "themes": [th["id"] for th in themes],
            "entreprises_potentielles": tickers if tickers is not None
            else self.entreprises_de(themes, norm),
            "sens_indicatif": self.sens(norm),
            "meta": meta,
        }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def source_amf(a, depuis):
    """Communiques reglementes des entreprises du referentiel."""
    isins = ", ".join(f"'{i}'" for i in a.par_isin)
    params = {
        "where": f"informationdeposee_inf_dat_emt >= '{depuis}' AND identificationsociete_iso_cd_isi IN ({isins})",
        "order_by": "informationdeposee_inf_dat_emt desc",
        "select": ("uin_idt_uin,informationdeposee_inf_dat_emt,identificationsociete_iso_cd_isi,"
                   "informationdeposee_inf_tit_inf,type_d_information,sous_type_d_information,"
                   "url_de_recuperation,informationdeposee_inf_lng_inf"),
        "limit": 100,
    }
    candidats, offset = [], 0
    while True:
        params["offset"] = offset
        d = json.loads(http_get(AMF_API + "?" + urllib.parse.urlencode(params)))
        res = d.get("results", [])
        for r in res:
            sous_type = r.get("sous_type_d_information") or ""
            if sous_type in AMF_EXCLUS:
                continue
            # Le meme communique est souvent depose en francais et en anglais.
            if (r.get("informationdeposee_inf_lng_inf") or "").lower().startswith("angl"):
                continue
            e = a.par_isin.get(r["identificationsociete_iso_cd_isi"])
            if e is None:
                continue
            titre = r.get("informationdeposee_inf_tit_inf") or sous_type
            candidats.append(a.candidat(
                "amf-" + r["uin_idt_uin"], "amf", r["informationdeposee_inf_dat_emt"][:10],
                f"{e['nom']} : {titre}", f"{sous_type}. {titre}", r.get("url_de_recuperation") or "",
                {"type": r.get("type_d_information"), "sous_type": sous_type, "heure": r["informationdeposee_inf_dat_emt"]},
                themes=[], tickers=[e["ticker"]]))
        offset += len(res)
        if not res or offset >= d.get("total_count", 0):
            break
    return candidats


def _url_amendement(a):
    """Page de l'amendement sur le site de l'Assemblee. Formats verifies :
    seance 3190/AN/143, premiere partie du budget 1906A/AN/1176 (numero
    "I-1176"), commission 2247/CION_FIN/CF2305."""
    base = "https://www.assemblee-nationale.fr/dyn/17/amendements"
    # PRJLANR5L17B1906 (texte depose) ou ...BTC3190 (texte de la commission).
    texte = re.search(r"B(?:TC)?(\d+)$", chaine(a.get("texteLegislatifRef")))
    ident = a.get("identification") or {}
    organe = chaine(ident.get("prefixeOrganeExamen")) or "AN"
    numero_long = chaine(ident.get("numeroLong"))
    if not texte:
        return base
    if organe != "AN":
        return f"{base}/{texte.group(1)}/{organe}/{numero_long}"
    if numero_long.startswith("II-"):
        return base  # seconde partie du budget : format d'adresse non verifie
    suffixe = "A" if numero_long.startswith("I-") else ""
    return f"{base}/{texte.group(1)}{suffixe}/AN/{chaine(ident.get('numeroOrdreDepot'))}"


def source_amendements(a, depuis):
    """Amendements deposes, ou dont le sort a ete decide, depuis `depuis`."""
    chemin = telecharger_cache(f"{AN_OPENDATA}/loi/amendements_div_legis/Amendements.json.zip",
                               "amendements_an.zip")
    candidats = []
    with zipfile.ZipFile(chemin) as z:
        for info in z.infolist():
            if not info.filename.endswith(".json"):
                continue
            am = json.loads(z.read(info))["amendement"]
            cv = am.get("cycleDeVie") or {}
            depot = chaine(cv.get("dateDepot"))
            date_sort = chaine(cv.get("dateSort"))[:10]
            if depot < depuis and date_sort < depuis:
                continue
            corps = (am.get("corps") or {}).get("contenuAuteur") or {}
            dispositif = texte_brut(chaine(corps.get("dispositif")))
            expose = texte_brut(chaine(corps.get("exposeSommaire")))
            texte = f"Dispositif : {dispositif} Exposé sommaire : {expose}"
            norm = normaliser(texte)
            themes = a.themes_de(norm)
            if not themes:
                continue
            sig = am.get("signataires") or {}
            auteur = sig.get("auteur") or {}
            etat = ((cv.get("etatDesTraitements") or {}).get("etat") or {})
            ident = am.get("identification") or {}
            ref = chaine(am.get("texteLegislatifRef"))
            candidats.append(a.candidat(
                "an-" + am["uid"], "assemblee", date_sort if date_sort >= depuis else depot,
                f"Amendement {chaine(ident.get('numeroLong'))} ({texte_brut(chaine(sig.get('libelle')))[:80]})",
                texte, _url_amendement(am),
                {
                    "texte_ref": ref,
                    # "AN" : seance publique ; sinon commission (CION_FIN...).
                    "organe": chaine(ident.get("prefixeOrganeExamen")),
                    "nature_texte": "projet de loi" if "PRJL" in ref else "proposition de loi" if "PION" in ref else "",
                    "type_auteur": chaine(auteur.get("typeAuteur")),
                    "rapporteur": bool(chaine(auteur.get("auteurRapporteurOrganeRef"))),
                    "article": chaine(((am.get("pointeurFragmentTexte") or {}).get("division") or {}).get("articleDesignationCourte")),
                    "date_depot": depot,
                    "etat": chaine(etat.get("libelle")),
                    "sort": chaine(cv.get("sort")),
                    "date_sort": date_sort,
                },
                themes=themes))
    return candidats


def _items(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return v.get("item") if isinstance(v.get("item"), list) else [v.get("item")] if v.get("item") else []
    return [v]


def _points_odj(odj):
    """Points de l'ordre du jour d'une reunion, sans doublons : la
    convocation et le resume repetent souvent les memes lignes."""
    points = (odj.get("pointsODJ") or {}).get("pointODJ") if isinstance(odj.get("pointsODJ"), dict) else None
    if isinstance(points, dict):
        points = [points]
    brutes = [chaine(x) for x in _items(odj.get("convocationODJ")) + _items(odj.get("resumeODJ"))]
    brutes += [chaine(p.get("objet")) for p in points or [] if isinstance(p, dict)]
    gardees = []
    for ligne in brutes:
        ligne = re.sub(r"^[\s\-–••*]+", "", ligne).strip()
        ligne = ligne.rstrip(" ;,")
        if not ligne:
            continue
        ligne = ligne[0].upper() + ligne[1:]
        n = normaliser(ligne)
        # Doublon, ou ligne contenue dans une autre (resume plus court).
        if any(n in normaliser(g) for g in gardees):
            continue
        gardees = [g for g in gardees if normaliser(g) not in n] + [ligne]
    return gardees


def _noms_organes():
    """Identifiant d'organe de l'Assemblee (PO420120...) -> nom lisible."""
    chemin = telecharger_cache(
        f"{AN_OPENDATA}/amo/deputes_actifs_mandats_actifs_organes/"
        "AMO10_deputes_actifs_mandats_actifs_organes.json.zip", "organes_an.zip")
    noms = {}
    with zipfile.ZipFile(chemin) as z:
        for n in z.namelist():
            if "/organe/" not in n:
                continue
            o = json.loads(z.read(n))["organe"]
            libelle = chaine(o.get("libelle"))
            noms[o["uid"]] = "Séance publique" if o.get("codeType") == "ASSEMBLEE" else (
                libelle if len(libelle) <= 90 else chaine(o.get("libelleAbrege")) or libelle)
    return noms


def source_agenda(a, jours_avant=21):
    """Reunions a venir de l'Assemblee ou un texte lie a un theme, ou le
    budget, est examine ou vote (pas les nominations, auditions, suites de
    debat...). Seuls les points concernes sont repris dans le
    titre ; la source indique la commission (ou la seance publique) et le
    lien ouvre l'agenda du jour, ou la reunion figure a son heure."""
    chemin = telecharger_cache(f"{AN_OPENDATA}/vp/reunions/Agenda.json.zip", "agenda_an.zip")
    try:
        organes = _noms_organes()
    except Exception:
        organes = {}  # noms indisponibles : source generique
    aujourd_hui = dt.date.today().isoformat()
    limite = (dt.date.today() + dt.timedelta(days=jours_avant)).isoformat()
    evenements = []
    with zipfile.ZipFile(chemin) as z:
        for info in z.infolist():
            if not info.filename.endswith(".json"):
                continue
            r = json.loads(z.read(info))["reunion"]
            debut = chaine(r.get("timeStampDebut"))
            if not (aujourd_hui <= debut[:10] <= limite):
                continue
            themes, budget, retenus = [], False, []
            for point in _points_odj(r.get("ODJ") or {}):
                norm = normaliser(point)
                if ("suite de l" in norm or any(m in norm for m in AGENDA_EXCLUS)
                        or not any(m in norm for m in AGENDA_DECISION)):
                    continue
                th = a.themes_de(norm)
                bu = (any(m in norm for m in AGENDA_BUDGET)
                      and any(m in norm for m in AGENDA_EXAMEN))
                if th or bu:
                    retenus.append(point)
                    themes += [t for t in th if t not in themes]
                    budget = budget or bu
            if not retenus:
                continue
            titre = " • ".join(retenus)
            evenements.append({
                "date": debut[:10],
                "heure": debut[11:16],
                "type": "budget" if budget else "examen_texte",
                "titre": titre if len(titre) <= 400 else titre[:397] + "…",
                "tickers": a.entreprises_de(themes, normaliser(titre)) if themes else [],
                "themes": [th["id"] for th in themes],
                "source": "Assemblée nationale" + (
                    f" — {organes[r.get('organeReuniRef')]}" if r.get("organeReuniRef") in organes else ""),
                # Page de l'agenda du jour, ancre sur la reunion.
                "url": f"https://www2.assemblee-nationale.fr/agendas/les-agendas/{debut[:10]}#odj-OMC_{r['uid']}",
                "fiable": True,
            })
    evenements.sort(key=lambda e: (e["date"], e["heure"]))
    return evenements


def source_presse(a, jours):
    """Google Actualites, une requete par theme. Erreur si aucune requete
    n'aboutit (acces reseau bloque), pour ne pas confondre avec "rien de
    nouveau"."""
    candidats = {}
    echecs = []
    for th in a.themes:
        q = f"{th['presse']} when:{max(jours, 1)}d"
        url = ("https://news.google.com/rss/search?" +
               urllib.parse.urlencode({"q": q, "hl": "fr", "gl": "FR", "ceid": "FR:fr"}))
        try:
            racine = ET.fromstring(http_get(url))
        except Exception as e:  # une requete en echec n'arrete pas la collecte
            print(f"  presse {th['id']} : {e}", file=sys.stderr)
            echecs.append(str(e))
            continue
        for item in racine.iter("item"):
            titre = item.findtext("title") or ""
            lien = item.findtext("link") or ""
            source = item.findtext("source") or ""
            try:
                date = email.utils.parsedate_to_datetime(item.findtext("pubDate")).date().isoformat()
            except Exception:
                date = dt.date.today().isoformat()
            cid = "presse-" + hashlib.sha1(lien.encode()).hexdigest()[:16]
            if cid in candidats:
                candidats[cid]["themes"] = sorted(set(candidats[cid]["themes"] + [th["id"]]))
                continue
            norm = normaliser(titre)
            themes = [th] + [t for t in a.themes_de(norm) if t is not th]
            candidats[cid] = a.candidat(cid, "presse", date, titre, titre, lien,
                                        {"media": source}, themes=themes)
    if echecs and len(echecs) == len(a.themes):
        raise RuntimeError(f"toutes les requetes ont echoue : {echecs[0]}")
    return list(candidats.values())


def source_calendrier_yahoo(a):
    """Prochaines dates de resultats et de detachement du dividende (Yahoo,
    peu fiable pour l'Europe : a confirmer)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        http_get("https://fc.yahoo.com", opener)
    except Exception:
        pass  # la reponse est une erreur, mais le cookie est pose
    crumb = _yahoo_get("https://query1.finance.yahoo.com/v1/test/getcrumb", opener).decode()
    aujourd_hui = dt.date.today()
    echecs = []

    def un(e):
        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{e['ticker']}"
               f"?modules=calendarEvents&crumb={urllib.parse.quote(crumb)}")
        try:
            cal = json.loads(_yahoo_get(url, opener))["quoteSummary"]["result"][0]["calendarEvents"]
        except Exception as err:
            echecs.append(str(err))
            return []
        evts = []
        for d in (cal.get("earnings") or {}).get("earningsDate") or []:
            jour = dt.datetime.fromtimestamp(d["raw"], dt.timezone.utc).date()
            if jour >= aujourd_hui:
                evts.append({"date": jour.isoformat(), "heure": "", "type": "resultats",
                             "titre": f"{e['nom']} : publication de résultats (date Yahoo, à confirmer)",
                             "tickers": [e["ticker"]], "themes": [], "source": "Yahoo Finance",
                             "url": f"https://finance.yahoo.com/quote/{e['ticker']}", "fiable": False})
                break
        ex = (cal.get("exDividendDate") or {}).get("raw")
        if ex:
            jour = dt.datetime.fromtimestamp(ex, dt.timezone.utc).date()
            if jour >= aujourd_hui:
                evts.append({"date": jour.isoformat(), "heure": "", "type": "dividende",
                             "titre": f"{e['nom']} : détachement du dividende",
                             "tickers": [e["ticker"]], "themes": [], "source": "Yahoo Finance",
                             "url": f"https://finance.yahoo.com/quote/{e['ticker']}", "fiable": False})
        return evts

    # Peu de requetes en parallele : Yahoo limite vite les serveurs cloud.
    with cf.ThreadPoolExecutor(2) as ex:
        evts = sorted((ev for evs in ex.map(un, a.entreprises) for ev in evs), key=lambda e: e["date"])
    if len(echecs) == len(a.entreprises):
        raise RuntimeError(f"aucune action lue sur Yahoo : {echecs[0]}")
    return evts


def _yahoo_get(url, opener, essais=4):
    """Requete Yahoo avec nouvelles tentatives espacees quand Yahoo repond
    429 (trop de requetes) ou 5xx."""
    for i in range(essais):
        try:
            return http_get(url, opener)
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or i == essais - 1:
                raise
            time.sleep(5 * (i + 1))


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jours", type=int, default=3, help="profondeur de la collecte (jours)")
    p.add_argument("--sans-amendements", action="store_true", help="ne telecharge pas les amendements (300 Mo)")
    p.add_argument("--sans-calendrier", action="store_true", help="pas de dates Yahoo")
    args = p.parse_args()

    a = Analyseur()
    depuis = (dt.date.today() - dt.timedelta(days=args.jours)).isoformat()
    etat_path = os.path.join(ICI, "etat.json")
    deja = {}
    if os.path.exists(etat_path):
        with open(etat_path, encoding="utf-8") as f:
            deja = json.load(f).get("ids_traites", {})

    rapport, candidats, agenda, calendrier = {}, [], [], []
    etapes = [("amf", lambda: source_amf(a, depuis)),
              ("presse", lambda: source_presse(a, args.jours))]
    if not args.sans_amendements:
        etapes.append(("assemblee", lambda: source_amendements(a, depuis)))
    for nom, f in etapes:
        print(f"Collecte {nom}…", file=sys.stderr)
        try:
            res = f()
            nouveaux = [c for c in res if c["id"] not in deja]
            candidats += nouveaux
            rapport[nom] = {"ok": True, "trouves": len(res), "nouveaux": len(nouveaux)}
        except Exception as e:
            rapport[nom] = {"ok": False, "erreur": str(e)[:300]}
    for nom, f in [("agenda", lambda: source_agenda(a)),
                   ("calendrier", None if args.sans_calendrier else lambda: source_calendrier_yahoo(a))]:
        if f is None:
            continue
        print(f"Collecte {nom}…", file=sys.stderr)
        try:
            res = f()
            (agenda if nom == "agenda" else calendrier).extend(res)
            rapport[nom] = {"ok": True, "trouves": len(res)}
        except Exception as e:
            rapport[nom] = {"ok": False, "erreur": str(e)[:300]}

    os.makedirs(SORTIE, exist_ok=True)
    sortie = {
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "depuis": depuis,
        "sources": rapport,
        "candidats": sorted(candidats, key=lambda c: c["date"], reverse=True),
        "agenda": agenda,
        "calendrier": calendrier,
    }
    with open(os.path.join(SORTIE, "candidats.json"), "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    print(json.dumps({"candidats": len(candidats), "agenda": len(agenda),
                      "calendrier": len(calendrier), "sources": rapport}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
