"""Publication de resultats du jour (onglet Actualites > Alertes) : chiffres
estimes par le consensus la veille, chiffres publies (avis de Claude), ecarts,
et probabilite de hausse du cours ajustee pas a pas.

Probabilite : on part de la part de hausse des publications passees de
l'entreprise de meme sens (bons, mauvais, mitiges), puis chaque element connu
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
LIBELLES_OBJECTIFS = {"superieurs": "Objectifs au-dessus du consensus", "conformes": "Objectifs conformes au consensus",
                      "inferieurs": "Objectifs sous le consensus"}


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
            "objectifs": croise(resultats.get("par_objectifs_sens")),
            "forte_hausse_12m": resultats.get("forte_hausse_12m"),
            "n": sum(n for _, n in par_sens.values())}


def _decalage(table, valeur, sens, base):
    """Ecart (log-cote) d'un element a sens egal, et nombre de cas."""
    p, n = ((table or {}).get(valeur) or {}).get(sens, (None, 0))
    if p is None or n < N_MIN or base is None:
        return None, n
    return _logit(p) - _logit(base), n


def probabilites(sens, p_base, perspectives, surprise, t_marche, t_perspectives=None, source_perspectives=None,
                 objectifs=None, perf_12m=None):
    """Etapes de la probabilite de hausse : [{libelle, p, n, source}] et
    probabilite finale. `t_perspectives` : tables d'un autre marche pour les
    perspectives quand le marche n'a pas encore d'historique (Etats-Unis)."""
    if sens not in LIBELLES_SENS or t_marche is None:
        return None
    base_sens = (t_marche["sens"].get(sens) or (None, 0))[0]
    p = p_base if p_base is not None else base_sens
    if p is None:
        return None
    etapes = [{"libelle": f"{LIBELLES_SENS[sens]} : ses publications passées"
               if p_base is not None else f"{LIBELLES_SENS[sens]} : toutes entreprises (pas d'historique propre)",
               "p": round(p, 3)}]
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
    non_comptes = []
    # Resultats seulement conformes au consensus apres une forte hausse sur 12
    # mois : le marche attendait mieux (historique du meme marche seulement).
    fh = t_marche.get("forte_hausse_12m")
    if surprise == "conforme" and perf_12m is not None:
        if not fh:
            non_comptes.append("Forte hausse sur 12 mois : historique encore trop court")
        elif perf_12m >= fh["seuil_pct"]:
            cel = fh["par_surprise"]["conforme"]
            n = cel["haut"]["n"]
            if n >= N_MIN and cel["haut"]["part_hausse"] is not None and cel["tous"]["part_hausse"]:
                x += _logit(cel["haut"]["part_hausse"]) - _logit(cel["tous"]["part_hausse"])
                etapes.append({"libelle": f"Conforme au consensus après forte hausse sur 12 mois "
                                          f"({perf_12m:+.0f} % face à l'indice)", "p": round(_sig(x), 3), "n": n})
            else:
                non_comptes.append(f"Forte hausse sur 12 mois : historique encore trop court ({n} cas)")
    if objectifs in LIBELLES_OBJECTIFS:
        d, n = _decalage(t_marche.get("objectifs"), objectifs, sens, base_sens)
        if d is not None:
            x += d
            etapes.append({"libelle": LIBELLES_OBJECTIFS[objectifs], "p": round(_sig(x), 3), "n": n})
        else:
            non_comptes.append(f"{LIBELLES_OBJECTIFS[objectifs]} : historique encore trop court ({n} cas)")
    res = {"etapes": etapes, "p_hausse": round(_sig(x), 3)}
    if non_comptes:
        res["non_comptes"] = non_comptes
    return res


