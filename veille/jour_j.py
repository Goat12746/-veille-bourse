"""Publication de resultats du jour (onglet Actualites > Alertes) : chiffres
estimes par le consensus la veille, chiffres publies (avis de Claude), ecarts,
et probabilite de hausse du cours ajustee pas a pas.

Les resultats ne sont juges que face aux attentes (consensus des analystes),
jamais face a l'an dernier. Probabilite : on part de la part de hausse des
publications passees de l'entreprise, puis chaque element connu le jour meme
la decale de l'ecart qu'il a montre dans l'historique du meme marche (en
log-cote) :
  - resultats au-dessus, conformes ou en dessous du consensus : face aux
    publications de son grand secteur si la position y compte au moins N_MIN
    cas, sinon face a tout le marche ;
  - perspectives relevees, confirmees, abaissees ou nouvelles, et objectifs
    face au consensus (a position face au consensus egale si l'historique le
    permet, sinon toutes positions confondues) ;
  - resultats seulement conformes apres une forte hausse sur 12 mois.
Un element n'est retenu que s'il compte au moins N_MIN publications dans
l'historique. Les elements sont supposes independants : c'est une
estimation, pas une certitude.

Chiffre d'affaires et BPA : publies (avis de Claude) et attendus (consensus
de la veille) ; quand l'un manque, celui que Google Finance releve pour la
publication (consensus_google.json, chiffre publie face a l'estimation des
analystes) le complete pour l'affichage et les ecarts, marque "google"
(chiffres.sources, estimes.<ca|bpa>_source) ; la position face au consensus
et la probabilite ne s'appuient que sur l'avis de Claude.

Utilise par etude_amf.py (France) et communiques_usa.py (Etats-Unis).
Bibliotheque standard uniquement.
"""

import datetime as dt
import json
import math
import os

ICI = os.path.dirname(os.path.abspath(__file__))
_GOOGLE = None

N_MIN = 20
SURPRISES = {"superieur": "positive", "conforme": "conforme", "inferieur": "negative"}
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
    "resultats" d'une etude : part de hausse de toutes les publications, par
    position face au consensus, par perspectives et par objectifs (en tout et
    par position)."""
    if not resultats:
        return None
    mc = resultats.get("matrice_consensus") or {}
    surprise = {s: (l.get("part_hausse"), l.get("n", 0)) for s, l in mc.items()}
    n_cons = sum(n for _, n in surprise.values())
    h_cons = sum((l.get("hausse") or {}).get("n", 0) for l in mc.values())

    def croise(lignes):
        return {x["valeur"]: dict({s: (v.get("part_hausse"), v.get("n", 0))
                                   for s, v in (x.get("par_surprise") or {}).items()},
                                  tous=(x.get("part_hausse"), x.get("n", 0)))
                for x in lignes or []}

    return {"tous": resultats.get("part_hausse"),
            "consensus": h_cons / n_cons if n_cons else None,
            "surprise": surprise,
            "perspectives": croise(resultats.get("par_perspectives_surprise")),
            "objectifs": croise(resultats.get("par_objectifs_surprise")),
            "secteurs": {g: _surprise(m) for g, m in (resultats.get("consensus_par_secteur") or {}).items()},
            "forte_hausse_12m": resultats.get("forte_hausse_12m")}


def _surprise(mc):
    """Part de hausse par position face au consensus et part de hausse de
    toutes les publications comparees ({position: (p, n)}, p_consensus)."""
    par = {s: (l.get("part_hausse"), l.get("n", 0)) for s, l in mc.items()}
    n = sum(x for _, x in par.values())
    h = sum((l.get("hausse") or {}).get("n", 0) for l in mc.values())
    return par, (h / n if n else None)


def _decalage(t, table, valeur, surprise):
    """Ecart (log-cote) d'un element : a position face au consensus egale si
    elle compte assez de cas, sinon toutes positions confondues. (ecart, n,
    croise) ; ecart None si l'historique est trop court."""
    ligne = (t.get(table) or {}).get(valeur) or {}
    if surprise:
        p, n = ligne.get(surprise, (None, 0))
        ref = (t["surprise"].get(surprise) or (None, 0))[0]
        if p is not None and n >= N_MIN and ref is not None:
            return _logit(p) - _logit(ref), n, True
    p, n = ligne.get("tous", (None, 0))
    if p is None or n < N_MIN or t.get("tous") is None:
        return None, n, False
    return _logit(p) - _logit(t["tous"]), n, False


