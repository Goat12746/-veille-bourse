#!/usr/bin/env python3
"""Resultats publies depuis la derniere cloture : ce que l'historique dit de
la reaction du cours, pour une alerte avant l'ouverture (9 h).

Pour chaque publication de resultats (ou revision d'objectifs) d'une
entreprise du referentiel, deposee apres la derniere cloture (17 h 35) et deja
jugee (resultats_classes.json) :
  - part de hausse historique selon l'ecart au consensus et les perspectives
    (toutes entreprises, statistiques.json) ;
  - historique propre a l'entreprise : meme position face au consensus,
    habitude face au consensus, ampleur habituelle de ses reactions ;
  - suggestion : sens (positif si 60 % de hausse ou plus, negatif si 40 % ou
    moins, incertain sinon, d'apres le premier element connu parmi consensus,
    perspectives relevees ou abaissees, perspectives
    confirmees ou nouvelles) et ampleur
    (ecart median de ses reactions aux resultats : faible sous 2 %, forte
    au-dela de 5 %).

Ecrit sortie/avant_ouverture.json et l'affiche. Sans IA : la routine s'en sert
pour rediger les alertes (voir CONSIGNES.md).

Usage :
  python avant_ouverture.py
  python avant_ouverture.py --depuis 2026-07-22T17:35 --jusqu-a 2026-07-23T09:00   (essai sur le passe)
Bibliotheque standard uniquement.
"""

import argparse
import datetime as dt
import json
import os

from communiques import charger_classes, charger_communiques
from mesures import CLOTURE, heure_paris

ICI = os.path.dirname(os.path.abspath(__file__))
SURPRISES = {"superieur": "positive", "conforme": "conforme", "inferieur": "negative"}
N_MIN = 30  # publications minimum pour qu'une part de hausse historique serve de reference


def _charger(nom):
    with open(os.path.join(ICI, nom), encoding="utf-8") as f:
        return json.load(f)


def derniere_cloture(maintenant):
    """Derniere cloture (heure de Paris) avant `maintenant` (jours ouvres)."""
    j = maintenant.date()
    cloture = dt.datetime.combine(j, dt.time(*CLOTURE))
    if maintenant < cloture or j.weekday() >= 5:
        j -= dt.timedelta(days=1)
        while j.weekday() >= 5:
            j -= dt.timedelta(days=1)
        cloture = dt.datetime.combine(j, dt.time(*CLOTURE))
    return cloture