def estimes(releve, periode, publie_le, surprises=None):
    """Consensus de la veille pour la periode publiee : trimestre (0q) ou
    exercice (0y), si sa date de fin precede la publication de moins de 120
    jours. {"fin", "ca": [moyenne en millions, analystes], "bpa": [...]}.
    BPA : celui que Yahoo garde dans son historique des publications
    (`surprises`, consensus au moment de la publication) prime sur le releve,
    qui peut etre reconstitue ou deja passe au trimestre suivant."""
    jour = publie_le[:10]
    histo = None
    for s in surprises or []:
        ecart = (dt.date.fromisoformat(jour) - dt.date.fromisoformat(s["trimestre"])).days
        if 0 <= ecart <= 120 and s.get("estime") is not None and (histo is None or s["trimestre"] > histo["trimestre"]):
            histo = s
    if not releve and not histo:
        return None
    if not releve:
        return {"fin": histo["trimestre"], "periode": "trimestre", "bpa": [histo["estime"], None]}
    cle = "0y" if (periode or "").lower().startswith(("annuel", "exercice")) or (periode or "").isdigit() else "0q"
    fin = (releve.get("fin") or {}).get(cle)
    if not fin or not (0 <= (dt.date.fromisoformat(jour) - dt.date.fromisoformat(fin)).days <= 120):
        return {"fin": histo["trimestre"], "periode": "trimestre", "bpa": [histo["estime"], None]} if histo else None
    res = {"fin": fin, "periode": "exercice" if cle == "0y" else "trimestre"}
    if (releve.get("ca") or {}).get(cle):
        m, n = releve["ca"][cle]
        res["ca"] = [round(m / 1e6, 1), n]
    if (releve.get("bpa") or {}).get(cle):
        res["bpa"] = releve["bpa"][cle]
    if histo and cle == "0q" and abs((dt.date.fromisoformat(fin) - dt.date.fromisoformat(histo["trimestre"])).days) <= 10:
        res["bpa"] = [histo["estime"], (res.get("bpa") or [None, None])[1]]
    return res if ("ca" in res or "bpa" in res) else None


def _ecart(publie, attendu):
    if publie is None or not attendu or not attendu[0]:
        return None
    return round((publie - attendu[0]) / abs(attendu[0]) * 100, 1)


def fiche(pub, avis, releve, p_base_par_sens, t_marche, t_perspectives=None, source_perspectives=None,
          surprises=None, perf_12m=None):
    """Publication du jour pour l'application : `pub` = {id, ticker, nom,
    publie_le, url}, `avis` = avis de Claude (ou None : jugement a venir),
    `releve` = consensus de la veille, `p_base_par_sens` = probabilite de
    hausse selon le sens des resultats pour l'entreprise."""
    res = dict(pub)
    if perf_12m is not None:
        res["perf_12m_pct"] = perf_12m
    est = estimes(releve, (avis or {}).get("periode"), pub["publie_le"], surprises)
    if est:
        res["estimes"] = est
    elif releve and (releve.get("bpa") or {}).get("0y"):
        # Pas de consensus pour la periode publiee (chiffre d'affaires de neuf
        # mois, semestre...) : celui de l'exercice, en repere.
        res["exercice"] = {"fin": (releve.get("fin") or {}).get("0y"), "bpa": releve["bpa"]["0y"],
                           **({"ca": [round(releve["ca"]["0y"][0] / 1e6, 1), releve["ca"]["0y"][1]]}
                              if (releve.get("ca") or {}).get("0y") else {})}
    if not avis or avis.get("type") not in ("resultats", "revision"):
        if avis and avis.get("type") == "autre":
            res["autre"] = True  # pas une publication de resultats (livraisons, calendrier...)
        return {k: v for k, v in res.items() if v is not None}
    ch = avis.get("chiffres") or {}
    res.update({k: avis.get(k) for k in ("periode", "sens", "activite", "rentabilite", "perspectives", "attentes",
                                         "consensus", "exceptionnel", "actionnaires", "objectifs", "resume",
                                         "points_cles", "a_surveiller", "impact")})
    res["juge"] = True
    if ch:
        res["chiffres"] = ch
    if est:
        res["ecarts"] = {k: v for k, v in {"ca_pct": _ecart(ch.get("ca"), est.get("ca")),
                                           "bpa_pct": _ecart(ch.get("bpa"), est.get("bpa"))}.items()
                         if v is not None}
    surprise = SURPRISES.get(avis.get("consensus"))
    ecart_bpa = (res.get("ecarts") or {}).get("bpa_pct")
    if ecart_bpa is not None:  # ecart chiffre au consensus : meme seuil que l'etude (5 %)
        surprise = "positive" if ecart_bpa > 5 else "negative" if ecart_bpa < -5 else "conforme"
    sens = avis.get("sens")
    pr = probabilites(sens, (p_base_par_sens or {}).get(sens), avis.get("perspectives"), surprise, t_marche,
                      t_perspectives, source_perspectives, (avis.get("objectifs") or {}).get("vs_consensus"),
                      perf_12m)
    if pr:
        res["probabilites"] = pr
    return {k: v for k, v in res.items() if v is not None and v is not False and v != {}}
