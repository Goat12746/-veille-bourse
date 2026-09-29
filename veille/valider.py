#!/usr/bin/env python3
"""Verifie le format de sortie/analyse.json (ecrit par Claude) et de
alertes.json (lu par l'application).

Usage : python valider.py [fichier]   (alertes.json par defaut)
Code de sortie 1 et liste des erreurs si le fichier est invalide.
"""

import json
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))

ETAPES = ["information", "rumeur", "annonce", "depot", "adopte_commission",
          "adopte_seance", "adopte_definitif", "rejete", "publie"]
SENS = ["positif", "negatif", "neutre", "incertain"]
AMPLEURS = ["faible", "moyenne", "forte", "inconnue"]
SOURCES = ["amf", "assemblee", "senat", "presse", "journal_officiel", "autre"]
TYPES_CALENDRIER = ["budget", "examen_texte", "resultats", "dividende", "indice", "autre"]
# Champs d'une alerte deja publiee que l'analyse peut mettre a jour.
CHAMPS_MISE_A_JOUR = {"etape", "probabilite", "texte", "article", "resume", "titre"}
# Retrait d'une alerte publiee des actualites : {"id", "retirer": true, "motif"}.
CHAMPS_RETRAIT = {"retirer", "motif"}


def tickers_connus():
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        return {e["ticker"] for e in json.load(f)["entreprises"]}


def _date(v):
    return isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-"


def _entreprises(alerte, lieu, tickers, erreurs):
    ents = alerte.get("entreprises")
    if not isinstance(ents, list) or not ents:
        erreurs.append(f"{lieu} : 'entreprises' doit etre une liste non vide")
        return
    for j, e in enumerate(ents):
        l = f"{lieu}.entreprises[{j}]"
        if e.get("ticker") not in tickers:
            erreurs.append(f"{l} : ticker inconnu du referentiel ({e.get('ticker')!r})")
        if e.get("sens") not in SENS:
            erreurs.append(f"{l} : sens invalide ({e.get('sens')!r}), attendu {SENS}")
        if e.get("ampleur") not in AMPLEURS:
            erreurs.append(f"{l} : ampleur invalide ({e.get('ampleur')!r}), attendu {AMPLEURS}")
        if not isinstance(e.get("justification"), str) or not e["justification"].strip():
            erreurs.append(f"{l} : justification manquante")


def _texte(alerte, lieu, erreurs):
    """Champs facultatifs : texte porteur de la mesure et article."""
    for champ in ("texte", "article"):
        v = alerte.get(champ)
        if v is not None and not isinstance(v, str):
            erreurs.append(f"{lieu} : '{champ}' doit etre une chaine")


def _commun(alerte, lieu, erreurs):
    if alerte.get("etape") not in ETAPES:
        erreurs.append(f"{lieu} : etape invalide ({alerte.get('etape')!r}), attendu {ETAPES}")
    p = alerte.get("probabilite")
    if not isinstance(p, (int, float)) or not 0 <= p <= 1:
        erreurs.append(f"{lieu} : probabilite doit etre un nombre entre 0 et 1")
    for champ in ("titre", "resume"):
        if not isinstance(alerte.get(champ), str) or not alerte[champ].strip():
            erreurs.append(f"{lieu} : '{champ}' manquant")


def _calendrier(evts, lieu, tickers, erreurs):
    for i, ev in enumerate(evts or []):
        l = f"{lieu}[{i}]"
        if not _date(ev.get("date")):
            erreurs.append(f"{l} : date invalide ({ev.get('date')!r})")
        if ev.get("type") not in TYPES_CALENDRIER:
            erreurs.append(f"{l} : type invalide ({ev.get('type')!r}), attendu {TYPES_CALENDRIER}")
        if not ev.get("titre"):
            erreurs.append(f"{l} : titre manquant")
        for t in ev.get("tickers") or []:
            if t not in tickers:
                erreurs.append(f"{l} : ticker inconnu ({t!r})")


