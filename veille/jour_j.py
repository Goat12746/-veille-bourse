"""Publication de resultats du jour (onglet Actualites > Alertes) : chiffres
estimes par le consensus la veille, chiffres publies (avis de Claude), ecarts,
et probabilite de hausse du cours ajustee pas a pas.

Probabilite : on part de la probabilite de hausse connaissant le sens des
resultats (bons, mauvais, mitiges), le cours des 20 seances avant et
l'habitude de l'entreprise (prevision de l'etude), puis chaque element connu
le jour meme la decale de l'ecart qu'il a montre dans l'historique, a sens
des resultats egal (en log-cote) :
  - perspectives relevees, confirmees, abaissees ou nouvelles ;
  - resultats au-dessus, conformes ou en dessous du consensus.
Un element n'est retenu que s'il compte au moins N_MIN publications de meme
sens dans l'historique. Les elements sont supposes independants : c'est une
estimation, pas une certitude.

Utilise par etude_amf.py (France) et communiques_usa.py (Etats-Unis).
Bibliotheque standard uniquement.
"""

import datetime as dt
import math

N_MIN = 20
SURPRISES = {"superieur": "positive", "conforme": "conforme", "inferieur": "negative"}
LIBELLES_SENS = {"positif": "Bons résultats", "negatif": "Mauvais résultats", "mitige": "Résultats mitigés"}
LIBELLES_PERSPECTIVES = {"relevees": "Perspectives relevées", "confirmees": "Perspectives confirmées",
                         "abaissees": "Perspectives abaissées", "nouvelles": "Nouvelles perspectives"}
LIBELLES_SURPRISE = {"positive": "Au-dessus du consensus", "conforme": "Conforme au consensus",
                     "negative": "Sous le consensus"}


def _logit(p):
    p = min(0.98, max(0.02, p))
    return math.log(p / (1 - p))


def _sig(x):
    return 1 / (1 + math.exp(-x))


def tables(resultats):
    """Tables de l'historique utiles a l'ajustement, tirees du bloc
    "resultats" d'une etude : part de hausse par sens, par perspectives x sens
    et par position face au consensus x sens."""
    if not resultats:
        return None
    par_sens = {s: (l.get("part_hausse"), l.get("n", 0)) for s, l in (resultats.get("matrice") or {}).items()}

    def croise(lignes):
        return {x["valeur"]: {s: (v.get("part_hausse"), v.get("n", 0)) for s, v in (x.get("par_sens") or {}).items()}
                for x in lignes or []}

    return {"sens": par_sens,
            "perspectives": croise(resultats.get("par_perspectives_sens")),
            "surprise": croise(((resultats.get("attentes_marche") or {}).get("consensus") or {}).get("par_surprise")),
            "n": sum(n for _, n in par_sens.values())}


def _decalage(table, valeur, sens, base):
    """Ecart (log-cote) d'un element a sens egal, et nombre de cas."""
    p, n = ((table or {}).get(valeur) or {}).get(sens, (None, 0))
    if p is None or n < N_MIN or base is None:
        return None, n
    return _logit(p) - _logit(base), n


