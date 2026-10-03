#!/usr/bin/env python3
"""Etude des publications de resultats americaines (onglet Statistiques >
Communiques > Etats-Unis) : meme methode que l'etude des communiques AMF
(etude_amf.py), avec le S&P 500 pour reference et l'heure de New York.

  - publications : resultats_usa.json (edgar.py), sens d'apres les comptes
    deposes a la SEC ;
  - reaction : ecart au S&P 500 corrige du beta le jour de la reaction,
    premiere seance dont la cloture (16 h a New York) suit la publication ;
  - consensus des analystes : consensus_usa.json (consensus.py --zone usa),
    avec la prochaine date de resultats donnee par Yahoo ;
  - pas de positions vendeuses (declarations propres a l'AMF).

Ecrit communiques_usa.json, au format de l'etude AMF (historique_amf de
statistiques.json).

Usage : python etude_usa.py
Bibliotheque standard uniquement.
"""

import datetime as dt
import json
import os
import sys

import consensus as consensus_mod
import edgar
from etude_amf import assembler, mesurer
from mesures import Calendrier, Serie, telecharger_cours

ICI = os.path.dirname(os.path.abspath(__file__))
RECENTES = 14  # jours de publications listees pour l'onglet Actualites > Etats-Unis
SORTIE = os.path.join(ICI, "communiques_usa.json")
INDICE = "^GSPC"  # S&P 500
CLOTURE = (16, 0)  # heure de New York


class CalendrierNewYork(Calendrier):
    """Seances du S&P 500, instants en heure de New York."""

    def reaction(self, t):
        jour = t.date().isoformat()
        if jour in self.pos and (t.hour, t.minute) < CLOTURE:
            return jour
        return self.suivante(jour)


def limite_cotation():
    """Derniere cloture connue : aujourd'hui apres 16 h 15 a New York, hier
    sinon (la barre du jour est provisoire)."""
    utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    local = utc + dt.timedelta(hours=edgar.decalage_new_york(utc - dt.timedelta(hours=4)))
    jour = local.date()
    if (local.hour, local.minute) < (16, 15):
        jour -= dt.timedelta(days=1)
    return jour.isoformat()


def heure_locale(iso):
    """"2026-07-30T16:30:28-04:00" -> heure de New York sans fuseau."""
    return dt.datetime.fromisoformat(iso).replace(tzinfo=None)


