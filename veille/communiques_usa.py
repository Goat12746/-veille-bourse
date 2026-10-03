#!/usr/bin/env python3
"""Publications de resultats americaines du jour, jugees par Claude comme
les communiques AMF (onglet Actualites > Alertes > Etats-Unis).

  - depots de resultats recents des entreprises suivies (8-K, item 2.02) :
    flux des derniers depots de la SEC (quelques jours) et publications des
    derniers jours de resultats_usa.json ;
  - communique de presse joint (piece EX-99.1) : debut (chiffres cles) et
    phrases sur les perspectives, avec le consensus des analystes de la
    veille (consensus_usa.json) et un premier reperage des perspectives par
    mots-cles (sans IA, a confirmer par Claude) ;
  - avis de Claude (meme format que pour la France, voir CONSIGNES.md) dans
    resultats_usa_classes.json ;
  - fiche du jour de publication (jour_j.py) : consensus, chiffres publies,
    ecarts, probabilite de hausse ajustee, dans resultats_usa_jour.json, lu
    par l'application.

Usage :
  python communiques_usa.py --a-classer          -> sortie/resultats_usa_a_classer.json
  python communiques_usa.py --integrer FICHIER   ajoute des avis de Claude
  python communiques_usa.py --jour               recalcule resultats_usa_jour.json
  python communiques_usa.py --approfondir NUMERO  extrait long pour l'analyse approfondie
                                                 -> sortie/analyse_approfondie.json
Bibliotheque standard uniquement.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error

import consensus as consensus_mod
import edgar
import jour_j
from communiques import valider_avis

ICI = os.path.dirname(os.path.abspath(__file__))
CLASSES = os.path.join(ICI, "resultats_usa_classes.json")
JOUR = os.path.join(ICI, "resultats_usa_jour.json")
A_CLASSER = os.path.join(ICI, "sortie", "resultats_usa_a_classer.json")
ETUDE_USA = os.path.join(ICI, "communiques_usa.json")
JOURS = 14  # publications gardees dans resultats_usa_jour.json
JOURS_A_CLASSER = 5  # publications proposees a Claude (rattrapage d'une veille manquee)
FLUX_PAGES = 8  # pages de 100 depots 8-K dans le flux de la SEC

# Reperage des perspectives par mots-cles (signal provisoire, a confirmer).
_OBJET = r"(?:its |our |the |full[- ]year |fiscal |annual |20\d\d |year |quarter |\w+ ){0,5}(?:guidance|outlook|forecast)"
_PERSPECTIVES = [
    ("abaissees", rf"\b(?:lower(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|cut(?:s|ting)?|trim(?:s|med)?|narrow(?:s|ed)? (?:to|toward) the low end of) {_OBJET}"),
    ("abaissees", r"\b(?:guidance|outlook) (?:was |is )?(?:lowered|reduced|cut)\b"),
    ("relevees", rf"\b(?:rais(?:e|es|ed|ing)|increas(?:e|es|ed|ing)|lift(?:s|ed)?|boost(?:s|ed)?|upgrad(?:e|es|ed)) {_OBJET}"),
    ("relevees", r"\b(?:guidance|outlook) (?:was |is )?(?:raised|increased)\b"),
    ("confirmees", rf"\b(?:reaffirm(?:s|ed|ing)?|reiterat(?:e|es|ed|ing)|maintain(?:s|ed|ing)?|confirm(?:s|ed|ing)?) {_OBJET}"),
]
_OUTLOOK = ["outlook", "guidance", "expects", "expect ", "forecast", "full-year", "full year", "fiscal 20",
            "reaffirm", "raises", "raised", "lowers", "lowered"]


def charger(chemin, defaut):
    if not os.path.exists(chemin):
        return defaut
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def ecrire_classes(doc):
    with open(CLASSES, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "classes": {\n')
        f.write(",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                           for k, v in sorted(doc["classes"].items())))
        f.write("\n }\n}\n")


# ---------------------------------------------------------------------------
# Depots recents et communique de presse
# ---------------------------------------------------------------------------

def entreprises():
    """Entreprises suivies : {cik: (ticker, nom)} (une ligne par societe)."""
    doc = charger(edgar.SORTIE, {"entreprises": {}})
    return {v["cik"]: (t, v["nom"]) for t, v in doc["entreprises"].items()}


def flux_sec(ciks, jours):
    """Depots 8-K item 2.02 des entreprises suivies dans le flux des derniers
    depots de la SEC : {numero: {cik, publie_le}}."""
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=jours)
    res = {}
    for page in range(FLUX_PAGES):
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
               f"&start={page * 100}&count=100&output=atom")
        entrees = re.findall(r"<entry>(.*?)</entry>", edgar.sec_get(url).decode("latin-1"), re.S)
        if not entrees:
            break
        plus_ancien = None
        for e in entrees:
            quand = re.search(r"<updated>(.*?)</updated>", e)
            if not quand:
                continue
            t = dt.datetime.fromisoformat(quand.group(1))
            plus_ancien = t
            titre = re.search(r"<title>8-K - .*? \((\d{10})\)", e)
            numero = re.search(r"accession-number=([\d-]+)", e)
            if titre and numero and "Item 2.02" in e and int(titre.group(1)) in ciks:
                res[numero.group(1)] = {"cik": int(titre.group(1)), "publie_le": quand.group(1)}
        if plus_ancien is None or plus_ancien < limite:
            break
    return res


def texte_html(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", " | ", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\xa0", " ")
    lignes = [re.sub(r"[ \t|]+", " ", x).strip(" |") for x in t.split("\n")]
    return "\n".join(x for x in lignes if x)


def communique(cik, numero):
    """Texte du communique de presse joint au depot (piece EX-99.x), a
    defaut le document principal."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{numero.replace('-', '')}/"
    page = edgar.sec_get(f"{base}{numero}-index.htm").decode("latin-1")
    docs = []
    for ligne in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cases = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", ligne, re.S)]
        lien = re.search(r'href="([^"]+)"', ligne)
        if len(cases) >= 4 and lien:
            docs.append((cases[3], lien.group(1)))
    choix = (next((l for t, l in docs if t.upper().startswith("EX-99")), None)
             or next((l for t, l in docs if t.upper() == "8-K"), None))
    if not choix:
        return "", None
    url = "https://www.sec.gov" + choix if choix.startswith("/") else base + choix
    url = url.replace("/ix?doc=", "")
    return texte_html(edgar.sec_get(url).decode("utf-8", "replace")), url