def valider_analyse(doc, ids_candidats, tickers=None, ids_publies=None):
    """sortie/analyse.json : alertes rattachees aux candidats par leur id,
    mises a jour d'alertes deja publiees (par leur id)."""
    tickers = tickers or tickers_connus()
    erreurs = []
    if not isinstance(doc.get("alertes"), list):
        return ["'alertes' doit etre une liste"]
    for i, a in enumerate(doc["alertes"]):
        lieu = f"alertes[{i}] ({a.get('id')})"
        for cid in [a.get("id")] + list(a.get("ids_lies") or []):
            if cid not in ids_candidats:
                erreurs.append(f"{lieu} : id de candidat inconnu ({cid!r})")
        _commun(a, lieu, erreurs)
        _texte(a, lieu, erreurs)
        _entreprises(a, lieu, tickers, erreurs)
        if not isinstance(a.get("nouveaute"), str) or not a["nouveaute"].strip():
            erreurs.append(f"{lieu} : 'nouveaute' manquant (ce que le marche ignorait la veille ; "
                           "sans nouveaute, ecarter le candidat)")
    for i, m in enumerate(doc.get("mises_a_jour") or []):
        lieu = f"mises_a_jour[{i}] ({m.get('id')})"
        if ids_publies is not None and m.get("id") not in ids_publies:
            erreurs.append(f"{lieu} : aucune alerte publiee avec cet id")
        if "retirer" in m:
            if m["retirer"] is not True or not isinstance(m.get("motif"), str) or not m["motif"].strip():
                erreurs.append(f"{lieu} : retrait = {{\"id\", \"retirer\": true, \"motif\": \"...\"}}")
            if set(m) - CHAMPS_RETRAIT - {"id"}:
                erreurs.append(f"{lieu} : un retrait ne modifie pas d'autre champ")
            continue
        inconnus = set(m) - CHAMPS_MISE_A_JOUR - {"id"}
        if inconnus:
            erreurs.append(f"{lieu} : champs non modifiables {sorted(inconnus)}, autorises {sorted(CHAMPS_MISE_A_JOUR)}")
        if "etape" in m and m["etape"] not in ETAPES:
            erreurs.append(f"{lieu} : etape invalide ({m['etape']!r})")
        if "probabilite" in m and not (isinstance(m["probabilite"], (int, float)) and 0 <= m["probabilite"] <= 1):
            erreurs.append(f"{lieu} : probabilite doit etre un nombre entre 0 et 1")
        _texte(m, lieu, erreurs)
    for cid in doc.get("ecartes") or []:
        if cid not in ids_candidats:
            erreurs.append(f"ecartes : id de candidat inconnu ({cid!r})")
    _calendrier(doc.get("calendrier"), "calendrier", tickers, erreurs)
    return erreurs


def valider_alertes(doc, tickers=None):
    """alertes.json : fichier publie pour l'application."""
    tickers = tickers or tickers_connus()
    erreurs = []
    if doc.get("version") != 1:
        erreurs.append("version doit valoir 1")
    ids = set()
    for i, a in enumerate(doc.get("alertes") or []):
        lieu = f"alertes[{i}] ({a.get('id')})"
        if a.get("id") in ids:
            erreurs.append(f"{lieu} : id en double")
        ids.add(a.get("id"))
        if not _date(a.get("date")):
            erreurs.append(f"{lieu} : date invalide")
        if a.get("source") not in SOURCES:
            erreurs.append(f"{lieu} : source invalide ({a.get('source')!r})")
        if a.get("analyse") not in ("claude", "mots_cles"):
            erreurs.append(f"{lieu} : analyse doit valoir 'claude' ou 'mots_cles'")
        _commun(a, lieu, erreurs)
        _texte(a, lieu, erreurs)
        _entreprises(a, lieu, tickers, erreurs)
    _calendrier(doc.get("calendrier"), "calendrier", tickers, erreurs)
    return erreurs


def main():
    chemin = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ICI, "alertes.json")
    with open(chemin, encoding="utf-8") as f:
        doc = json.load(f)
    if os.path.basename(chemin) == "analyse.json":
        with open(os.path.join(ICI, "sortie", "candidats.json"), encoding="utf-8") as f:
            ids = {c["id"] for c in json.load(f)["candidats"]}
        # Alertes publiees (60 derniers jours) et archivees : toutes peuvent
        # etre mises a jour.
        ids_publies = None
        for nom in ("alertes.json", "historique.json"):
            chemin_publie = os.path.join(ICI, nom)
            if os.path.exists(chemin_publie):
                with open(chemin_publie, encoding="utf-8") as f:
                    ids_publies = (ids_publies or set()) | {a["id"] for a in json.load(f).get("alertes", [])}
        erreurs = valider_analyse(doc, ids, ids_publies=ids_publies)
    else:
        erreurs = valider_alertes(doc)
    if erreurs:
        print(f"{len(erreurs)} erreur(s) dans {chemin} :")
        for e in erreurs:
            print(" -", e)
        sys.exit(1)
    print(f"{chemin} : OK")


if __name__ == "__main__":
    main()