def probabilites(sens, p_base, perspectives, surprise, t_marche, t_perspectives=None, source_perspectives=None):
    """Etapes de la probabilite de hausse : [{libelle, p, n, source}] et
    probabilite finale. `t_perspectives` : tables d'un autre marche pour les
    perspectives quand le marche n'a pas encore d'historique (Etats-Unis)."""
    if sens not in LIBELLES_SENS or t_marche is None:
        return None
    base_sens = (t_marche["sens"].get(sens) or (None, 0))[0]
    p = p_base if p_base is not None else base_sens
    if p is None:
        return None
    etapes = [{"libelle": f"{LIBELLES_SENS[sens]}, cours des 20 séances avant, habitude de l'entreprise"
               if p_base is not None else LIBELLES_SENS[sens], "p": round(p, 3)}]
    x = _logit(p)
    if perspectives in LIBELLES_PERSPECTIVES:
        t, src = t_marche, None
        d, n = _decalage(t["perspectives"], perspectives, sens, base_sens)
        if d is None and t_perspectives is not None:
            t, src = t_perspectives, source_perspectives
            d, n = _decalage(t["perspectives"], perspectives, sens, (t["sens"].get(sens) or (None, 0))[0])
        if d is not None:
            x += d
            etapes.append({"libelle": LIBELLES_PERSPECTIVES[perspectives], "p": round(_sig(x), 3), "n": n,
                           **({"source": src} if src else {})})
    if surprise in LIBELLES_SURPRISE:
        d, n = _decalage(t_marche["surprise"], surprise, sens, base_sens)
        if d is not None:
            x += d
            etapes.append({"libelle": LIBELLES_SURPRISE[surprise], "p": round(_sig(x), 3), "n": n})
    return {"etapes": etapes, "p_hausse": round(_sig(x), 3)}


def estimes(releve, periode, publie_le):
    """Consensus de la veille pour la periode publiee : trimestre (0q) ou
    exercice (0y), si sa date de fin precede la publication de moins de 120
    jours. {"fin", "ca": [moyenne en millions, analystes], "bpa": [...]}."""
    if not releve:
        return None
    cle = "0y" if (periode or "").lower().startswith(("annuel", "exercice")) or (periode or "").isdigit() else "0q"
    fin = (releve.get("fin") or {}).get(cle)
    jour = publie_le[:10]
    if not fin or not (0 <= (dt.date.fromisoformat(jour) - dt.date.fromisoformat(fin)).days <= 120):
        return None
    res = {"fin": fin, "periode": "exercice" if cle == "0y" else "trimestre"}
    if (releve.get("ca") or {}).get(cle):
        m, n = releve["ca"][cle]
        res["ca"] = [round(m / 1e6, 1), n]
    if (releve.get("bpa") or {}).get(cle):
        res["bpa"] = releve["bpa"][cle]
    return res if ("ca" in res or "bpa" in res) else None


def _ecart(publie, attendu):
    if publie is None or not attendu or not attendu[0]:
        return None
    return round((publie - attendu[0]) / abs(attendu[0]) * 100, 1)


def fiche(pub, avis, releve, p_base_par_sens, t_marche, t_perspectives=None, source_perspectives=None):
    """Publication du jour pour l'application : `pub` = {id, ticker, nom,
    publie_le, url}, `avis` = avis de Claude (ou None : jugement a venir),
    `releve` = consensus de la veille, `p_base_par_sens` = probabilite de
    hausse selon le sens des resultats pour l'entreprise."""
    res = dict(pub)
    est = estimes(releve, (avis or {}).get("periode"), pub["publie_le"])
    if est:
        res["estimes"] = est
    if not avis or avis.get("type") not in ("resultats", "revision"):
        if avis and avis.get("type") == "autre":
            res["autre"] = True  # pas une publication de resultats (livraisons, calendrier...)
        return {k: v for k, v in res.items() if v is not None}
    ch = avis.get("chiffres") or {}
    res.update({k: avis.get(k) for k in ("periode", "sens", "activite", "rentabilite", "perspectives", "attentes",
                                         "consensus", "exceptionnel", "actionnaires")})
    res["juge"] = True
    if ch:
        res["chiffres"] = ch
    if est:
        res["ecarts"] = {k: v for k, v in {"ca_pct": _ecart(ch.get("ca"), est.get("ca")),
                                           "bpa_pct": _ecart(ch.get("bpa"), est.get("bpa"))}.items()
                         if v is not None}
    surprise = SURPRISES.get(avis.get("consensus"))
    sens = avis.get("sens")
    pr = probabilites(sens, (p_base_par_sens or {}).get(sens), avis.get("perspectives"), surprise, t_marche,
                      t_perspectives, source_perspectives)
    if pr:
        res["probabilites"] = pr
    return {k: v for k, v in res.items() if v is not None and v is not False and v != {}}
