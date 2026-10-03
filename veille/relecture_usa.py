#!/usr/bin/env python3
"""Relecture par Claude des perspectives des publications americaines
comparees au consensus (environ 2 000 depuis fin 2025) : les mots-cles
(perspectives_usa.py) ne reconnaissent qu'une formule sur quatre et se
trompent parfois. Les perspectives relues priment ensuite sur les mots-cles
dans l'etude (etude_usa.py), apres les avis du jour de publication.

Economie du quota : seul un extrait court est lu (titre et phrases sur les
perspectives, 1 200 caracteres au plus), par lots de 100 publications, avec
des consignes courtes (relecture/CONSIGNES.md) et une reponse d'un mot par
publication.

Usage :
  python relecture_usa.py --preparer          telecharge les communiques et ecrit relecture/lot_NN.json
  python relecture_usa.py --suivant           numero du prochain lot a relire (rien s'il n'en reste pas)
  python relecture_usa.py --integrer FICHIER  ajoute les reponses {numero: valeur} a relecture/perspectives_claude.json
  python relecture_usa.py --etat              lots relus et restants
Bibliotheque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys

import communiques_usa as cu
import consensus as consensus_mod

ICI = os.path.dirname(os.path.abspath(__file__))
DOSSIER = os.path.join(ICI, "relecture")
SORTIE = os.path.join(DOSSIER, "perspectives_claude.json")
TAILLE_LOT = 100
MAXI = 1200  # caracteres d'extrait par publication
VALEURS = ("relevees", "confirmees", "abaissees", "nouvelles")


def _charger(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def lues():
    return _charger(SORTIE, {}).get("publications", {})


def ecrire(doc):
    os.makedirs(DOSSIER, exist_ok=True)
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "source": "relecture par Claude des perspectives (extraits des communiques)",\n'
                ' "publications": {\n')
        f.write(",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(doc.items())))
        f.write("\n }\n}\n")


# Phrases juridiques ou de contact : sans information sur les perspectives.
_BRUIT = re.compile(r"forward-looking|safe harbor|pursuant to|risks and uncertainties|reconciliation|non-gaap "
                    r"financial measures? (?:is|are) |webcast|conference call|investor contact|media contact|"
                    r"@|undue reliance|private securities litigation", re.I)


def extrait_perspectives(texte, maxi=MAXI):
    """Phrase titre (« ... Reports Third Quarter Results ») et phrases qui
    parlent des perspectives, sans mentions juridiques ni contacts."""
    t = " ".join((texte or "").split())
    phrases = [p for p in re.split(r"(?<=[.;!?])\s+", t) if 30 < len(p) < 700 and not _BRUIT.search(p)]
    titre = next((p for p in phrases[:15] if re.search(r"report|announce|result", p, re.I)), "")
    titre = re.sub(r"^.*?Document\s+", "", titre)[-250:]
    garde, total = [], len(titre)
    for p in phrases:
        if p != titre and any(m in p.lower() for m in cu._OUTLOOK) and p not in garde:
            garde.append(p)
            total += len(p) + 1
            if total >= maxi:
                break
    return (titre + (" [...] " + " ".join(garde) if garde else ""))[:maxi]


def publications_comparees():
    """Publications americaines comparees au consensus (BPA Yahoo) : celle
    qui suit chaque trimestre de moins de 110 jours (comme etude_amf.py).
    [(numero, cik, ticker, nom, periode)]."""
    doc = cu.charger(os.path.join(ICI, "resultats_usa.json"), {"entreprises": {}})
    estimations = consensus_mod.charger(consensus_mod.SORTIE_USA).get("entreprises", {})
    res = []
    for ticker, e in doc["entreprises"].items():
        pubs = sorted(e.get("publications", []), key=lambda p: p.get("publie_le", ""))
        for s in (estimations.get(ticker) or {}).get("surprises", []):
            if s.get("surprise_pct") is None:
                continue
            fin = (dt.date.fromisoformat(s["trimestre"]) + dt.timedelta(days=110)).isoformat()
            p = next((p for p in pubs if s["trimestre"] < p.get("publie_le", "")[:10] <= fin), None)
            if p:
                res.append((p["id"], e.get("cik"), ticker, e.get("nom", ticker), p.get("titre", "")))
    return sorted(set(res))


def preparer():
    deja = cu.charger(cu.CLASSES, {"classes": {}})["classes"]  # avis du jour : perspectives deja jugees
    a_lire = [x for x in publications_comparees() if x[0] not in deja]
    print(f"{len(a_lire)} publications a extraire", file=sys.stderr)

    def lire(x):
        numero, cik, ticker, nom, titre = x
        try:
            texte, _ = cu.communique(cik, numero)
        except Exception:  # noqa: BLE001 - depot illisible : ignore
            return None
        if not texte:
            return None
        return {"id": numero, "entreprise": nom, "publication": titre, "extrait": extrait_perspectives(texte)}

    with cf.ThreadPoolExecutor(4) as ex:
        lus = [r for r in ex.map(lire, a_lire) if r]
    os.makedirs(DOSSIER, exist_ok=True)
    for f in os.listdir(DOSSIER):
        if f.startswith("lot_"):
            os.remove(os.path.join(DOSSIER, f))
    for i in range(0, len(lus), TAILLE_LOT):
        n = i // TAILLE_LOT + 1
        with open(os.path.join(DOSSIER, f"lot_{n:02d}.json"), "w", encoding="utf-8") as f:
            json.dump({"lot": n, "publications": lus[i:i + TAILLE_LOT]}, f, ensure_ascii=False, indent=0)
    print(f"relecture/ : {len(lus)} extraits en {-(-len(lus) // TAILLE_LOT)} lots de {TAILLE_LOT} "
          f"({sum(len(x['extrait']) for x in lus) // 1000} k caracteres).")


def lots():
    return sorted(f for f in os.listdir(DOSSIER) if re.fullmatch(r"lot_\d+\.json", f)) if os.path.isdir(DOSSIER) else []


def restants():
    faites = lues()
    res = []
    for f in lots():
        ids = [p["id"] for p in _charger(os.path.join(DOSSIER, f), {}).get("publications", [])]
        if any(i not in faites for i in ids):
            res.append(f)
    return res


def integrer(chemin):
    nouveaux = _charger(chemin, None)
    if not isinstance(nouveaux, dict):
        sys.exit(f"{chemin} : objet JSON {{numero: valeur}} attendu")
    connus = {p["id"] for f in lots() for p in _charger(os.path.join(DOSSIER, f), {}).get("publications", [])}
    erreurs = [f"{k} : numero inconnu" for k in nouveaux if k not in connus]
    erreurs += [f"{k} : {v!r}, attendu {', '.join(VALEURS)} ou null" for k, v in nouveaux.items()
                if v not in VALEURS + (None,)]
    if erreurs:
        sys.exit("Reponses invalides :\n - " + "\n - ".join(erreurs))
    doc = lues()
    doc.update(nouveaux)
    ecrire(doc)
    print(f"relecture/perspectives_claude.json : {len(nouveaux)} ajoutee(s), {len(doc)} au total ; "
          f"lots restants : {len(restants())}.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--preparer", action="store_true")
    g.add_argument("--suivant", action="store_true")
    g.add_argument("--integrer", metavar="FICHIER")
    g.add_argument("--etat", action="store_true")
    a = p.parse_args()
    if a.preparer:
        preparer()
    elif a.suivant:
        r = restants()
        if r:
            print(r[0])
    elif a.integrer:
        integrer(a.integrer)
    else:
        print(f"{len(lots())} lots, {len(restants())} restant(s), {len(lues())} publications relues.")


if __name__ == "__main__":
    main()
