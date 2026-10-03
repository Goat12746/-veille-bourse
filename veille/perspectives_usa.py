#!/usr/bin/env python3
"""Perspectives des publications de resultats americaines reperees par
mots-cles, sans IA (encadre « Ce qui compte : les perspectives » de l'onglet
Statistiques > Communiques > Etats-Unis) : formules explicites du communique
de presse (« raises its full-year guidance », « lowers outlook »,
« reaffirms guidance »...). Une publication sans formule reconnue reste sans
perspectives. Les avis de Claude (depuis octobre 2026) priment dans l'etude.

Ecrit resultats_usa_perspectives.json : {numero: [valeur ou null, phrase]}.
Seules les publications pas encore lues le sont (reprise possible).

Usage : python perspectives_usa.py [--max N]
Bibliotheque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys

import communiques_usa as cu
import edgar

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "resultats_usa_perspectives.json")


def charger():
    if not os.path.exists(SORTIE):
        return {}
    with open(SORTIE, encoding="utf-8") as f:
        return json.load(f).get("publications", {})


def ecrire(doc):
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "source": "communiques de presse (SEC), mots-cles",\n "publications": {\n')
        f.write(",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in sorted(doc.items())))
        f.write("\n }\n}\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max", type=int, default=0, help="nombre maximal de communiques lus (0 : tous)")
    args = p.parse_args()
    doc = charger()
    pubs = [(v["cik"], x["id"]) for v in edgar.charger()["entreprises"].values() for x in v["publications"]
            if x.get("fin") and x["id"] not in doc]
    if args.max:
        pubs = pubs[:args.max]
    print(f"Perspectives : {len(pubs)} communiqué(s) à lire…", file=sys.stderr)

    def lire(cik, numero):
        texte, _ = cu.communique(cik, numero)
        valeur, phrase = cu.perspectives_mots(texte)
        return [valeur, (phrase or "")[:200] or None]

    erreurs = 0
    with cf.ThreadPoolExecutor(4) as ex:
        futurs = {ex.submit(lire, c, n): n for c, n in pubs}
        for i, fu in enumerate(cf.as_completed(futurs), 1):
            try:
                doc[futurs[fu]] = fu.result()
            except Exception:
                erreurs += 1  # relu au prochain passage
            if i % 500 == 0:
                print(f"  {i}/{len(pubs)}", file=sys.stderr)
                ecrire(doc)
    ecrire(doc)
    trouves = sum(1 for v in doc.values() if v[0])
    print(f"resultats_usa_perspectives.json : {len(doc)} communiqués lus, perspectives repérées dans {trouves}"
          + (f" ; {erreurs} illisible(s), relus au prochain passage" if erreurs else "") + ".")


if __name__ == "__main__":
    main()
