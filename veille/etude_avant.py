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

Ecrit test_avant.json. Aucun reseau : lit les cours deja en cache
(.cache/cours, ecrits par statistiques.py et etude_usa.py).

Usage : python etude_avant.py
Bibliotheque standard uniquement.
"""

import bisect
import datetime as dt
import json
import os

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "test_avant.json")
COURS = os.path.join(ICI, ".cache", "cours")
HORIZONS = (1, 10, 30)
# Bornes basses (incluses) des tranches de R[N], en %.
BORNES = (-10, -5, -2, 2, 5, 10)
LIBELLES = ("< −10 %", "−10 à −5 %", "−5 à −2 %", "−2 à 2 %", "2 à 5 %", "5 à 10 %", "> 10 %")


def tranche(r):
    """Indice de la tranche de R (en %) : bornes basses incluses."""
    return bisect.bisect_right(BORNES, r)


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


def etudier(histo):
    """Tableaux d'une zone : pour chaque horizon, les 7 tranches avec le
    nombre de publications, de hausses et de baisses."""
    cases = {h: [{"n": 0, "hausses": 0, "baisses": 0, "somme_reaction_pct": 0.0} for _ in LIBELLES]
             for h in HORIZONS}
    total = {"n": 0, "hausses": 0, "baisses": 0}
    cours, sans_cours = {}, set()
    for e in histo["entreprises"]:
        t = e["ticker"]
        if t not in cours:
            cours[t] = charger_cours(t)
        if cours[t] is None:
            sans_cours.add(t)
            continue
        dates, clo = cours[t]
        for ev in e["resultats"]["evenements"]:
            rea = ev.get("rendement_pct")
            if rea is None or rea == 0 or ev["jour"] not in dates_index(t, dates):
                continue
            s = dates_index(t, dates)[ev["jour"]]
            hausse = rea > 0
            total["n"] += 1
            total["hausses" if hausse else "baisses"] += 1
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
    return {"depuis": histo["depuis"], "jusqu_a": histo["jusqu_a"], "n": total["n"], "hausses": total["hausses"],
            "baisses": total["baisses"],
            "horizons": [{"seances": h, "tranches": [{"libelle": LIBELLES[i], **c}
                                                      for i, c in enumerate(cases[h])]} for h in HORIZONS],
            "sans_cours": sorted(sans_cours)}


_index = {}


def dates_index(ticker, dates):
    """Position de chaque date dans la serie du titre (calculee une fois)."""
    if ticker not in _index:
        _index[ticker] = {d: i for i, d in enumerate(dates)}
    return _index[ticker]


def main():
    zones = {}
    for zone, histo in (("france", evenements_france()), ("usa", evenements_usa())):
        if histo:
            zones[zone] = etudier(histo)
    doc = {"version": 1, "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "tranches": list(LIBELLES), "zones": zones}
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    for zone, z in zones.items():
        print(f"test_avant.json : {zone} {z['n']} publications ({z['hausses']} hausses), "
              f"cours manquants : {len(z['sans_cours'])}.")


if __name__ == "__main__":
    main()