def probabilites(p_base, perspectives, surprise, t_marche, objectifs=None, perf_12m=None, secteur=None):
    """Etapes de la probabilite de hausse : [{libelle, p, n}] et probabilite
    finale. `p_base` : {"p", "n"}, part de hausse des publications passees de
    l'entreprise (None : celle de toutes les entreprises du marche)."""
    if t_marche is None:
        return None
    if p_base is not None:
        p = p_base["p"]
        etapes = [{"libelle": f"Ses {p_base['n']} publications passées", "p": round(p, 3), "n": p_base["n"]}]
    elif t_marche.get("tous") is not None:
        p = t_marche["tous"]
        etapes = [{"libelle": "Toutes entreprises (pas d'historique propre)", "p": round(p, 3)}]
    else:
        return None
    x = _logit(p)
    non_comptes = []
    if surprise in LIBELLES_SURPRISE:
        # Face aux publications de son grand secteur, a defaut tout le marche.
        par_s, ref_s = (t_marche.get("secteurs") or {}).get(secteur) or ({}, None)
        p_s, n_s = par_s.get(surprise, (None, 0))
        if secteur and p_s is not None and n_s >= N_MIN and ref_s:
            x += _logit(p_s) - _logit(ref_s)
            etapes.append({"libelle": f"{LIBELLES_SURPRISE[surprise]} (secteur {secteur})", "p": round(_sig(x), 3),
                           "n": n_s})
        else:
            p_m, n = t_marche["surprise"].get(surprise) or (None, 0)
            if p_m is not None and n >= N_MIN and t_marche.get("consensus"):
                x += _logit(p_m) - _logit(t_marche["consensus"])
                trop_court = f"secteur {secteur} : {n_s} cas" if secteur else "secteur inconnu"
                etapes.append({"libelle": f"{LIBELLES_SURPRISE[surprise]} (tout le marché ; {trop_court})",
                               "p": round(_sig(x), 3), "n": n})
            else:
                non_comptes.append(f"{LIBELLES_SURPRISE[surprise]} : historique encore trop court ({n} cas)")
    else:
        non_comptes.append("Pas de consensus comparable : position face aux attentes inconnue")
    for table, valeur, libelles in (("perspectives", perspectives, LIBELLES_PERSPECTIVES),
                                    ("objectifs", objectifs, LIBELLES_OBJECTIFS)):
        if valeur not in libelles:
            continue
        d, n, croise = _decalage(t_marche, table, valeur, surprise if surprise in LIBELLES_SURPRISE else None)
        if d is not None:
            x += d
            etapes.append({"libelle": libelles[valeur] + ("" if croise or not surprise else " (toutes positions)"),
                           "p": round(_sig(x), 3), "n": n})
        else:
            non_comptes.append(f"{libelles[valeur]} : historique encore trop court ({n} cas)")
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


SEUIL_SURPRISE = 5.0  # % d'ecart du BPA au consensus (meme seuil que l'etude, etude_amf.py)


def position_consensus(publie, attendu):
    """Au-dessus (positive), conforme ou en dessous (negative) du consensus :
    ecart exact du chiffre publie a l'attendu, compare au seuil avant tout
    arrondi (5,03 % est au-dessus, meme affiche "+5,0 %"). None sans chiffre."""
    if publie is None or not attendu:
        return None
    e = (publie - attendu) / abs(attendu) * 100
    return "positive" if e > SEUIL_SURPRISE else "negative" if e < -SEUIL_SURPRISE else "conforme"


def _ecart(publie, attendu):
    if publie is None or not attendu or not attendu[0]:
        return None
    return round((publie - attendu[0]) / abs(attendu[0]) * 100, 2)


