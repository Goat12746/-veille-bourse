#!/usr/bin/env python3
"""Etude des publications de resultats du STOXX Europe 600 hors marche francais
(onglet Statistiques > Communiques > STOXX 600) : meme methode que l'etude des
communiques AMF (etude_amf.py) et des resultats americains (etude_usa.py), avec
le STOXX 600 pour reference.

  - publications : resultats_stoxx.json (stoxx_collecte.py), jugees face au
    consensus des analystes quand Yahoo en donne l'historique (BPA estime avant
    la publication et BPA publie) ;
  - position face au consensus : au-dessus ou en dessous si le BPA publie
    s'ecarte de plus de 5 % du BPA attendu, conforme sinon (jour_j.SEUIL_SURPRISE) ;
  - reaction : ecart au STOXX 600 corrige du beta le jour de la reaction,
    premiere seance dont la cloture suit la publication. Quand Yahoo ne donne
    que la date (heure inconnue), on suppose une publication avant l'ouverture,
    sauf pour les entreprises dont les heures connues ou les mouvements de cours
    montrent une publication apres la cloture ;
  - perspectives : jugees par Claude a la lecture du communique ou de la presse
    (relecture_stoxx/perspectives_claude.json) ;
  - pas de positions vendeuses (declarations propres a l'AMF).

Ecrit communiques_stoxx.json, au format de l'etude AMF.

Usage : python etude_stoxx.py
Bibliotheque standard uniquement.
"""

import datetime as dt
import re
import json
import math
import os
import sys

from etude_amf import assembler, mesurer
import jour_j
from mesures import Calendrier, Serie, heure_paris, telecharger_cours

ICI = os.path.dirname(os.path.abspath(__file__))
ENTREE = os.path.join(ICI, "resultats_stoxx.json")
SORTIE = os.path.join(ICI, "communiques_stoxx.json")
PERSPECTIVES = os.path.join(ICI, "relecture_stoxx", "perspectives_claude.json")
INDICE = "^STOXX"
# Cloture des places (heure de Paris), a defaut 17 h 30.
CLOTURES = {"Norway": (16, 25), "Greece": (16, 20), "Poland": (17, 0), "Denmark": (17, 0)}
CLOTURE_DEFAUT = (17, 30)
PAYS = {"United Kingdom": "Royaume-Uni", "Germany": "Allemagne", "Switzerland": "Suisse", "Sweden": "Suède",
        "Italy": "Italie", "Netherlands": "Pays-Bas", "Spain": "Espagne", "Denmark": "Danemark", "Norway": "Norvège",
        "Finland": "Finlande", "Poland": "Pologne", "Belgium": "Belgique", "Greece": "Grèce", "Austria": "Autriche",
        "Ireland": "Irlande", "Portugal": "Portugal", "France": "France"}
SURPRISE_CODE = {"positive": "superieur", "conforme": "conforme", "negative": "inferieur"}


def lire_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def limite_cotation():
    """Derniere cloture connue : aujourd'hui apres 17 h 45 a Paris, hier sinon."""
    maintenant = heure_paris(dt.datetime.now(dt.timezone.utc).isoformat())
    jour = maintenant.date()
    if (maintenant.hour, maintenant.minute) < (17, 45):
        jour -= dt.timedelta(days=1)
    return jour.isoformat()


class CalendrierEurope(Calendrier):
    """Seances du STOXX 600, instants en heure de Paris."""

    def reaction(self, t, cloture=CLOTURE_DEFAUT):
        jour = t.date().isoformat()
        if jour in self.pos and (t.hour, t.minute) < cloture:
            return jour
        return self.suivante(jour)


def z_seance(serie, jour):
    """Ecart (en sigma) du cours a l'indice une seance donnee, d'apres le modele
    de marche estime avant : None sans cours."""
    if serie is None or jour not in serie.pos:
        return None
    s = serie.pos[jour]
    mod = serie.modele(s)
    if mod is None:
        return None
    return serie.ecart(mod, s, s) / mod[2]


