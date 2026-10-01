#!/usr/bin/env python3
"""Positions courtes nettes (ventes a decouvert) rendues publiques par l'AMF
depuis novembre 2012 (data.gouv.fr, mis a jour chaque jour ouvre).

Un detenteur declare sa position des qu'elle atteint 0,5 % du capital, puis
a chaque palier de 0,1 point ; sous 0,5 % elle n'est plus publique. Pour
chaque entreprise du referentiel : total des positions publiques et nombre de
detenteurs, jour par jour (seuls les changements sont gardes).

Ecrit positions_courtes.json, lu par l'etude des communiques (etude_amf.py) :
part du capital vendue a decouvert la veille d'une publication de resultats.

Usage :
  python positions.py        met a jour si l'AMF a publie un nouvel export
                             (une fois par jour ouvre : rien a faire sinon)
Bibliotheque standard uniquement.
"""

import bisect
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "positions_courtes.json")
JEU = "https://www.data.gouv.fr/api/1/datasets/62738e33f1be79935d3e5553/"
SEUIL = 0.5  # % du capital : en dessous, la position n'est plus publique
DEPUIS = "2018-06-01"  # 20 seances avant le debut de l'etude des communiques (2019)
UA = "Mozilla/5.0 (veille-bourse; usage personnel)"


def _get(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def dernier_export():
    """(url, date de mise a jour) du dernier export CSV de l'AMF."""
    jeu = json.loads(_get(JEU, 60))
    res = max(jeu["resources"], key=lambda r: r.get("last_modified") or "")
    return res["url"], res.get("last_modified")


def series_par_isin(texte, isins):
    """{isin: [[jour, total_pct, n_detenteurs], ...]} : un point a chaque
    changement du total public."""
    lignes = csv.reader(io.StringIO(texte), delimiter=";")
    next(lignes)
    par_detenteur = {}
    for l in lignes:
        if len(l) < 8 or l[4] not in isins or not l[6]:
            continue
        try:
            ratio = float(l[3].replace(",", "."))
        except ValueError:
            continue
        cle = (l[1] or l[0], l[4])
        par_detenteur.setdefault(cle, []).append((l[6], l[7] or None, ratio))
    # Chaque position publiee vaut de sa publication jusqu'a la suivante du
    # meme detenteur, ou jusqu'a la fin de sa publication.
    variations = {}
    for (_, isin), pos in par_detenteur.items():
        pos.sort(key=lambda p: (p[0], p[1] or "9999"))
        for i, (debut, fin, ratio) in enumerate(pos):
            suivante = pos[i + 1][0] if i + 1 < len(pos) else None
            bornes = [d for d in (fin, suivante) if d]
            fin_eff = min(bornes) if bornes else None
            if ratio < SEUIL or (fin_eff is not None and fin_eff <= debut):
                continue
            v = variations.setdefault(isin, {})
            v.setdefault(debut, [0.0, 0])
            v[debut][0] += ratio
            v[debut][1] += 1
            if fin_eff:
                v.setdefault(fin_eff, [0.0, 0])
                v[fin_eff][0] -= ratio
                v[fin_eff][1] -= 1
    res = {}
    for isin, v in variations.items():
        total, n, points = 0.0, 0, []
        for jour in sorted(v):
            total += v[jour][0]
            n += v[jour][1]
            point = [jour, round(max(total, 0.0), 2), max(n, 0)]
            if not points or points[-1][1:] != point[1:]:
                points.append(point)
        # Avant DEPUIS, seul le dernier etat compte (niveau de depart).
        avant = [p for p in points if p[0] < DEPUIS]
        res[isin] = avant[-1:] + [p for p in points if p[0] >= DEPUIS]
    return res


def charger():
    """Contenu de positions_courtes.json ({} s'il n'existe pas)."""
    if not os.path.exists(SORTIE):
        return {}
    with open(SORTIE, encoding="utf-8") as f:
        return json.load(f)


def niveau(points, avant):
    """(total_pct, n_detenteurs) public la veille de `avant` (AAAA-MM-JJ) :
    dernier changement strictement anterieur. (0, 0) sans position."""
    if not points:
        return 0.0, 0
    i = bisect.bisect_left([p[0] for p in points], avant) - 1
    return (points[i][1], points[i][2]) if i >= 0 else (0.0, 0)


def main():
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        ref = json.load(f)["entreprises"]
    isins = {e["isin"]: e["ticker"] for e in ref if e.get("isin")}
    try:
        url, maj = dernier_export()
        if maj and maj == charger().get("maj_source"):
            print(f"positions_courtes.json : deja a jour (export de l'AMF du {maj[:10]}), rien a faire.")
            return
        texte = _get(url).decode("utf-8-sig")
    except Exception as e:
        print(f"Positions courtes indisponibles (le fichier precedent est garde) : {e}", file=sys.stderr)
        return
    series = series_par_isin(texte, set(isins))
    doc = {"version": 1, "source": "AMF, positions courtes nettes publiques (data.gouv.fr)", "maj_source": maj,
           "seuil_pct": SEUIL,
           "entreprises": {isins[i]: {"isin": i, "points": p} for i, p in sorted(series.items())}}
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n')
        entetes = [f' {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}' for k, v in doc.items()
                   if k != "entreprises"]
        f.write(",\n".join(entetes) + ',\n "entreprises": {\n')
        f.write(",\n".join(f'  {json.dumps(t)}: {json.dumps(v, separators=(",", ":"))}'
                           for t, v in doc["entreprises"].items()))
        f.write('\n }\n}\n')
    aujourd_hui = dt.date.today().isoformat()
    actives = {t: niveau(v["points"], "9999") for t, v in doc["entreprises"].items()}
    actives = {t: x for t, x in actives.items() if x[0] > 0}
    print(f"positions_courtes.json : {len(doc['entreprises'])} entreprise(s) avec un historique, "
          f"{len(actives)} avec des positions publiques au {aujourd_hui} "
          f"(plus forte : {max(actives.items(), key=lambda kv: kv[1][0], default=('-', (0, 0)))}).")


if __name__ == "__main__":
    main()
