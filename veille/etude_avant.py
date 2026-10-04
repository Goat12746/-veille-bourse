#!/usr/bin/env python3
"""Onglet Test : le rendement de l'action avant la publication des resultats
annonce-t-il le sens de la reaction ?

Pour chaque publication de resultats deja mesuree (France : historique_amf de
statistiques.json ; Etats-Unis : communiques_usa.json) :

  - R[N] = rendement de l'action (cours ajustes) sur les N seances qui
    precedent la seance de reaction, N = 1, 10 et 30 : cloture de la derniere
    seance avant la reaction / cloture N seances plus tot - 1 ;
  - sens de la reaction : hausse ou baisse du cours le jour de la reaction
    (rendement_pct de l'evenement, brut, sans correction de l'indice) ;
  - R[N] est range dans l'une des 7 tranches (< -10 %, -10 a -5, -5 a -2,
    -2 a 2, 2 a 5, 5 a 10, > 10 %), puis P(hausse | R[N] dans la tranche) est
    le nombre de hausses sur le nombre de publications de la tranche.

"ensuite" (aussi par entreprise) : rendement moyen des 20 seances avant la reaction et de la seance
de reaction, selon que le cours a monte ou baisse ce jour-la.

"consensus" (France seulement, seule zone ou le consensus du chiffre
d'affaires est connu) : P(hausse) selon l'ecart du BPA publie au consensus
(lignes) et celui du chiffre d'affaires (colonnes), publications ou les deux
sont connus (surprise_bpa_pct et surprise_ca_pct de statistiques.json) ; une
grille detaillee (9 tranches : bornes +-2, 5, 10 et 15 %) puis une grille 3 x 3
par seuil (au-dessus, conforme, en dessous).

Les tableaux sont aussi donnes par grand secteur (secteurs.py).

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
HORIZONS = (1, 10, 30)
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


def etudier(entreprises):
    """Tableaux d'un ensemble d'entreprises : pour chaque horizon, les 7
    tranches avec le nombre de publications, de hausses et de baisses."""
    cases = {h: [{"n": 0, "hausses": 0, "baisses": 0, "somme_reaction_pct": 0.0} for _ in LIBELLES]
             for h in HORIZONS}
    total = {"n": 0, "hausses": 0, "baisses": 0}
    # Cours recent (R[20]) et reaction du jour, moyennes selon le sens.
    ensuite = nouvelle_ensuite()
    par_ticker = {}
    couples = []  # (ecart BPA, ecart CA, hausse) face au consensus
    cours, sans_cours = {}, set()
    for e in entreprises:
        t = e["ticker"]
        if t not in cours:
            cours[t] = charger_cours(t)
        if cours[t] is None:
            sans_cours.add(t)
            continue
        dates, clo = cours[t]
        locale = par_ticker[t] = nouvelle_ensuite()
        for ev in e["resultats"]["evenements"]:
            rea = ev.get("rendement_pct")
            if rea is None or rea == 0 or ev["jour"] not in dates_index(t, dates):
                continue
            s = dates_index(t, dates)[ev["jour"]]
            hausse = rea > 0
            if ev.get("surprise_bpa_pct") is not None and ev.get("surprise_ca_pct") is not None:
                couples.append((ev["surprise_bpa_pct"], ev["surprise_ca_pct"], int(hausse)))
            total["n"] += 1
            total["hausses" if hausse else "baisses"] += 1
            if s - 1 - AVANT >= 0:
                for cumul in (ensuite, locale):
                    x = cumul["hausse" if hausse else "baisse"]
                    x["n"] += 1
                    x["avant"] += (clo[s - 1] / clo[s - 1 - AVANT] - 1) * 100
                    x["jour"] += rea
            for h in HORIZONS:
                if s - 1 - h < 0:
                    continue
                r = (clo[s - 1] / clo[s - 1 - h] - 1) * 100
                c = cases[h][tranche(r)]
                c["n"] += 1
                c["hausses" if hausse else "baisses"] += 1
                c["somme_reaction_pct"] += rea
    for h in HORIZONS:
        for c in cases[h]:
            c["reaction_moyenne_pct"] = round(c.pop("somme_reaction_pct") / c["n"], 2) if c["n"] else None
    res = {"n": total["n"], "hausses": total["hausses"], "baisses": total["baisses"], "ensuite": moyennes(ensuite),
            "entreprises": {t: moyennes(x) for t, x in par_ticker.items() if x["hausse"]["n"] + x["baisse"]["n"]},
            "horizons": [{"seances": h, "tranches": [{"libelle": LIBELLES[i], **c}
                                                      for i, c in enumerate(cases[h])]} for h in HORIZONS],
            "sans_cours": sorted(sans_cours)}
    if couples:
        res["consensus"] = grilles_consensus(couples)
    return res


def etudier_zone(histo):
    """Une zone : toutes les entreprises, puis chaque grand secteur."""
    zone = {"depuis": histo["depuis"], "jusqu_a": histo["jusqu_a"], **etudier(histo["entreprises"])}
    # Par entreprise : seulement au niveau de la zone (pas dans chaque secteur).
    secteurs = {}
    for nom in SECTEURS:
        membres = [e for e in histo["entreprises"] if grand_secteur(e.get("secteur")) == nom]
        if membres:
            secteurs[nom] = etudier(membres)
            secteurs[nom].pop("entreprises")
    zone["secteurs"] = secteurs
    return zone


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
    doc = {"version": 1, "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "tranches": list(LIBELLES), "zones": zones}
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    for zone, z in zones.items():
        print(f"test_avant.json : {zone} {z['n']} publications ({z['hausses']} hausses), "
              f"{len(z['secteurs'])} secteurs, cours manquants : {len(z['sans_cours'])}"
              + (f", BPA et CA face au consensus : {z['consensus']['n']}." if "consensus" in z else "."))


if __name__ == "__main__":
    main()