def regimes(doc, cal, series):
    """Moment habituel de publication de chaque entreprise : "apres" (apres la
    cloture) ou "avant" (avant l'ouverture, ou pendant la seance). D'abord les
    heures connues (Investegate, Nasdaq Nordic, parfois Yahoo) ; sans assez
    d'heures, les mouvements de cours : si l'ecart a l'indice est nettement plus
    fort le lendemain de la date de publication que le jour meme, c'est que
    l'entreprise publie apres la cloture."""
    res = {}
    for ticker, fiche in doc["entreprises"].items():
        cloture = CLOTURES.get(fiche["pays"], CLOTURE_DEFAUT)
        connues = [p for p in fiche["publications"] if p.get("heure")]
        tard = sum(1 for p in connues if (lambda t: (t.hour, t.minute) >= cloture)(heure_paris(p["publie_le"])))
        if len(connues) >= 3:
            res[ticker] = ("apres" if tard * 2 > len(connues) else "avant", "heures")
            continue
        serie = series.get(ticker)
        somme_jour, lendemain, n = 0.0, 0.0, 0
        for p in fiche["publications"]:
            d = p["publie_le"][:10]
            if serie is None or d not in cal.pos:
                continue
            z0, z1 = z_seance(serie, d), z_seance(serie, cal.suivante(d) or "")
            if z0 is None or z1 is None:
                continue
            somme_jour += abs(z0)
            lendemain += abs(z1)
            n += 1
        if n >= 6 and lendemain > 1.5 * somme_jour:
            res[ticker] = ("apres", "cours")
        else:
            res[ticker] = ("avant", "cours" if n >= 6 else "defaut")
    return res


def instant_local(p, regime):
    """Instant de publication en heure de Paris (naif) ; sans heure connue,
    7 h ou 22 h selon l'habitude de l'entreprise."""
    if p.get("heure"):
        return heure_paris(p["publie_le"])
    d = dt.datetime.fromisoformat(p["publie_le"][:10])
    return d.replace(hour=22 if regime == "apres" else 7)


# Communiques qui ne sont pas les resultats de l'entreprise elle-meme :
# filiales cotees publiees sous le nom du groupe (Anglo American Platinum,
# Kumba Iron Ore), assemblees de porteurs d'obligations.
PAS_LES_RESULTATS = re.compile(r"anglo american platinum|kumba iron ore|bondholder", re.I)
DOUBLON_JOURS = 4  # jours calendaires : correction ou remplacement du meme communique
# BPA publie Yahoo verifie contre les communiques (R.-U. et nordiques) : exclu
# (publie ou attendu faux, periodes melangees) ou corrige.
BPA_VERIFIE = lire_json(os.path.join(ICI, "bpa_verifie_stoxx.json"), {}).get("publications", {})


def bpa(p):
    """(estime, publie) apres verification ; (None, None) si la publication est ecartee."""
    est, pub = p.get("bpa_estime"), p.get("bpa_publie")
    v = BPA_VERIFIE.get(p["id"])
    if v and v["verdict"] == "exclu":
        return None, None
    if v and v["verdict"] == "corrige":
        pub = v["publie_corrige"]
    return est, pub


def evenements(doc, cal, series, perspectives, reg):
    evts, en_attente, sans_cours = [], 0, 0
    for ticker, fiche in doc["entreprises"].items():
        cloture = CLOTURES.get(fiche["pays"], CLOTURE_DEFAUT)
        derniere = None
        for p in sorted(fiche["publications"], key=lambda x: x["publie_le"]):
            if PAS_LES_RESULTATS.search(p.get("titre") or ""):
                continue
            # Correction, remplacement ou rapport publie quelques jours apres :
            # la publication ne compte qu'une fois (la premiere).
            d = dt.date.fromisoformat(p["publie_le"][:10])
            if derniere is not None and (d - derniere).days <= DOUBLON_JOURS:
                continue
            derniere = d
            t = instant_local(p, reg[ticker][0])
            jour = cal.reaction(t, cloture)
            if jour is None:
                en_attente += 1
                continue
            ev = {"ticker": ticker, "nom": fiche["nom"], "jour": jour, "categorie": "resultats",
                  "periode": p.get("periode"), "titre": p.get("titre") or "Résultats", "id": p["id"],
                  "publie_le": t.isoformat(), "n_communiques": 1, "sens": None, "origine": "comptes"}
            if p["id"] in perspectives and perspectives[p["id"]]:
                ev["perspectives"] = perspectives[p["id"]]
            est, pub = bpa(p)
            position = jour_j.position_consensus(pub, est)
            if position:
                ev["consensus"] = SURPRISE_CODE[position]
                ev["surprise_bpa_pct"] = round((pub - est) / abs(est) * 100, 1)
            if mesurer(ev, series.get(ticker), t):
                evts.append(ev)
            else:
                sans_cours += 1
    jours = {}
    for ev in evts:
        jours.setdefault(ev["ticker"], set()).add(ev["jour"])
    return evts, jours, en_attente, sans_cours


