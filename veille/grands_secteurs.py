#!/usr/bin/env python3
"""Grand secteur (un des 11, secteurs.py) de chaque action de l'application :
tri par secteur de l'onglet Analyse et de la recherche de Statistiques >
Resultats. Sources : univers_objectifs.json (STOXX 600, S&P 500, Nasdaq-100,
deja en 11 secteurs) puis referentiel.json (SBF 120, secteurs detailles
regroupes).

Ecrit grands_secteurs.json (a copier dans veille/ de l'application).
Usage : python grands_secteurs.py
"""

import json
import os

from secteurs import grand_secteur

ICI = os.path.dirname(os.path.abspath(__file__))


def main():
    tickers = {}
    for fichier in ("univers_objectifs.json", "referentiel.json"):
        with open(os.path.join(ICI, fichier), encoding="utf-8") as f:
            for e in json.load(f)["entreprises"]:
                s = grand_secteur(e.get("secteur"))
                if s and e["ticker"] not in tickers:
                    tickers[e["ticker"]] = s
    with open(os.path.join(ICI, "grands_secteurs.json"), "w", encoding="utf-8") as f:
        json.dump({"tickers": dict(sorted(tickers.items()))}, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"grands_secteurs.json : {len(tickers)} action(s).")


if __name__ == "__main__":
    main()
