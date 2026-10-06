#!/usr/bin/env python3
"""Onglet Test : le rendement de l'action avant la publication des resultats
annonce-t-il le sens de la reaction ?

L'historique fige (publications avant DEBUT_SUIVI) est calcule sur les
donnees FMP (donnees-fmp/stat_fmp.py, communiques_fmp.json, integre a
l'application). Ce script ne mesure que les publications suivies depuis
DEBUT_SUIVI (France : historique_amf de statistiques.json ; Etats-Unis :
communiques_usa.json ; STOXX 600 : communiques_stoxx.json), bloc "suivi" que
l'application additionne a l'historique FMP :

  - R[30] = rendement de l'action (cours ajustes) sur les 30 seances qui
    precedent la seance de reaction, range dans l'une des 7 tranches
    (< -10 %, -10 a -5, -5 a -2, -2 a 2, 2 a 5, 5 a 10, > 10 %) ; sens de la
    reaction : hausse ou baisse du cours le jour de la reaction (rendement_pct,
    brut) ; P(hausse | tranche) = hausses / publications ;
  - "ensuite" (zone et chaque entreprise) : rendement moyen des 20 seances
    avant la reaction et de la seance de reaction, selon le sens du jour ;
  - "consensus" : P(hausse) selon l'ecart du BPA (lignes) et du chiffre
    d'affaires (colonnes) au consensus, grille detaillee (bornes +-2, 5, 10 et
    15 %) puis une grille 3 x 3 par seuil.

Memes tableaux par grand secteur (secteurs.py), sauf "ensuite".

Ecrit test_avant.json. Aucun reseau : lit les cours deja en cache
(.cache/cours, ecrits par statistiques.py et etude_usa.py).

Usage : python etude_avant.py
Bibliotheque standard uniquement.
"""

import bisect
import datetime as dt
import json
import os

from secteurs import SECTEURS, grand_secteur

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "test_avant.json")
COURS = os.path.join(ICI, ".cache", "cours")
HORIZON = 30  # seances de R[N] (R[1] et R[10] retires de l'application)
AVANT = 20  # seances du "cours recent" de l'encadre Avant / jour J
# Bornes basses (incluses) des tranches de R[N], en %.
BORNES = (-10, -5, -2, 2, 5, 10)
LIBELLES = ("< −10 %", "−10 à −5 %", "−5 à −2 %", "−2 à 2 %", "2 à 5 %", "5 à 10 %", "> 10 %")
# Ecart au consensus (BPA et chiffre d'affaires) : grille detaillee, puis un
# seuil a la fois.
BORNES_CONSENSUS = (-15, -10, -5, -2, 2, 5, 10, 15)
LIBELLES_CONSENSUS = ("< −15 %", "−15 à −10 %", "−10 à −5 %", "−5 à −2 %", "−2 à 2 %", "2 à 5 %", "5 à 10 %",
                      "10 à 15 %", "> 15 %")
SEUILS_CONSENSUS = (2, 5, 10, 15)
# Debut du suivi : l'application a un historique fige des publications
# anterieures (BPA et chiffre d'affaires face au consensus) et y ajoute celles
# qui reagissent a partir de cette date (bloc "suivi" de chaque ensemble).
DEBUT_SUIVI = "2026-10-05"


def tranche(r):
    """Indice de la tranche de R (en %) : bornes basses incluses."""
    return bisect.bisect_right(BORNES, r)


def grilles_consensus(couples):
    """couples = [(ecart BPA %, ecart CA %, hausse)] : grille detaillee puis
    une grille 3 x 3 par seuil ; cases[i][j] = BPA dans la tranche i, CA dans
    la tranche j (n, hausses)."""
    def grille(seuil, bornes, libelles):
        cases = [[{"n": 0, "hausses": 0} for _ in libelles] for _ in libelles]
        for bpa, ca, hausse in couples:
            c = cases[bisect.bisect_right(bornes, bpa)][bisect.bisect_right(bornes, ca)]
            c["n"] += 1
            c["hausses"] += hausse
        return {"seuil": seuil, "libelles": list(libelles), "cases": cases}

    return {"n": len(couples), "hausses": sum(h for _, _, h in couples),
            "grilles": [grille(None, BORNES_CONSENSUS, LIBELLES_CONSENSUS)]
            + [grille(t, (-t, t), (f"< −{t} %", f"±{t} %", f"> {t} %")) for t in SEUILS_CONSENSUS]}


def charger_cours(ticker):
    """Cloture ajustee d'un titre : (dates, clotures), ou None."""
    chemin = os.path.join(COURS, ticker.replace("^", "_") + ".json")
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8") as f:
        seances = json.load(f)["seances"]
    dates = sorted(d for d, v in seances.items() if v[1])
    return dates, [seances[d][1] for d in dates]