def extrait(texte, n=3000, persp=1200):
    """Debut du communique (chiffres cles) et phrases sur les perspectives."""
    t = texte or ""
    debut = t[:n]
    phrases, total = [], 0
    for p in re.split(r"(?<=[.;!?])\s+|\n", t[n:]):
        if len(p) > 30 and any(m in p.lower() for m in _OUTLOOK) and p not in phrases:
            phrases.append(p.strip())
            total += len(p)
            if total >= persp:
                break
    return debut + ((" [...] " + " ".join(phrases)[:persp + 200]) if phrases else "")


def perspectives_mots(texte):
    """Premier reperage des perspectives (sans IA) : (valeur, phrase) ou
    (None, None). Abaissees l'emporte sur relevees, qui l'emporte sur
    confirmees."""
    t = " ".join((texte or "").split())
    for valeur in ("abaissees", "relevees", "confirmees"):
        for v, motif in _PERSPECTIVES:
            if v != valeur:
                continue
            m = next((x for x in re.finditer(motif, t, re.I)
                      # "is not updating or confirming", "does not change its guidance"
                      if not re.search(r"\b(?:not|no longer|neither|nor)\b[^.]{0,40}$", t[max(0, x.start() - 60):x.start()],
                                       re.I)), None)
            if m:
                debut = max(0, t.rfind(".", 0, m.start()) + 1)
                fin = t.find(".", m.end())
                return valeur, t[debut:fin + 1 if fin > 0 else m.end() + 150].strip()[:300]
    return None, None


