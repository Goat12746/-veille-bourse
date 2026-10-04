#!/usr/bin/env python3
"""consensus_historique.json : BPA estime avant chaque publication et BPA publie
(calendrier des resultats de Yahoo Finance, get_earnings_dates), depuis fin
2014, pour les entreprises francaises et americaines : {ticker: [[date, estime,
publie]]}. Lu par etude_amf.py et etude_usa.py.

Source brute : yahoo_bpa.json (etude_surprise/recolte_yahoo.py de l'application,
60 trimestres par valeur, ecrit {ticker: [[date heure locale, estime, publie,
surprise %]]}).

Usage : python historique_bpa.py [yahoo_bpa.json]
"""
import json
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
DEBUT = "2014-10-01"  # quelques mois avant 2015 : une publication de janvier peut figurer sous une date voisine
BRUT = os.path.join(os.path.dirname(os.path.dirname(ICI)), "AndroidStudioProjects", "portfolio_app", "etude_surprise",
                    "yahoo_bpa.json")


def main():
    chemin = sys.argv[1] if len(sys.argv) > 1 else BRUT
    with open(chemin, encoding="utf-8") as f:
        brut = json.load(f)
    res = {}
    for t, lignes in brut.items():
        if not isinstance(lignes, list):
            continue
        v = sorted(([r[0][:10], r[1], r[2]] for r in lignes if r[0][:10] >= DEBUT and r[1] is not None
                    and r[2] is not None), key=lambda r: r[0])
        if v:
            res[t] = v
    with open(os.path.join(ICI, "consensus_historique.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"consensus_historique.json : {len(res)} valeurs, {sum(len(v) for v in res.values())} lignes depuis {DEBUT}.")


if __name__ == "__main__":
    main()