def _part(lignes, valeur):
    x = next((l for l in lignes if l["valeur"] == valeur), None)
    return (x["part_hausse"], x["n"]) if x and x.get("part_hausse") is not None else (None, 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--depuis", help="debut (heure de Paris, AAAA-MM-JJTHH:MM) ; par defaut la derniere cloture")
    p.add_argument("--jusqu-a", help="fin (heure de Paris) ; par defaut maintenant")
    args = p.parse_args()
    ref = {e["isin"]: e for e in _charger("referentiel.json")["entreprises"]}
    histo = (_charger("statistiques.json").get("historique_amf") or {})
    res = histo.get("resultats") or {}
    fiches = {e["ticker"]: e for e in histo.get("entreprises", [])}
    attentes = (res.get("attentes_marche") or {}).get("consensus") or {}
    classes = charger_classes()["classes"]
    maintenant = (dt.datetime.fromisoformat(args.jusqu_a) if args.jusqu_a
                  else heure_paris(dt.datetime.now(dt.timezone.utc).isoformat()))
    depuis = dt.datetime.fromisoformat(args.depuis) if args.depuis else derniere_cloture(maintenant)
    # Pendant la seance (9 h - 17 h 35), la reaction a deja commence : pas
    # d'alerte de resultats (veille de mi-seance).
    if not args.depuis and maintenant.weekday() < 5 and dt.time(9) <= maintenant.time() < dt.time(*CLOTURE):
        print("Séance en cours : la réaction aux résultats a déjà commencé, pas d'alerte de résultats "
              "(seulement avant 9 h).")
        return

    # Candidats de la collecte (sortie/candidats.json) : l'alerte porte l'id
    # d'un candidat. La collecte ne garde que la version francaise d'un
    # communique : a defaut du meme id, un candidat AMF de l'entreprise depuis
    # la cloture.
    try:
        candidats = _charger(os.path.join("sortie", "candidats.json")).get("candidats", [])
    except (OSError, ValueError):
        candidats = []
    vus, sortie = set(), []
    for c in sorted(charger_communiques()["communiques"], key=lambda c: c["publie_le"]):
        e = ref.get(c["isin"])
        a = classes.get(c["id"])
        if e is None or a is None or a["type"] not in ("resultats", "revision") or e["ticker"] in vus:
            continue
        if not depuis <= heure_paris(c["publie_le"]) <= maintenant:
            continue
        vus.add(e["ticker"])
        fiche = fiches.get(e["ticker"]) or {}
        sens, persp = a.get("sens"), a.get("perspectives")
        surprise = SURPRISES.get(a.get("consensus"))
        refs = []
        if surprise:
            p, n = _part(attentes.get("par_surprise", []), surprise)
            refs.append({"element": f"résultats {a['consensus']} au consensus", "part_hausse": p, "n": n})
        # Ordre de priorite : consensus, perspectives relevees ou abaissees,
        # puis perspectives confirmees ou nouvelles (peu informatives : autour
        # de 50 %). Les resultats ne sont juges que face aux attentes.
        if persp in ("relevees", "abaissees"):
            p, n = _part(res.get("par_perspectives", []), persp)
            refs.append({"element": f"perspectives {persp}", "part_hausse": p, "n": n})
        if a["type"] == "revision":
            r = histo.get("revisions") or {}
            p = r.get("hausse_si_positif") if sens == "positif" else (
                1 - r["baisse_si_negatif"] if sens == "negatif" and r.get("baisse_si_negatif") is not None else None)
            refs.append({"element": f"révision d'objectifs ({sens})", "part_hausse": p,
                         "n": r.get("n_positif" if sens == "positif" else "n_negatif", 0)})
        if persp in ("confirmees", "nouvelles"):
            p, n = _part(res.get("par_perspectives", []), persp)
            refs.append({"element": f"perspectives {persp}", "part_hausse": p, "n": n})
        propre = ((fiche.get("resultats") or {}).get("matrice_consensus") or {}).get(surprise) or {}
        mouv = (fiche.get("resultats") or {}).get("mouvements") or {}
        retenue = next((r for r in refs if r["part_hausse"] is not None and r["n"] >= N_MIN), None)
        p = retenue["part_hausse"] if retenue else None
        med = mouv.get("ecart_abs_median_pct")
        cand = next((x["id"] for x in candidats if x["id"] == "amf-" + c["id"]), None) or next(
            (x["id"] for x in candidats
             if x["source"] == "amf" and e["ticker"] in (x.get("entreprises_potentielles") or [])
             and heure_paris(x.get("publie_le") or x["date"]) >= depuis), None)
        sortie.append({
            "id": "amf-" + c["id"], "candidat": cand, "ticker": e["ticker"], "nom": e["nom"], "publie_le": c["publie_le"],
            "titre": c.get("entete") or c["titre"], "avis": {k: a.get(k) for k in (
                "type", "perspectives", "attentes", "consensus")}, **({"sens": sens} if a["type"] == "revision" else {}),
            "historique_ensemble": refs,
            "historique_entreprise": {
                "meme_position_consensus": {"n": propre.get("n", 0), "part_hausse": propre.get("part_hausse")},
                "habitude_consensus": (fiche.get("marche") or {}).get("habitude"),
                "ecart_median_reactions_pct": med,
            },
            "suggestion": {
                "sens": None if p is None else "positif" if p >= 0.6 else "negatif" if p <= 0.4 else "incertain",
                "part_hausse": p, "d_apres": retenue["element"] if retenue else None,
                "ampleur": None if med is None else "faible" if med < 2 else "forte" if med > 5 else "moyenne",
            },
        })

    os.makedirs(os.path.join(ICI, "sortie"), exist_ok=True)
    with open(os.path.join(ICI, "sortie", "avant_ouverture.json"), "w", encoding="utf-8") as f:
        json.dump({"depuis": depuis.isoformat(), "publications": sortie}, f, ensure_ascii=False, indent=1)
    print(f"sortie/avant_ouverture.json : {len(sortie)} publication(s) de résultats jugée(s) depuis la clôture "
          f"du {depuis:%d/%m à %H h %M}.")
    for x in sortie:
        s = x["suggestion"]
        print(f"- {x['nom']} (candidat {x['candidat'] or 'absent : pas d alerte possible'}) : perspectives {x['avis']['perspectives']}, "
              f"consensus {x['avis']['consensus']} -> "
              + (f"hausse {s['part_hausse'] * 100:.0f} % d'après {s['d_apres']}, suggestion {s['sens']}, "
                 f"ampleur {s['ampleur']}" if s["part_hausse"] is not None else "pas de référence historique"))


if __name__ == "__main__":
    main()