# ---------------------------------------------------------------------------
# A classer, integrer, fiche du jour
# ---------------------------------------------------------------------------

def recentes(jours):
    """Publications recentes : flux de la SEC et resultats_usa.json.
    {numero: {cik, ticker, nom, publie_le, url}}."""
    suivies = entreprises()
    res = {}
    limite = (dt.date.today() - dt.timedelta(days=jours)).isoformat()
    for t, v in charger(edgar.SORTIE, {"entreprises": {}})["entreprises"].items():
        for p in v["publications"]:
            if p["publie_le"][:10] >= limite:
                res[p["id"]] = {"cik": v["cik"], "ticker": t, "nom": v["nom"], "publie_le": p["publie_le"],
                                "url": p.get("url")}
    try:
        for numero, d in flux_sec(set(suivies), jours).items():
            if numero not in res:
                t, nom = suivies[d["cik"]]
                res[numero] = {"cik": d["cik"], "ticker": t, "nom": nom, "publie_le": d["publie_le"],
                               "url": (f"https://www.sec.gov/Archives/edgar/data/{d['cik']}/"
                                       f"{numero.replace('-', '')}/{numero}-index.htm")}
    except Exception as e:  # le flux injoignable n'empeche pas le reste
        print(f"Flux des derniers depots de la SEC indisponible : {e}", file=sys.stderr)
    return res