def evenements_france():
    chemin = os.path.join(ICI, "statistiques.json")
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8") as f:
        histo = json.load(f).get("historique_amf")
    return histo


def evenements_usa():
    chemin = os.path.join(ICI, "communiques_usa.json")
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def evenements_stoxx():
    chemin = os.path.join(ICI, "communiques_stoxx.json")
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def nouvelle_ensuite():
    return {k: {"n": 0, "avant": 0.0, "jour": 0.0} for k in ("hausse", "baisse")}


def moyennes(ensuite):
    return {k: {"n": x["n"], "avant_20_pct": round(x["avant"] / x["n"], 2) if x["n"] else None,
                "jour_pct": round(x["jour"] / x["n"], 2) if x["n"] else None} for k, x in ensuite.items()}


def etudier(entreprises, par_entreprise=False):
    """Bloc "suivi" d'un ensemble d'entreprises : publications depuis
    DEBUT_SUIVI (l'historique fige vient de FMP, communiques_fmp.json, et
    l'application les additionne). Les 7 tranches de R[30] (publications et
    hausses), la grille consensus, l'encadre Avant / jour J, et par entreprise
    si demande."""
    cases = [{"n": 0, "hausses": 0} for _ in LIBELLES]
    total = {"n": 0, "hausses": 0}
    ensuite, par_ticker, couples = nouvelle_ensuite(), {}, []
    for e in entreprises:
        t = e["ticker"]
        evs = [ev for ev in e["resultats"]["evenements"] if ev["jour"] >= DEBUT_SUIVI and ev.get("rendement_pct")]
        if not evs:
            continue
        cours = charger_cours(t)
        if cours is None:
            continue
        dates, clo = cours
        index = dates_index(t, dates)
        locale = nouvelle_ensuite()
        for ev in evs:
            if ev["jour"] not in index:
                continue
            s, rea = index[ev["jour"]], ev["rendement_pct"]
            hausse = rea > 0
            if ev.get("surprise_bpa_pct") is not None and ev.get("surprise_ca_pct") is not None:
                couples.append((ev["surprise_bpa_pct"], ev["surprise_ca_pct"], int(hausse)))
            total["n"] += 1
            total["hausses"] += hausse
            if s - 1 - AVANT >= 0:
                for cumul in (ensuite, locale):
                    x = cumul["hausse" if hausse else "baisse"]
                    x["n"] += 1
                    x["avant"] += (clo[s - 1] / clo[s - 1 - AVANT] - 1) * 100
                    x["jour"] += rea
            if s - 1 - HORIZON >= 0:
                c = cases[tranche((clo[s - 1] / clo[s - 1 - HORIZON] - 1) * 100)]
                c["n"] += 1
                c["hausses"] += hausse
        if locale["hausse"]["n"] + locale["baisse"]["n"]:
            par_ticker[t] = moyennes(locale)
    res = {"depuis": DEBUT_SUIVI, "n": total["n"], "hausses": total["hausses"],
           "horizons": [{"seances": HORIZON, "tranches": [{"libelle": LIBELLES[i], **c} for i, c in enumerate(cases)]}],
           "consensus": grilles_consensus(couples), "ensuite": moyennes(ensuite)}
    if par_entreprise:
        res["entreprises"] = par_ticker
    return res


def etudier_zone(histo):
    """Une zone : toutes les entreprises (et chacune), puis chaque grand
    secteur ayant des publications suivies."""
    secteurs = {}
    for nom in SECTEURS:
        membres = [e for e in histo["entreprises"] if grand_secteur(e.get("secteur")) == nom]
        s = etudier(membres) if membres else None
        if s and s["n"]:
            s.pop("ensuite")
            secteurs[nom] = {"suivi": s}
    return {"suivi": etudier(histo["entreprises"], par_entreprise=True), "secteurs": secteurs}


_index = {}


def dates_index(ticker, dates):
    """Position de chaque date dans la serie du titre (calculee une fois)."""
    if ticker not in _index:
        _index[ticker] = {d: i for i, d in enumerate(dates)}
    return _index[ticker]


def main():
    zones = {}
    for zone, histo in (("france", evenements_france()), ("usa", evenements_usa()), ("stoxx", evenements_stoxx())):
        if histo:
            zones[zone] = etudier_zone(histo)
    doc = {"version": 2, "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "tranches": list(LIBELLES), "zones": zones}
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    for zone, z in zones.items():
        print(f"test_avant.json : {zone} {z['suivi']['n']} publications suivies depuis {DEBUT_SUIVI} "
              f"({z['suivi']['hausses']} hausses), {len(z['secteurs'])} secteurs.")


if __name__ == "__main__":
    main()