def main():
    doc = lire_json(ENTREE, None)
    if doc is None:
        sys.exit("resultats_stoxx.json introuvable : lancer stoxx_collecte.py --fusion")
    perspectives = lire_json(PERSPECTIVES, {}).get("publications", {})
    limite = limite_cotation()
    debut = (dt.date.fromisoformat(doc["depuis"]) - dt.timedelta(days=420)).isoformat()
    print(f"Cours depuis {debut} (dernière clôture prise en compte : {limite})…", file=sys.stderr)
    indice = telecharger_cours(INDICE, debut, limite)
    cal = CalendrierEurope(sorted(indice))
    series, erreurs = {INDICE: Serie(indice, indice)}, {}
    tickers = sorted(doc["entreprises"])
    for i, t in enumerate(tickers, 1):
        try:
            series[t] = Serie(telecharger_cours(t, debut, limite), indice)
        except Exception as err:  # une action illisible n'arrete pas le calcul
            erreurs[t] = str(err)[:120]
        if i % 50 == 0:
            print(f"  cours {i}/{len(tickers)}", file=sys.stderr)
    if erreurs:
        print(f"Cours indisponibles : {erreurs}", file=sys.stderr)

    reg = regimes(doc, cal, series)
    evts, jours, en_attente, sans_cours = evenements(doc, cal, series, perspectives, reg)

    referentiel, consensus = [], {}
    aujourd_hui = dt.date.today().isoformat()
    for t in tickers:
        f = doc["entreprises"][t]
        etiquettes = [i for i in f["indices"] if i != "STOXX 600"]
        referentiel.append({"ticker": t, "nom": f["nom"], "secteur": f.get("secteur"),
                            "indice": " · ".join([PAYS.get(f["pays"], f["pays"])] + etiquettes)})
        surprises = []
        for p in f["publications"]:
            est, pub = bpa(p)
            if est is None or pub is None:
                continue
            veille = (dt.date.fromisoformat(p["publie_le"][:10]) - dt.timedelta(days=1)).isoformat()
            surprises.append({"trimestre": veille, "estime": est, "publie": pub,
                              "surprise_pct": round((pub - est) / abs(est) * 100, 1) if est else None})
        consensus[t] = {"releves": [], "surprises": surprises, "prochaine": f.get("prochaine")}
    calendrier = [{"type": "resultats", "date": f["prochaine"], "tickers": [t], "source": "Yahoo Finance",
                   "fiable": False}
                  for t, f in doc["entreprises"].items() if f.get("prochaine") and f["prochaine"] >= aujourd_hui]
    n_par_ticker = {t: len(f["publications"]) for t, f in doc["entreprises"].items()}
    histo = assembler(referentiel, evts, jours, en_attente, cal, series, doc["depuis"],
                      sum(n_par_ticker.values()), n_par_ticker, None, consensus, calendrier, aujourd_hui,
                      indice="STOXX 600")
    for e in histo["entreprises"]:
        for x in e["resultats"]["evenements"]:
            x.pop("origine", None)
    # Pas de releves quotidiens du consensus en Europe : les entreprises "suivies" sont celles dont
    # Yahoo donne l'historique du BPA estime.
    histo["resultats"]["attentes_marche"]["consensus"]["n_entreprises"] = sum(
        1 for f in consensus.values() if f["surprises"])
    histo["zone"] = "stoxx"
    histo["genere_le"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    histo["cours_indisponibles"] = sorted(erreurs)
    histo["sources"] = {"yahoo": sum(1 for f in doc["entreprises"].values() for p in f["publications"]
                                     if p["source"].startswith("yahoo")),
                        "heure_connue": sum(1 for f in doc["entreprises"].values() for p in f["publications"]
                                            if p.get("heure"))}
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(histo, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    r = histo["resultats"]
    apres = sum(1 for v in reg.values() if v[0] == "apres")
    print(f"communiques_stoxx.json : {histo['n_evenements']} publications mesurées ({histo['n_entreprises']} "
          f"entreprises) depuis {histo['depuis']}, dont {r['n_consensus']} comparées au consensus ; en attente de "
          f"cotation : {en_attente} ; sans cours : {sans_cours} ; entreprises publiant après la clôture : {apres} ; "
          f"{os.path.getsize(SORTIE) // 1024} Ko.")
    try:  # onglet Test : rendement avant resultats et sens de la reaction
        import etude_avant
        etude_avant.main()
    except Exception as e:  # ne bloque pas la publication des resultats
        print(f"test_avant.json indisponible : {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
