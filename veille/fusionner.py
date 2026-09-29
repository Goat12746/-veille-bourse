#!/usr/bin/env python3
"""Publie alertes.json (lu par l'application) a partir de la collecte
(sortie/candidats.json) et de l'analyse de Claude (sortie/analyse.json).

  python fusionner.py            analyse de Claude requise
  python fusionner.py --sans-ia  alertes brutes, sens deduit des mots-cles

Les alertes deja publiees sont conservees 60 jours ; les candidats traites
(publies ou ecartes) sont memorises dans etat.json pour ne pas etre
proposes de nouveau a l'analyse.
"""

import argparse
import datetime as dt
import json
import os
import sys

from valider import tickers_connus, valider_alertes, valider_analyse

ICI = os.path.dirname(os.path.abspath(__file__))
CONSERVATION_ALERTES = 60  # jours
CONSERVATION_ETAT = 120  # jours


def charger(chemin, defaut=None):
    if not os.path.exists(chemin):
        return defaut
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def ecrire(chemin, doc):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")


def etape_brute(c):
    """Etape deduite des metadonnees, pour les alertes sans IA."""
    if c["source"] == "amf":
        return "publie"
    if c["source"] == "assemblee":
        sort = (c["meta"].get("sort") or "").lower()
        if sort.startswith("adopt"):
            return "adopte_seance" if c["meta"].get("organe") == "AN" else "adopte_commission"
        if sort.startswith(("rejet", "tomb", "retir", "non soutenu", "irrecevable")):
            return "rejete"
        return "depot"
    return "information"