def a_classer():
    classes = charger(CLASSES, {"classes": {}})["classes"]
    estimations = consensus_mod.charger(consensus_mod.SORTIE_USA).get("entreprises", {})
    liste = []
    for numero, p in sorted(recentes(JOURS_A_CLASSER).items(), key=lambda kv: kv[1]["publie_le"]):
        if numero in classes:
            continue
        try:
            texte, url = communique(p["cik"], numero)
        except Exception as e:
            print(f"{p['ticker']} {numero} : communique illisible ({e})", file=sys.stderr)
            texte, url = "", None
        persp, phrase = perspectives_mots(texte)
        liste.append({"id": numero, "ticker": p["ticker"], "nom": p["nom"], "publie_le": p["publie_le"],
                      "url": url or p.get("url"), "extrait": extrait(texte),
                      "perspectives_mots": persp, "phrase_perspectives": phrase,
                      "consensus": consensus_mod.avant(estimations.get(p["ticker"]), p["publie_le"][:10])})
    os.makedirs(os.path.dirname(A_CLASSER), exist_ok=True)
    with open(A_CLASSER, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    print(f"{os.path.relpath(A_CLASSER, ICI)} : {len(liste)} publication(s) de résultats américaine(s) à juger.")
    return liste


def valider_chiffres(cid, a):
    e = []
    ch = a.get("chiffres")
    if ch is None:
        return e
    if not isinstance(ch, dict):
        return [f"{cid} : chiffres doit etre un objet"]
    for k, v in ch.items():
        if k in ("devise",):
            if not isinstance(v, str):
                e.append(f"{cid} : chiffres.devise doit etre un texte (USD, EUR...)")
        elif k == "bpa_ajuste":
            if not isinstance(v, bool):
                e.append(f"{cid} : chiffres.bpa_ajuste doit etre true ou false")
        elif k not in ("ca", "ca_var_pct", "resultat", "resultat_var_pct", "bpa", "bpa_var_pct"):
            e.append(f"{cid} : chiffres.{k} inconnu")
        elif v is not None and not isinstance(v, (int, float)):
            e.append(f"{cid} : chiffres.{k} doit etre un nombre ou null")
    return e


def valider_objectifs(cid, a):
    o = a.get("objectifs")
    if o is None:
        return []
    if not isinstance(o, dict):
        return [f"{cid} : objectifs doit etre un objet"]
    e = []
    if o.get("vs_consensus") not in ("superieurs", "conformes", "inferieurs", None):
        e.append(f"{cid} : objectifs.vs_consensus {o.get('vs_consensus')!r}, attendu superieurs, conformes, "
                 "inferieurs ou null")
    for k in ("ca", "bpa", "consensus_ca", "consensus_bpa"):
        v = o.get(k)
        if v is not None and not (isinstance(v, (int, float)) or (isinstance(v, list) and len(v) == 2 and all(
                isinstance(x, (int, float)) for x in v))):
            e.append(f"{cid} : objectifs.{k} doit etre un nombre ou [min, max]")
    return e


IMPACTS = ("positif", "negatif", "incertain")
AMPLEURS = ("faible", "moyenne", "forte")
EFFETS = ("+", "-", "=")


def valider_analyse(cid, a):
    """Analyse de Claude : resume, points cles (effet +, - ou =), points a
    surveiller, impact attendu."""
    e = []
    if a.get("resume") is not None and not isinstance(a.get("resume"), str):
        e.append(f"{cid} : resume doit etre un texte")
    for p in a.get("points_cles") or []:
        if not isinstance(p, dict) or not isinstance(p.get("texte"), str) or p.get("effet") not in EFFETS:
            e.append(f"{cid} : points_cles attend des objets {{texte, effet: +, - ou =}}")
            break
    if not all(isinstance(x, str) for x in a.get("a_surveiller") or []):
        e.append(f"{cid} : a_surveiller attend une liste de textes")
    imp = a.get("impact")
    if imp is not None and (not isinstance(imp, dict) or imp.get("sens") not in IMPACTS
                            or imp.get("ampleur") not in AMPLEURS + (None,)):
        e.append(f"{cid} : impact attend {{sens: {'/'.join(IMPACTS)}, ampleur: {'/'.join(AMPLEURS)}}}")
    return e


def integrer(chemin):
    nouveaux = charger(chemin, None)
    if nouveaux is None:
        sys.exit(f"{chemin} introuvable")
    if isinstance(nouveaux, list):
        nouveaux = {a["id"]: a for a in nouveaux}
    nouveaux = {k: {c: v for c, v in a.items() if c != "id"} for k, a in nouveaux.items()}
    erreurs = [x for k, a in nouveaux.items()
               for x in valider_avis(k, a) + valider_chiffres(k, a) + valider_objectifs(k, a)
               + valider_analyse(k, a)]
    if erreurs:
        sys.exit("Avis invalides :\n - " + "\n - ".join(erreurs))
    doc = charger(CLASSES, {"version": 1, "classes": {}})
    a_juger = {x["id"]: x for x in charger(A_CLASSER, [])}
    jour = dt.date.today().isoformat()
    for k, a in nouveaux.items():
        # Meta de la publication gardee avec l'avis (le depot peut ne pas
        # encore figurer dans resultats_usa.json).
        meta = {c: a_juger[k][c] for c in ("ticker", "nom", "publie_le", "url", "perspectives_mots")
                if k in a_juger and a_juger[k].get(c) is not None}
        # Complete l'avis existant (analyse approfondie ajoutee apres coup).
        doc["classes"][k] = dict(doc["classes"].get(k, {}), **meta, **a, le=a.get("le") or jour)
    ecrire_classes(doc)
    print(f"{os.path.relpath(CLASSES, ICI)} : {len(nouveaux)} avis ajouté(s), {len(doc['classes'])} au total.")
    ecrire_jour()


def ecrire_jour():
    """Fiche du jour de publication des publications des derniers jours
    (jugees ou non), lue par l'application."""
    classes = charger(CLASSES, {"classes": {}})["classes"]
    estimations = consensus_mod.charger(consensus_mod.SORTIE_USA).get("entreprises", {})
    etude = charger(ETUDE_USA, {})
    t_usa = jour_j.tables(etude.get("resultats"))
    p_base = {e["ticker"]: e.get("p_hausse_passee") for e in etude.get("entreprises", [])}
    perf = {e["ticker"]: (e.get("prochaine") or e.get("cours_recent") or {}).get("perf_12m_pct")
            for e in etude.get("entreprises", [])}
    pubs = {}
    for numero, p in recentes(JOURS).items():
        pubs[numero] = {"id": numero, "ticker": p["ticker"], "nom": p["nom"], "publie_le": p["publie_le"],
                        "url": p.get("url")}
    limite = (dt.date.today() - dt.timedelta(days=JOURS)).isoformat()
    for numero, a in classes.items():
        if numero not in pubs and a.get("ticker") and (a.get("publie_le") or "")[:10] >= limite:
            pubs[numero] = {"id": numero, **{c: a.get(c) for c in ("ticker", "nom", "publie_le", "url")}}
    # Reaction du cours une fois cotee (calcul d'etude_usa.py).
    reactions = {x["id"]: x for x in etude.get("recentes", [])}
    fiches = []
    for numero, pub in pubs.items():
        avis = classes.get(numero)
        fe = estimations.get(pub["ticker"]) or {}
        f = jour_j.fiche(pub, avis, consensus_mod.avant(fe, pub["publie_le"][:10]),
                         p_base.get(pub["ticker"]), t_usa, fe.get("surprises"), perf.get(pub["ticker"]))
        if avis and avis.get("perspectives_mots"):
            f["perspectives_mots"] = avis["perspectives_mots"]
        rx = reactions.get(numero) or {}
        f.update({k: rx[k] for k in ("jour", "rendement_pct", "indice_pct", "ecart_pct", "z") if k in rx})
        fiches.append(f)
    fiches.sort(key=lambda x: x["publie_le"], reverse=True)
    with open(JOUR, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "publications": fiches}, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"{os.path.relpath(JOUR, ICI)} : {len(fiches)} publication(s) des {JOURS} derniers jours, "
          f"{sum(1 for x in fiches if x.get('juge'))} jugée(s).")