def evenements(doc, referentiel, cal, series):
    """Publications mesurees, jours d'evenement par action, publications pas
    encore cotees."""
    nom_de = {e["ticker"]: e["nom"] for e in referentiel}
    # Avis de Claude sur les publications du jour (depuis octobre 2026) :
    # perspectives, objectifs face au consensus, position face au consensus.
    classes = {}
    chemin = os.path.join(ICI, "resultats_usa_classes.json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            classes = json.load(f).get("classes", {})
    evts, en_attente = [], 0
    for ticker, fiche in doc["entreprises"].items():
        for p in fiche["publications"]:
            t = heure_locale(p["publie_le"])
            jour = cal.reaction(t)
            if jour is None:
                en_attente += 1
                continue
            ev = {"ticker": ticker, "nom": nom_de.get(ticker, fiche["nom"]), "jour": jour,
                  "categorie": "resultats", "periode": p.get("periode"), "titre": p["titre"], "id": p["id"],
                  "publie_le": p["publie_le"], "n_communiques": 1, "sens": p.get("sens"), "origine": "comptes",
                  "activite": p.get("activite"), "rentabilite": p.get("rentabilite")}
            avis = classes.get(p["id"])
            if avis and avis.get("type") == "resultats":
                for k in ("perspectives", "attentes", "consensus", "exceptionnel", "actionnaires"):
                    if avis.get(k) is not None:
                        ev[k] = avis[k]
                ev["objectifs_vs"] = (avis.get("objectifs") or {}).get("vs_consensus")
                if not ev["sens"] and avis.get("sens"):  # comptes pas encore deposes
                    ev["sens"], ev["origine"] = avis["sens"], "claude"
            if mesurer(ev, series.get(ticker), t):
                evts.append(ev)
    jours = {}
    for ev in evts:
        jours.setdefault(ev["ticker"], set()).add(ev["jour"])
    return evts, jours, en_attente


def recentes(doc, evts, aujourd_hui):
    """Publications des derniers jours, mesurees ou non (Actualites >
    Etats-Unis) : heure, titre, sens s'il est connu, reaction si elle est
    cotee, ecart au consensus du BPA."""
    limite = (dt.date.fromisoformat(aujourd_hui) - dt.timedelta(days=RECENTES)).isoformat()
    mesures = {ev["id"]: ev for ev in evts}
    res = []
    for ticker, fiche in doc["entreprises"].items():
        for p in fiche["publications"]:
            if p["publie_le"][:10] < limite:
                continue
            x = {"ticker": ticker, "nom": fiche["nom"], "id": p["id"], "publie_le": p["publie_le"],
                 "titre": p["titre"], "url": p.get("url"), "sens": p.get("sens")}
            ev = mesures.get(p["id"])
            if ev:
                x.update({k: ev.get(k) for k in ("jour", "rendement_pct", "indice_pct", "ecart_pct", "z",
                                                 "surprise", "surprise_bpa_pct")})
            res.append({k: v for k, v in x.items() if v is not None})
    return sorted(res, key=lambda x: x["publie_le"], reverse=True)


def main():
    with open(edgar.SORTIE, encoding="utf-8") as f:
        doc = json.load(f)
    univers = {e["ticker"]: e for e in edgar.univers()}
    referentiel = []
    for t in sorted(doc["entreprises"]):
        e = univers.get(t, {"ticker": t, "nom": doc["entreprises"][t]["nom"]})
        # "S&P 500 · Nasdaq-100" pour une valeur des deux indices (filtre de
        # l'application par indice contenu).
        indices = [i for i in ("S&P 500", "Nasdaq-100") if i in (e.get("indices") or [])]
        referentiel.append({"ticker": t, "nom": e["nom"], "secteur": e.get("secteur"),
                            "indice": " · ".join(indices) or None})

    limite = limite_cotation()
    debut = (dt.date.fromisoformat(doc["depuis"]) - dt.timedelta(days=420)).isoformat()
    print(f"Cours depuis {debut} (dernière clôture prise en compte : {limite})…", file=sys.stderr)
    indice = telecharger_cours(INDICE, debut, limite)
    cal = CalendrierNewYork(sorted(indice))
    series, erreurs = {INDICE: Serie(indice, indice)}, {}
    for i, e in enumerate(referentiel, 1):
        try:
            series[e["ticker"]] = Serie(telecharger_cours(e["ticker"], debut, limite), indice)
        except Exception as err:  # une action illisible n'arrete pas le calcul
            erreurs[e["ticker"]] = str(err)[:120]
        if i % 100 == 0:
            print(f"  cours {i}/{len(referentiel)}", file=sys.stderr)
    if erreurs:
        print(f"Cours indisponibles : {erreurs}", file=sys.stderr)

    evts, jours, en_attente = evenements(doc, referentiel, cal, series)
    consensus = consensus_mod.charger(consensus_mod.SORTIE_USA).get("entreprises", {})
    aujourd_hui = dt.date.today().isoformat()
    calendrier = [{"type": "resultats", "date": f["prochaine"], "tickers": [t], "source": "Yahoo Finance",
                   "fiable": False}
                  for t, f in consensus.items() if f.get("prochaine") and f["prochaine"] >= aujourd_hui]
    n_par_ticker = {t: len(f["publications"]) for t, f in doc["entreprises"].items()}
    histo = assembler(referentiel, evts, jours, en_attente, cal, series, doc["depuis"],
                      sum(n_par_ticker.values()), n_par_ticker, None, consensus, calendrier, aujourd_hui,
                      indice="S&P 500")
    # Sens toujours tire des comptes : champ inutile dans chaque publication.
    # CIK : l'application reconnait les depots du jour dans le flux de la SEC.
    for e in histo["entreprises"]:
        e["cik"] = doc["entreprises"][e["ticker"]]["cik"]
        for x in e["resultats"]["evenements"]:
            x.pop("origine", None)
    histo["recentes"] = recentes(doc, evts, aujourd_hui)
    histo["zone"] = "usa"
    histo["genere_le"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    histo["cours_indisponibles"] = sorted(erreurs)
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(histo, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    r = histo["resultats"]
    print(f"communiques_usa.json : {histo['n_evenements']} publications mesurées ({histo['n_entreprises']} "
          f"entreprises) depuis {histo['depuis']}, dont {r['n'] - r['n_sans_sens']} avec sens ; "
          f"en attente de cotation : {en_attente} ; {os.path.getsize(SORTIE) // 1024} Ko.")


if __name__ == "__main__":
    main()
