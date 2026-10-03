#!/usr/bin/env python3
"""Consensus des analystes (Yahoo Finance) : releve quotidien pour les
entreprises du referentiel, afin de comparer chaque publication de resultats a
ce que le marche attendait.

Pour chaque entreprise :
  - estimations moyennes du benefice par action (BPA) et du chiffre
    d'affaires, pour le trimestre en cours, le suivant, l'annee en cours et la
    suivante, avec le nombre d'analystes (un releve n'est garde que s'il change) ;
  - revisions : analystes ayant releve ou abaisse leur estimation sur 7 et 30
    jours ;
  - surprises : BPA estime et publie des derniers trimestres (seulement pour les
    societes qui publient un BPA trimestriel), cumulees d'un releve a l'autre.

Historique gratuit disponible : Yahoo donne le BPA moyen d'il y a 7, 30, 60 et
90 jours (reconstitue au premier releve) et les surprises des 4 derniers
trimestres. Au-dela, l'historique se construit au fil des releves.

Yahoo est interroge une fois par jour (un second lancement le meme jour ne
fait rien) mais tous les releves ne sont pas gardes :
  - un par semaine et par entreprise (les revisions se lisent deja sur 7 a 90
    jours dans chaque releve) ;
  - un par jour autour d'une publication de resultats attendue (de 21 jours
    avant a 7 jours apres la date connue ou estimee) : le consensus de la
    veille sert a mesurer la surprise ;
  - le dernier releve, toujours (marque provisoire, remplace le lendemain),
    pour une publication a une date imprevue.

Ecrit consensus.json, lu par communiques.py (--a-classer : consensus avant la
publication) et par l'etude des communiques (etude_amf.py). Avec --zone usa :
entreprises americaines d'univers_objectifs.json, avec la prochaine date de
resultats (Yahoo), dans consensus_usa.json, lu par etude_usa.py.

Usage :
  python consensus.py              releve du jour (rien si deja fait aujourd'hui)
  python consensus.py --forcer     releve meme s'il a deja ete fait aujourd'hui
  python consensus.py --zone usa   entreprises americaines
Bibliotheque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import http.cookiejar
import json
import math
import os
import sys
import urllib.parse
import urllib.request

from collecte import _yahoo_get, http_get

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "consensus.json")
SORTIE_USA = os.path.join(ICI, "consensus_usa.json")
PERIODES = ("0q", "+1q", "0y", "+1y")
RECUL = {"7daysAgo": 7, "30daysAgo": 30, "60daysAgo": 60, "90daysAgo": 90}
INTERVALLE = 7  # jours entre deux releves gardes, loin d'une publication
AVANT_PUBLICATION, APRES_PUBLICATION = 21, 7  # releves quotidiens autour d'une publication
MODULES = "earningsTrend,earningsHistory"


def _sig(x, n=4):
    """Arrondi a n chiffres significatifs (releves comparables d'un jour a
    l'autre)."""
    if x is None or x == 0:
        return x
    return round(x, n - 1 - int(math.floor(math.log10(abs(x)))))


def _raw(d, *cles):
    for c in cles:
        d = (d or {}).get(c)
    return d.get("raw") if isinstance(d, dict) else d


def session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        http_get("https://fc.yahoo.com", opener)
    except Exception:
        pass  # la reponse est une erreur, mais le cookie est pose
    crumb = _yahoo_get("https://query1.finance.yahoo.com/v1/test/getcrumb", opener).decode()
    return opener, crumb


def lire(ticker, opener, crumb, modules=MODULES):
    """Releve du jour, BPA moyen des jours passes, revisions, surprises et
    prochaine date de resultats (si calendarEvents est demande)."""
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
           f"?modules={modules}&crumb={urllib.parse.quote(crumb)}")
    r = json.loads(_yahoo_get(url, opener))["quoteSummary"]["result"][0]
    dates = ((r.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
    prochaine = (dt.datetime.fromtimestamp(dates[0]["raw"], dt.timezone.utc).date().isoformat()
                 if dates and dates[0].get("raw") else None)
    releve = {"bpa": {}, "ca": {}, "fin": {}}
    passe = {}  # jours -> {periode: bpa moyen}
    revisions = {}
    for t in (r.get("earningsTrend") or {}).get("trend", []):
        p = t.get("period")
        if p not in PERIODES:
            continue
        if t.get("endDate"):
            releve["fin"][p] = t["endDate"]
        bpa = _raw(t, "earningsEstimate", "avg")
        if bpa is not None:
            releve["bpa"][p] = [_sig(bpa), _raw(t, "earningsEstimate", "numberOfAnalysts")]
        ca = _raw(t, "revenueEstimate", "avg")
        if ca:  # Yahoo met 0 quand il n'a rien
            releve["ca"][p] = [_sig(ca), _raw(t, "revenueEstimate", "numberOfAnalysts")]
        for cle, jours in RECUL.items():
            v = _raw(t, "epsTrend", cle)
            if v is not None:
                passe.setdefault(jours, {})[p] = _sig(v)
        rev = [_raw(t, "epsRevisions", c) for c in ("upLast7days", "upLast30days", "downLast7Days",
                                                    "downLast30days")]
        if any(x is not None for x in rev):
            revisions[p] = rev
    surprises = []
    for h in (r.get("earningsHistory") or {}).get("history", []):
        est, pub = _raw(h, "epsEstimate"), _raw(h, "epsActual")
        trim = (h.get("quarter") or {}).get("fmt")
        if trim and est is not None and pub is not None:
            surprises.append({"trimestre": trim, "estime": _sig(est), "publie": _sig(pub),
                              "surprise_pct": round((pub - est) / abs(est) * 100, 1) if est else None})
    return releve, passe, revisions, surprises, prochaine


def charger(chemin=SORTIE):
    if not os.path.exists(chemin):
        return {"version": 1, "entreprises": {}}
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def avant(fiche, jour):
    """Dernier releve strictement anterieur a `jour` (AAAA-MM-JJ), ou None."""
    rel = [r for r in (fiche or {}).get("releves", []) if r["jour"] < jour]
    return rel[-1] if rel else None


def revision_pct(fiche, jour, periode="0y", jours=30):
    """Evolution (%) du BPA moyen de `periode` entre le releve d'il y a
    `jours` jours et le dernier releve avant `jour` (meme exercice)."""
    fin = avant(fiche, jour)
    if not fin or periode not in fin.get("bpa", {}):
        return None
    limite = (dt.date.fromisoformat(fin["jour"]) - dt.timedelta(days=jours)).isoformat()
    debut = avant(fiche, (dt.date.fromisoformat(limite) + dt.timedelta(days=1)).isoformat())
    if (not debut or periode not in debut.get("bpa", {}) or debut["jour"] < (
            dt.date.fromisoformat(limite) - dt.timedelta(days=10)).isoformat()
            or debut.get("fin", {}).get(periode) != fin.get("fin", {}).get(periode)):
        return None
    a, b = debut["bpa"][periode][0], fin["bpa"][periode][0]
    return round((b - a) / abs(a) * 100, 1) if a else None


def dates_publication(usa=False):
    """Prochaine date de resultats connue ou estimee par entreprise (calcul
    precedent de statistiques.py, ou d'etude_usa.py)."""
    chemin = os.path.join(ICI, "communiques_usa.json" if usa else "statistiques.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            doc = json.load(f)
        histo = doc if usa else doc.get("historique_amf") or {}
    except (OSError, ValueError):
        return {}
    return {e["ticker"]: e["prochaine"]["date"] for e in histo.get("entreprises", [])
            if (e.get("prochaine") or {}).get("date")}


def garder(fiche, releve, aujourd_hui, publication):
    """Ajoute le releve du jour a l'historique de l'entreprise selon la regle
    du module (hebdomadaire, quotidien pres d'une publication, dernier releve
    provisoire). Retourne True si un releve definitif a ete ajoute."""
    jour = aujourd_hui.isoformat()
    rel = fiche["releves"]
    if rel and (rel[-1].get("provisoire") or rel[-1]["jour"] == jour):
        rel.pop()  # remplace par celui du jour
    dernier = rel[-1] if rel else None
    if dernier and {k: dernier.get(k) for k in ("bpa", "ca", "fin")} == releve:
        return False  # rien n'a change depuis le dernier releve garde
    proche = False
    if publication:
        d = dt.date.fromisoformat(publication)
        proche = d - dt.timedelta(days=AVANT_PUBLICATION) <= aujourd_hui <= d + dt.timedelta(days=APRES_PUBLICATION)
    definitif = (dernier is None or proche
                 or (aujourd_hui - dt.date.fromisoformat(dernier["jour"])).days >= INTERVALLE)
    rel.append(dict(releve, jour=jour) if definitif else dict(releve, jour=jour, provisoire=True))
    return definitif


def ecrire(doc, chemin=SORTIE):
    with open(chemin, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "source": "Yahoo Finance (consensus des analystes)",\n')
        f.write(f' "maj": {json.dumps(doc.get("maj"))},\n "entreprises": {{\n')
        f.write(",\n".join(f'  {json.dumps(t)}: {json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                           for t, v in sorted(doc["entreprises"].items())))
        f.write('\n }\n}\n')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--forcer", action="store_true", help="releve meme s'il a deja ete fait aujourd'hui")
    p.add_argument("--zone", choices=["france", "usa"], default="france")
    args = p.parse_args()
    usa = args.zone == "usa"
    chemin = SORTIE_USA if usa else SORTIE
    if usa:
        with open(os.path.join(ICI, "univers_objectifs.json"), encoding="utf-8") as f:
            tickers = [e["ticker"] for e in json.load(f)["entreprises"] if e["zone"] == "usa"]
    else:
        with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
            tickers = [e["ticker"] for e in json.load(f)["entreprises"]]
    doc = charger(chemin)
    aujourd_hui = dt.date.today()
    jour = aujourd_hui.isoformat()
    if doc.get("maj") == jour and not args.forcer:
        print(f"{os.path.basename(chemin)} : deja releve aujourd'hui ({jour}), rien a faire.")
        return
    publications = dates_publication(usa)
    try:
        opener, crumb = session()
    except Exception as e:
        print(f"Consensus indisponible (Yahoo) : {e}", file=sys.stderr)
        return
    erreurs, nouveaux = {}, 0
    with cf.ThreadPoolExecutor(4) as ex:
        modules = MODULES + (",calendarEvents" if usa else "")
        futurs = {ex.submit(lire, t, opener, crumb, modules): t for t in tickers}
        for fu in cf.as_completed(futurs):
            t = futurs[fu]
            try:
                releve, passe, revisions, surprises, prochaine = fu.result()
            except Exception as e:
                erreurs[t] = str(e)[:120]
                continue
            fiche = doc["entreprises"].setdefault(t, {"releves": [], "surprises": []})
            if not releve["bpa"] and not releve["ca"]:
                continue
            # Premier releve : le BPA moyen des 90 derniers jours donne deja
            # un debut d'historique (revisions avant la prochaine publication).
            if not fiche["releves"]:
                for jours in sorted(passe, reverse=True):
                    d = (aujourd_hui - dt.timedelta(days=jours)).isoformat()
                    fiche["releves"].append({"jour": d, "bpa": {p: [v, None] for p, v in passe[jours].items()},
                                             "ca": {}, "fin": releve["fin"], "reconstitue": True})
            nouveaux += garder(fiche, releve, aujourd_hui, publications.get(t) or prochaine)
            fiche["revisions"] = revisions
            if usa:
                fiche["prochaine"] = prochaine
            connus = {s["trimestre"]: s for s in fiche.get("surprises", [])}
            connus.update({s["trimestre"]: s for s in surprises})
            fiche["surprises"] = [connus[k] for k in sorted(connus)]
    doc["maj"] = jour
    ecrire(doc, chemin)
    avec = sum(1 for f in doc["entreprises"].values() if f["releves"])
    surpr = sum(len(f.get("surprises", [])) for f in doc["entreprises"].values())
    print(f"{os.path.basename(chemin)} : {avec} entreprise(s) suivie(s), {nouveaux} releve(s) garde(s), "
          f"{surpr} surprise(s) de BPA connue(s)"
          + (f" ; illisibles : {sorted(erreurs)}" if erreurs else "") + ".")


if __name__ == "__main__":
    main()