def approfondir(numero):
    """Analyse approfondie demandee depuis l'application : extrait long du
    communique, avis deja donne et consensus de la veille."""
    classes = charger(CLASSES, {"classes": {}})["classes"]
    avis = classes.get(numero) or {}
    pub = recentes(30).get(numero) or {c: avis.get(c) for c in ("ticker", "nom", "publie_le", "url")}
    if not pub.get("ticker"):
        sys.exit(f"{numero} : publication inconnue (ni recente, ni jugee)")
    cik = pub.get("cik") or next((c for c, (t, _) in entreprises().items() if t == pub["ticker"]), None)
    texte, url = communique(cik, numero)
    estimations = consensus_mod.charger(consensus_mod.SORTIE_USA).get("entreprises", {})
    demande = {"marche": "usa", "id": numero, "ticker": pub["ticker"], "nom": pub["nom"],
               "publie_le": pub["publie_le"], "url": url or pub.get("url"), "avis": avis,
               "extrait": extrait(texte, 7000, 2500),
               "consensus": consensus_mod.avant(estimations.get(pub["ticker"]), pub["publie_le"][:10])}
    os.makedirs(os.path.dirname(A_CLASSER), exist_ok=True)
    chemin = os.path.join(ICI, "sortie", "analyse_approfondie.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(demande, f, ensure_ascii=False, indent=1)
    print(f"sortie/analyse_approfondie.json : {pub['nom']} ({numero}), extrait de {len(demande['extrait'])} caractères.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--a-classer", action="store_true")
    g.add_argument("--integrer", metavar="FICHIER")
    g.add_argument("--jour", action="store_true")
    g.add_argument("--approfondir", metavar="NUMERO")
    args = p.parse_args()
    if args.a_classer:
        a_classer()
    elif args.integrer:
        integrer(args.integrer)
    elif args.approfondir:
        approfondir(args.approfondir)
    else:
        ecrire_jour()


if __name__ == "__main__":
    main()