def alerte_brute(c, noms):
    return {
        "id": c["id"],
        "titre": c["titre"],
        "resume": c["extrait"] or c["titre"],
        "etape": etape_brute(c),
        "probabilite": 0.5,
        "themes": c["themes"],
        "entreprises": [
            {"ticker": t, "sens": c["sens_indicatif"], "ampleur": "inconnue",
             "justification": "Détecté par mots-clés (" + ", ".join(c["themes"]) + ")" if c["themes"]
             else "Communiqué de l'entreprise", "extrait": c["extrait"]}
            for t in c["entreprises_potentielles"] if t in noms
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sans-ia", action="store_true", help="publier les candidats sans analyse de Claude")
    args = p.parse_args()

    today = dt.date.today()
    tickers = tickers_connus()
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        noms = {e["ticker"]: e["nom"] for e in json.load(f)["entreprises"]}
    collecte = charger(os.path.join(ICI, "sortie", "candidats.json"))
    if collecte is None:
        sys.exit("sortie/candidats.json introuvable : lancer d'abord collecte.py")
    candidats = {c["id"]: c for c in collecte["candidats"]}
    publie = charger(os.path.join(ICI, "alertes.json"), {"alertes": []})

    if args.sans_ia:
        analyse = {"alertes": [alerte_brute(c, noms) for c in candidats.values()
                               if any(t in noms for t in c["entreprises_potentielles"])],
                   "ecartes": []}
        mode = "mots_cles"
    else:
        analyse = charger(os.path.join(ICI, "sortie", "analyse.json"))
        if analyse is None:
            sys.exit("sortie/analyse.json introuvable (ou utiliser --sans-ia)")
        erreurs = valider_analyse(analyse, set(candidats), tickers,
                                  ids_publies={a["id"] for a in publie.get("alertes", [])})
        if erreurs:
            print("analyse.json invalide :")
            for e in erreurs:
                print(" -", e)
            sys.exit(1)
        mode = "claude"

    # Nouvelles alertes : metadonnees reprises de la collecte (date, source,
    # liens), contenu repris de l'analyse.
    nouvelles = []
    for a in analyse["alertes"]:
        c = candidats[a["id"]]
        lies = [candidats[i] for i in a.get("ids_lies") or [] if i in candidats]
        nouvelles.append({
            "id": a["id"],
            "date": max([c["date"]] + [l["date"] for l in lies]),
            "source": c["source"],
            "titre": a["titre"],
            "resume": a["resume"],
            "url": c["url"],
            "etape": a["etape"],
            "probabilite": a["probabilite"],
            "themes": a.get("themes") or c["themes"],
            "entreprises": [dict(e, nom=noms[e["ticker"]]) for e in a["entreprises"]],
            "sources": [{"titre": x["titre"], "url": x["url"], "date": x["date"],
                         "media": x["meta"].get("media", x["source"])} for x in [c] + lies],
            "analyse": mode,
            "ajoute_le": today.isoformat(),
            # Texte porteur de la mesure (PLF 2027, n° 2892...) et article.
            **{k: a[k] for k in ("texte", "article") if a.get(k)},
        })

    # Fusion avec les alertes deja publiees, apres application des mises a
    # jour de l'analyse (ex. mesure retrouvee dans le texte depose : etape,
    # article, probabilite).
    limite = (today - dt.timedelta(days=CONSERVATION_ALERTES)).isoformat()
    par_id = {a["id"]: a for a in publie.get("alertes", []) if a["date"] >= limite}
    for m in analyse.get("mises_a_jour") or []:
        if m["id"] in par_id:
            par_id[m["id"]].update({k: v for k, v in m.items() if k != "id"})
            par_id[m["id"]]["maj_le"] = today.isoformat()
    for a in nouvelles:
        par_id[a["id"]] = a

    # Calendrier : agenda et dates Yahoo de la collecte, dates ajoutees par
    # Claude. Une source en erreur lors de cette collecte (reseau) ne doit pas
    # effacer ce qu'elle avait publie : on garde alors ses evenements deja
    # publies, a venir.
    hier = (today - dt.timedelta(days=1)).isoformat()
    ok = lambda nom: (collecte.get("sources", {}).get(nom) or {}).get("ok", False)
    # Origine d'un evenement deja publie, d'apres sa source.
    def origine(ev):
        src = ev.get("source") or ""
        if src.startswith("Assemblée nationale"):
            return "agenda"
        return "calendrier" if src == "Yahoo Finance" else "claude"

    precedents = [ev for ev in publie.get("calendrier", []) if ev["date"] >= hier]
    evenements = list(analyse.get("calendrier") or [])
    for nom in ("agenda", "calendrier"):
        if ok(nom):
            evenements += collecte.get(nom, [])
        else:
            evenements += [ev for ev in precedents if origine(ev) == nom]
    # Dates ajoutees par Claude lors des analyses precedentes.
    evenements += [ev for ev in precedents if origine(ev) == "claude"]
    # Mesures en jeu par texte : alertes (non rejetees) portant sur ce texte.
    par_texte = {}
    for a in par_id.values():
        if a.get("texte") and a["etape"] != "rejete":
            par_texte.setdefault(a["texte"], []).append(a)

    evts = {}
    for ev in evenements:
        if ev["date"] < hier:
            continue
        # Entreprises trouvees par mots-cles ; celles des alertes sont
        # recalculees a chaque publication (une mesure abandonnee disparait).
        base = [t for t in ev.get("tickers_mots_cles", ev.get("tickers")) or [] if t in tickers]
        mesures = par_texte.get(ev.get("texte") or "", [])
        lies = [e["ticker"] for a in mesures for e in a["entreprises"]]
        evts[(ev["date"], ev["type"], ev["titre"])] = {
            "date": ev["date"], "heure": ev.get("heure", ""), "type": ev["type"],
            "titre": ev["titre"],
            "tickers": list(dict.fromkeys(base + [t for t in lies if t in tickers])),
            "tickers_mots_cles": base,
            "themes": ev.get("themes") or [], "texte": ev.get("texte") or "",
            "mesures": [{"alerte_id": a["id"], "titre": a["titre"], "etape": a["etape"],
                         "article": a.get("article", "")} for a in mesures],
            "source": ev.get("source", ""),
            "url": ev.get("url", ""), "fiable": bool(ev.get("fiable", True))}

    sortie = {
        "version": 1,
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": collecte.get("sources", {}),
        "alertes": sorted(par_id.values(), key=lambda a: (a["date"], a["ajoute_le"]), reverse=True),
        "calendrier": sorted(evts.values(), key=lambda e: (e["date"], e["heure"])),
    }
    erreurs = valider_alertes(sortie, tickers)
    if erreurs:
        print("alertes.json invalide, rien n'est publie :")
        for e in erreurs:
            print(" -", e)
        sys.exit(1)
    ecrire(os.path.join(ICI, "alertes.json"), sortie)

    # Candidats traites : ne plus les proposer a l'analyse.
    etat = charger(os.path.join(ICI, "etat.json"), {"ids_traites": {}})
    traites = set(candidats) if args.sans_ia else (
        {a["id"] for a in analyse["alertes"]}
        | {i for a in analyse["alertes"] for i in a.get("ids_lies") or []}
        | set(analyse.get("ecartes") or []))
    for cid in traites:
        etat["ids_traites"][cid] = today.isoformat()
    limite_etat = (today - dt.timedelta(days=CONSERVATION_ETAT)).isoformat()
    etat["ids_traites"] = {k: v for k, v in etat["ids_traites"].items() if v >= limite_etat}
    ecrire(os.path.join(ICI, "etat.json"), etat)

    non_traites = len(set(candidats) - traites)
    print(f"alertes.json : {len(nouvelles)} nouvelle(s) alerte(s), {len(sortie['alertes'])} au total, "
          f"{len(sortie['calendrier'])} evenement(s) de calendrier.")
    if non_traites:
        print(f"Attention : {non_traites} candidat(s) ni publies ni ecartes (ils seront reproposes).")


if __name__ == "__main__":
    main()