def rapport_google(ticker, publie_le):
    """Rapport Google Finance de la publication (date de 1 jour avant a 4
    jours apres la publication) : {"devise", "bpa", "bpa_estime", "ca",
    "ca_estime"} (ca en millions), ou None."""
    global _GOOGLE
    if _GOOGLE is None:
        try:
            with open(os.path.join(ICI, "consensus_google.json"), encoding="utf-8") as f:
                _GOOGLE = json.load(f).get("entreprises", {})
        except (OSError, ValueError):
            _GOOGLE = {}
    jour = dt.date.fromisoformat(publie_le[:10])
    for r in reversed((_GOOGLE.get(ticker) or {}).get("rapports") or []):
        if -1 <= (dt.date.fromisoformat(r[0]) - jour).days <= 4:
            return {"devise": r[2], "bpa": r[3], "bpa_estime": r[4],
                    "ca": round(r[5] / 1e6, 1) if r[5] else None, "ca_estime": round(r[6] / 1e6, 1) if r[6] else None}
    return None


def completer_google(ch, est, g):
    """Complete les chiffres publies (`ch`) et attendus (`est`) avec le
    rapport Google Finance `g` la ou ils manquent (chiffres de la meme
    devise seulement). Renvoie (ch, est), copies."""
    ch, est = dict(ch or {}), dict(est or {})
    if not g or (ch.get("devise") and g.get("devise") and ch["devise"] != g["devise"]):
        return ch, est or None
    sources = dict(ch.get("sources") or {})
    for k in ("ca", "bpa"):
        if ch.get(k) is None and g.get(k) is not None:
            ch[k] = g[k]
            sources[k] = "google"
        if not est.get(k) and g.get(k + "_estime") is not None:
            est[k] = [g[k + "_estime"], None]
            est[k + "_source"] = "google"
    if sources:
        ch["sources"] = sources
    if g.get("devise") and not ch.get("devise") and (ch.get("ca") is not None or ch.get("bpa") is not None):
        ch["devise"] = g["devise"]
    return ch, (est if ("ca" in est or "bpa" in est) else None)


def fiche(pub, avis, releve, p_base, t_marche, surprises=None, perf_12m=None, secteur=None):
    """Publication du jour pour l'application : `pub` = {id, ticker, nom,
    publie_le, url}, `avis` = avis de Claude (ou None : jugement a venir),
    `releve` = consensus de la veille, `p_base` = part de hausse des
    publications passees de l'entreprise ({"p", "n"})."""
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
    # Chiffre d'affaires et BPA publies et attendus : Google Finance comble
    # ce que l'avis et le consensus de la veille n'ont pas.
    bpa_avis = ch.get("bpa")  # la position face au consensus ne s'appuie que sur l'avis
    ch, est = completer_google(ch, est, rapport_google(pub["ticker"], pub["publie_le"]))
    if est:
        res["estimes"] = est
        res.pop("exercice", None)
    res.update({k: avis.get(k) for k in ("periode", "perspectives", "attentes", "consensus", "consensus_raison",
                                         "exceptionnel", "actionnaires", "objectifs", "resume", "points_cles",
                                         "a_surveiller", "impact")})
    res["juge"] = True
    if ch:
        res["chiffres"] = ch
    if est:
        res["ecarts"] = {k: v for k, v in {"ca_pct": _ecart(ch.get("ca"), est.get("ca")),
                                           "bpa_pct": _ecart(ch.get("bpa"), est.get("bpa"))}.items()
                         if v is not None}
    # Position face au consensus : jugement d'ensemble de Claude (BPA, chiffre
    # d'affaires, marges... ponderes selon ce qui compte le plus pour le
    # titre), a defaut l'ecart chiffre du seul BPA au seuil de 5 %.
    surprise = SURPRISES.get(avis.get("consensus"))
    if surprise:
        res["position_source"] = "claude"
    else:
        surprise = position_consensus(bpa_avis, ((est or {}).get("bpa") or [None])[0])
        if surprise:
            res["position_source"] = "bpa"
    if surprise:
        res["surprise"] = surprise
    pr = probabilites(p_base, avis.get("perspectives"), surprise, t_marche,
                      (avis.get("objectifs") or {}).get("vs_consensus"), perf_12m, secteur)
    if pr:
        res["probabilites"] = pr
    return {k: v for k, v in res.items() if v is not None and v is not False and v != {}}
