"""Etude des communiques AMF (onglet Statistiques > Historique AMF) : reaction
des cours a chaque communique des entreprises du referentiel depuis 2019.

  - evenement = une entreprise et une seance de reaction (plusieurs
    communiques le meme jour ne comptent qu'une fois), de type celui du
    communique le plus important (revision d'objectifs, resultats...) ;
  - reaction : ecart au CAC 40 corrige du beta le jour de la reaction
    (hausse s'il est positif, baisse s'il est negatif), et en sigma ;
  - sens du communique : avis de Claude pour les publications de resultats et
    les revisions d'objectifs (resultats_classes.json), mots-cles sinon ;
  - publications de resultats : quatre scenarios (bons ou mauvais resultats,
    hausse ou baisse) et facteurs des scenarios contraires (perspectives,
    cours avant la publication, elements exceptionnels, retour aux
    actionnaires...) ;
  - prevision de la reaction a la prochaine publication d'apres le cours des
    20 seances precedentes, validee sur le passe : chaque publication depuis
    2022 est prevue avec les seules donnees anterieures.
"""

import datetime as dt
import math

from communiques import (CATEGORIES, LIBELLES, ORDRE, charger_classes, charger_communiques, generique, normaliser,
                         rapports_redondants)
import consensus as consensus_mod
import jour_j
import positions as positions_mod
from mesures import (SEUIL_FORT, SEUIL_NET, arrondi, heure_paris, mediane, moyenne, p_binomiale,
                     p_deux_proportions, pct)

PRIORITE = {cid: i for i, cid in enumerate(ORDRE)}
AVANT = 20  # seances avant la publication (le cours "recent")
SUITE = 5  # seances apres la reaction (la baisse ou la hausse se prolonge-t-elle ?)
UN_AN = 250  # seances de l'evolution sur 12 mois face a l'indice
QUANTILE_FORTE_HAUSSE = 0.8  # 20 % des publications ayant le plus monte sur 12 mois
SENS_RESULTATS = ("positif", "negatif", "mitige")
REACTIONS = ("hausse", "baisse")
DEBUT_VALIDATION = "2022-01-01"
FORCE_SENS = 6  # poids des frequences de l'ensemble face a l'historique de l'entreprise
FORCE_REACTION = 10
RECENTES = 14  # jours de publications listees pour l'onglet Actualites > Alertes
HORIZON_PROCHAINES = 60  # jours
# % d'ecart au consensus du BPA en deca duquel le resultat est conforme. Mesure
# sur 1 990 publications americaines (octobre 2026) : sous +5 %, battre le
# consensus ne fait pas monter le cours (46 a 48 % de hausses) ; au-dela, 56 a
# 58 % ; sous -5 %, 30 a 37 %. Les etudes citent +-2 %, moins discriminant ici.
SEUIL_SURPRISE = 5.0
SEUIL_REVISION = 1.0  # % d'evolution du consensus sur 30 jours en deca duquel il est stable
# Part du capital vendue a decouvert (positions publiques, >= 0,5 % chacune).
TRANCHES_COURTES = [("aucune", 0, 0.5), ("moderees", 0.5, 2), ("fortes", 2, 1e9)]
SURPRISES = {"superieur": "positive", "conforme": "conforme", "inferieur": "negative"}


# ---------------------------------------------------------------------------
# Evenements
# ---------------------------------------------------------------------------

def _categorie_effective(c, avis):
    """Type d'un communique : celui de l'avis de Claude s'il existe (il peut
    corriger une publication de resultats mal reperee), sinon les mots-cles."""
    if avis is None:
        return c["categorie"]
    if avis["type"] != "autre":
        return avis["type"]
    if c["categorie"] not in ("resultats", "revision"):
        return c["categorie"]
    # Pas des resultats d'apres Claude (calendrier, essai clinique, assemblee
    # generale, trafic...) : le type suivant d'apres les mots du titre.
    n = normaliser(_titre(c) + " " + (c.get("entete") or ""))
    return next((cid for cid, _, mots in CATEGORIES
                 if cid not in ("resultats", "revision") and any(m in n for m in mots)), "autre")


def _titre(c):
    return c["entete"] if generique(c["titre"]) and c.get("entete") else c["titre"]


def construire_evenements(communiques, classes, referentiel, cal, series):
    """Evenements mesures : (entreprise, seance de reaction), type, sens,
    reaction du jour, cours avant et apres. Retourne aussi les jours
    d'evenement par action (exclus de la base "jour ordinaire") et le
    nombre de publications pas encore cotees."""
    par_isin = {e["isin"]: e for e in referentiel}
    redondants = rapports_redondants(communiques, classes=classes)
    groupes, en_attente = {}, 0
    for c in communiques:
        e = par_isin.get(c["isin"])
        if e is None or c["id"] in redondants:
            continue
        t = heure_paris(c["publie_le"])
        jour = cal.reaction(t)
        if jour is None:
            en_attente += 1
            continue
        groupes.setdefault((e["ticker"], jour), []).append((t, c))

    evts = []
    for (ticker, jour), cs in groupes.items():
        cs.sort(key=lambda x: x[0])
        avis_evt = [classes[c["id"]] for _, c in cs if c["id"] in classes]
        cats = []
        for _, c in cs:
            a = classes.get(c["id"])
            if a is None and c["categorie"] in ("resultats", "revision"):
                # Autre communique du meme jour (rapport financier...) : il
                # suit l'avis porte sur le communique de presse de resultats,
                # pas un avis "autre" (document mis en ligne, calendrier...).
                a = next((x for x in avis_evt if x["type"] != "autre"), None)
            cats.append(_categorie_effective(c, a))
        cat = min(cats, key=PRIORITE.get)
        membres = [c for (_, c), k in zip(cs, cats) if k == cat]
        avis = next((classes[c["id"]] for _, c in cs if c["id"] in classes and classes[c["id"]]["type"] == cat),
                    None)
        if avis is None and cat in ("resultats", "revision"):
            avis = next((a for a in avis_evt if a["type"] == cat), None)
        # Un avis "autre" (document mis en ligne, calendrier...) n'a pas de
        # sens : celui des mots-cles reste.
        if avis is not None and avis["type"] in ("resultats", "revision"):
            sens, origine = avis.get("sens"), "claude"
        else:
            vus = {c["sens_mots"] for c in membres} - {None}
            sens, origine = (vus.pop() if len(vus) == 1 else None), "mots"
        principal = membres[0]
        ev = {"ticker": ticker, "nom": par_isin[principal["isin"]]["nom"], "jour": jour, "categorie": cat,
              "periode": next((c["periode"] for c in membres if c.get("periode")), None),
              "titre": _titre(principal), "id": principal["id"], "publie_le": cs[0][1]["publie_le"],
              "n_communiques": len(cs), "sens": sens, "origine": origine}
        if avis is not None and avis["type"] in ("resultats", "revision"):
            for k in ("activite", "rentabilite", "perspectives", "attentes", "consensus"):
                ev[k] = avis.get(k)
            ev["objectifs_vs"] = (avis.get("objectifs") or {}).get("vs_consensus")
            ev["exceptionnel"] = bool(avis.get("exceptionnel"))
            ev["actionnaires"] = bool(avis.get("actionnaires"))
        if not mesurer(ev, series.get(ticker), cs[0][0]):
            continue
        evts.append(ev)
    jours = {}
    for ev in evts:
        jours.setdefault(ev["ticker"], set()).add(ev["jour"])
    return evts, jours, en_attente


def mesurer(ev, serie, premier):
    """Reaction du jour, cours des 20 seances avant, suite sur 5 seances.
    False si les cours manquent."""
    if serie is None or ev["jour"] not in serie.pos:
        return False
    s = serie.pos[ev["jour"]]
    mod = serie.modele(s)
    if mod is None:
        return False
    m = serie.mesure(mod, s, s)
    ev.update({k: m[k] for k in ("rendement_pct", "indice_pct", "ecart_pct", "z", "sigma_pct")})
    ev["reaction"] = "hausse" if m["ecart_pct"] > 0 else "baisse" if m["ecart_pct"] < 0 else None
    # Cours avant : ecart cumule au CAC 40 sur les 20 seances precedentes,
    # d'apres un modele estime avant cette periode.
    mod_av = serie.modele(s - AVANT) if s - AVANT > 0 else None
    if mod_av is not None:
        car = serie.ecart(mod_av, s - AVANT, s - 1)
        ev["avant_pct"] = pct(car)
        ev["z_avant"] = round(car / (mod_av[2] * math.sqrt(AVANT)), 2)
    if s + SUITE < len(serie.dates):
        ev["suite_pct"] = pct(serie.ecart(mod, s + 1, s + SUITE))
    # Publication apres la cloture : reaction attendue le lendemain ; on
    # mesure aussi la seance de publication pour comparer.
    veille = premier.date().isoformat()
    if veille in serie.pos and veille != ev["jour"] and s >= 1:
        ev["z_veille"] = round(serie.ecart(mod, s - 1, s - 1) / mod[2], 2)
    perf = perf_12m(serie, s)
    if perf is not None:
        ev["perf_12m_pct"] = perf
    ev["_mod"] = (s, mod)
    return True


def enrichir(evts, positions, consensus):
    """Attentes du marche avant chaque publication de resultats ou revision :
    part du capital vendue a decouvert la veille (AMF), ecart du BPA publie au
    consensus (avis de Claude, sinon Yahoo pour les BPA trimestriels) et
    revision du consensus sur les 30 jours precedents."""
    par_ticker = {}
    for ev in evts:
        if ev["categorie"] in ("resultats", "revision"):
            par_ticker.setdefault(ev["ticker"], []).append(ev)
    for ticker, v in par_ticker.items():
        v.sort(key=lambda x: x["jour"])
        fiche = consensus.get(ticker)
        for ev in v:
            pub = ev["publie_le"][:10]
            if positions is not None:
                total, n = positions_mod.niveau((positions.get(ticker) or {}).get("points"), pub)
                ev["courtes_pct"], ev["courtes_n"] = total, n
                ev["courtes"] = next(t for t, a, b in TRANCHES_COURTES if a <= total < b)
            rev = consensus_mod.revision_pct(fiche, pub)
            if rev is not None:
                ev["revision_30j_pct"] = rev
                ev["revision"] = ("hausse" if rev > SEUIL_REVISION else "baisse" if rev < -SEUIL_REVISION
                                  else "stable")
            # Avis de Claude, a defaut de l'ecart chiffre de Yahoo (qui prime, ci-dessous).
            if ev.get("consensus") in SURPRISES:
                ev["surprise"] = SURPRISES[ev["consensus"]]
        # Surprises de BPA trimestriel (Yahoo) : premiere publication de
        # resultats dans les 110 jours qui suivent la fin du trimestre.
        for s in (fiche or {}).get("surprises", []):
            if s.get("surprise_pct") is None:
                continue
            fin = (dt.date.fromisoformat(s["trimestre"]) + dt.timedelta(days=110)).isoformat()
            ev = next((x for x in v if x["categorie"] == "resultats" and s["trimestre"] < x["jour"] <= fin), None)
            if ev is None:
                continue
            ev["surprise_bpa_pct"] = s["surprise_pct"]
            ev["surprise"] = ("positive" if s["surprise_pct"] > SEUIL_SURPRISE
                              else "negative" if s["surprise_pct"] < -SEUIL_SURPRISE else "conforme")


def croise(v, cle, valeurs):
    """Part de hausse selon la valeur d'un champ, en tout et par sens des
    resultats."""
    res = []
    for val in valeurs:
        w = [x for x in v if x.get(cle) == val and x["reaction"]]
        par_sens = {}
        for s in SENS_RESULTATS:
            u = [x for x in w if x["sens"] == s]
            par_sens[s] = {"n": len(u), "part_hausse": _part(sum(1 for x in u if x["reaction"] == "hausse"), len(u))}
        suites = [x["suite_pct"] for x in w if x.get("suite_pct") is not None]
        res.append({"valeur": val, "n": len(w),
                    "part_hausse": _part(sum(1 for x in w if x["reaction"] == "hausse"), len(w)),
                    "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in w]), 2) if w else None,
                    "suite_moyenne_pct": arrondi(moyenne(suites), 2) if suites else None,
                    "par_sens": par_sens})
    return res


def attentes_marche(avec_sens, consensus):
    """Resume des attentes du marche : positions vendeuses (historique AMF
    complet) et consensus des analystes (historique en construction)."""
    courtes = [x for x in avec_sens if x.get("courtes") is not None]
    surpr = [x for x in avec_sens if x.get("surprise")]
    revis = [x for x in avec_sens if x.get("revision")]
    concord = [x for x in surpr if x["surprise"] != "conforme" and x["reaction"]]
    ok = sum(1 for x in concord if (x["surprise"], x["reaction"]) in (("positive", "hausse"), ("negative", "baisse")))
    releves = [r["jour"] for f in consensus.values() for r in f.get("releves", []) if not r.get("reconstitue")]
    return {
        "positions_courtes": {"n": len(courtes),
                              "par_tranche": croise(courtes, "courtes", [t for t, _, _ in TRANCHES_COURTES])},
        "consensus": {
            "n_entreprises": sum(1 for f in consensus.values() if f.get("releves")),
            "releve_depuis": min(releves) if releves else None,
            "n_surprises": len(surpr),
            "par_surprise": croise(surpr, "surprise", ["positive", "conforme", "negative"]),
            "concordance": _part(ok, len(concord)),
            "p": arrondi(p_binomiale(ok, len(concord)), 4) if concord else None,
            "n_revisions": len(revis),
            "par_revision": croise(revis, "revision", ["hausse", "stable", "baisse"]),
        },
    }


def marche_actuel(ticker, positions, consensus, aujourd_hui, publications=()):
    """Attentes du marche aujourd'hui pour une entreprise (fiche), et son
    habitude face au consensus : ecart moyen de son BPA publie (tous les
    trimestres connus, mediane : un BPA attendu proche de zero donne des
    ecarts en % extremes) et nombre de publications au-dessus, conformes ou en
    dessous du consensus."""
    res = {}
    jugees = [x for x in publications if x.get("surprise")]
    bpa = [s["surprise_pct"] for s in (consensus.get(ticker) or {}).get("surprises", [])
           if s.get("surprise_pct") is not None]
    if jugees or bpa:
        res["habitude"] = {"n": len(jugees),
                           "n_positive": sum(1 for x in jugees if x["surprise"] == "positive"),
                           "n_conforme": sum(1 for x in jugees if x["surprise"] == "conforme"),
                           "n_negative": sum(1 for x in jugees if x["surprise"] == "negative"),
                           "n_bpa": len(bpa), "bpa_mediane_pct": arrondi(mediane(bpa), 1) if bpa else None}
    if positions is not None:
        pts = (positions.get(ticker) or {}).get("points")
        total, n = positions_mod.niveau(pts, "9999-12-31")
        il_y_a = (dt.date.fromisoformat(aujourd_hui) - dt.timedelta(days=30)).isoformat()
        avant, _ = positions_mod.niveau(pts, il_y_a)
        res["courtes"] = {"pct": total, "n": n, "variation_30j": round(total - avant, 2),
                          "au": pts[-1][0] if pts else None}
    fiche = consensus.get(ticker)
    dernier = fiche["releves"][-1] if fiche and fiche.get("releves") else None
    if dernier:
        res["consensus"] = {"au": dernier["jour"], "fin": dernier.get("fin", {}),
                            "bpa": dernier.get("bpa", {}), "ca": dernier.get("ca", {}),
                            "revision_30j_pct": consensus_mod.revision_pct(fiche, "9999-12-31"),
                            "revisions": fiche.get("revisions", {}),
                            "surprises": fiche.get("surprises", [])[-4:]}
    return res


def jour_ordinaire(evts, series, jours):
    """Seances ordinaires : periodes d'estimation hors jours de communique
    (chaque seance comptee une fois par action)."""
    base = {"n": 0, "abs": 0.0, "net": 0, "fort": 0}
    vus = set()
    for ev in evts:
        serie = series[ev["ticker"]]
        s, mod = ev["_mod"]
        debut = max(1, s - len(mod[3]) - 10)
        for i, res in enumerate(mod[3]):
            d = serie.dates[debut + i]
            if (ev["ticker"], d) in vus or d in jours[ev["ticker"]]:
                continue
            vus.add((ev["ticker"], d))
            z = abs(res) / mod[2]
            base["n"] += 1
            base["abs"] += z
            base["net"] += z >= SEUIL_NET
            base["fort"] += z >= SEUIL_FORT
    n = base["n"]
    return {"n": n, "z_abs_moyen": arrondi(base["abs"] / n, 2) if n else None,
            "part_net": arrondi(base["net"] / n) if n else None,
            "part_fort": arrondi(base["fort"] / n) if n else None}


# ---------------------------------------------------------------------------
# Resumes
# ---------------------------------------------------------------------------

def _part(k, n):
    return arrondi(k / n) if n else None


def mouvements(v):
    """Ampleur des reactions (sans le signe)."""
    if not v:
        return {"n": 0}
    zs = [abs(x["z"]) for x in v]
    return {"n": len(v), "z_abs_moyen": arrondi(moyenne(zs), 2),
            "part_net": _part(sum(1 for z in zs if z >= SEUIL_NET), len(v)),
            "part_fort": _part(sum(1 for z in zs if z >= SEUIL_FORT), len(v)),
            "ecart_abs_median_pct": arrondi(mediane([abs(x["ecart_pct"]) for x in v]), 2)}


def sens_reaction(v):
    """Communiques orientes : part des positifs suivis d'une hausse, des
    negatifs suivis d'une baisse, et test de concordance."""
    pos = [x for x in v if x["sens"] == "positif" and x["reaction"]]
    neg = [x for x in v if x["sens"] == "negatif" and x["reaction"]]
    h = sum(1 for x in pos if x["reaction"] == "hausse")
    b = sum(1 for x in neg if x["reaction"] == "baisse")
    return {"n_positif": len(pos), "n_negatif": len(neg),
            "hausse_si_positif": _part(h, len(pos)), "baisse_si_negatif": _part(b, len(neg)),
            "concordance": _part(h + b, len(pos) + len(neg)),
            "p": arrondi(p_binomiale(h + b, len(pos) + len(neg)), 4),
            "ecart_moyen_positif_pct": arrondi(moyenne([x["ecart_pct"] for x in pos]), 2),
            "ecart_moyen_negatif_pct": arrondi(moyenne([x["ecart_pct"] for x in neg]), 2)}


POSITIONS_CONSENSUS = ("positive", "conforme", "negative")


def matrice(v, cle="sens", valeurs=SENS_RESULTATS):
    """Scenarios : sens des resultats (ou position face au consensus, cle
    "surprise") x reaction, nombre, ecart moyen a l'indice sur les 20 seances
    avant, le jour de la reaction et les 5 seances apres."""
    res = {}
    for s in valeurs:
        ligne = {}
        for r in REACTIONS:
            w = [x for x in v if x.get(cle) == s and x["reaction"] == r]
            ligne[r] = {"n": len(w),
                        "avant_moyen_pct": arrondi(moyenne([x["avant_pct"] for x in w if "avant_pct" in x]), 2),
                        "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in w]), 2),
                        "suite_moyenne_pct": arrondi(moyenne([x["suite_pct"] for x in w if "suite_pct" in x]), 2)}
        n = ligne["hausse"]["n"] + ligne["baisse"]["n"]
        ligne["n"] = n
        ligne["part_hausse"] = _part(ligne["hausse"]["n"], n)
        res[s] = ligne
    return res


def _tiers(v):
    """Bornes des tiers du cours avant publication (z sur 20 seances)."""
    zs = sorted(x["z_avant"] for x in v if x.get("z_avant") is not None)
    if len(zs) < 30:
        return None
    return zs[len(zs) // 3], zs[2 * len(zs) // 3]


def tiers_de(z, bornes):
    if z is None or bornes is None:
        return None
    return "baisse" if z < bornes[0] else "hausse" if z > bornes[1] else "stable"


def par_cours_avant(v, bornes):
    """Cours des 20 seances avant x sens des resultats -> part de hausse."""
    lignes = []
    for t in ("baisse", "stable", "hausse"):
        w = [x for x in v if tiers_de(x.get("z_avant"), bornes) == t]
        ligne = {"tiers": t, "n": len(w), "avant_moyen_pct": arrondi(moyenne([x["avant_pct"] for x in w]), 2)}
        for s in SENS_RESULTATS:
            u = [x for x in w if x["sens"] == s and x["reaction"]]
            ligne[s] = {"n": len(u), "part_hausse": _part(sum(1 for x in u if x["reaction"] == "hausse"), len(u)),
                        "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in u]), 2)}
        lignes.append(ligne)
    return lignes


def par_valeur(v, cle, valeurs):
    """Part de hausse et ecart moyen selon la valeur d'un champ."""
    res = []
    for val in valeurs:
        w = [x for x in v if x.get(cle) == val and x["reaction"]]
        if w:
            res.append({"valeur": val, "n": len(w),
                        "part_hausse": _part(sum(1 for x in w if x["reaction"] == "hausse"), len(w)),
                        "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in w]), 2),
                        "z_abs_moyen": arrondi(moyenne([abs(x["z"]) for x in w]), 2)})
    return res


# Facteurs des scenarios contraires (bons resultats suivis d'une baisse,
# mauvais resultats suivis d'une hausse) : mecanismes decrits dans la
# litterature et mesurables ici.
FACTEURS = [
    ("perspectives_abaissees", "Perspectives abaissées", lambda x, b: x.get("perspectives") == "abaissees"),
    ("perspectives_relevees", "Perspectives relevées", lambda x, b: x.get("perspectives") == "relevees"),
    ("hausse_avant", "Forte hausse dans les 20 séances avant", lambda x, b: tiers_de(x.get("z_avant"), b) == "hausse"),
    ("baisse_avant", "Forte baisse dans les 20 séances avant", lambda x, b: tiers_de(x.get("z_avant"), b) == "baisse"),
    ("activite_baisse", "Chiffre d'affaires en baisse", lambda x, b: x.get("activite") == "-"),
    ("activite_hausse", "Chiffre d'affaires en hausse", lambda x, b: x.get("activite") == "+"),
    ("rentabilite_baisse", "Rentabilité en baisse", lambda x, b: x.get("rentabilite") == "-"),
    ("rentabilite_hausse", "Rentabilité en hausse", lambda x, b: x.get("rentabilite") == "+"),
    ("exceptionnel", "Éléments exceptionnels", lambda x, b: bool(x.get("exceptionnel"))),
    ("actionnaires", "Retour aux actionnaires annoncé", lambda x, b: bool(x.get("actionnaires"))),
    ("attentes_superieures", "Supérieurs aux attentes (dit par l'entreprise)",
     lambda x, b: x.get("attentes") == "superieures"),
    ("attentes_inferieures", "Inférieurs aux attentes (dit par l'entreprise)",
     lambda x, b: x.get("attentes") == "inferieures"),
    ("surprise_positive", "Au-dessus du consensus des analystes", lambda x, b: x.get("surprise") == "positive"),
    ("surprise_negative", "Sous le consensus des analystes", lambda x, b: x.get("surprise") == "negative"),
    ("revision_hausse", "Consensus relevé dans les 30 jours avant", lambda x, b: x.get("revision") == "hausse"),
    ("revision_baisse", "Consensus abaissé dans les 30 jours avant", lambda x, b: x.get("revision") == "baisse"),
    ("courtes_fortes", "Plus de 2 % du capital vendu à découvert", lambda x, b: x.get("courtes") == "fortes"),
    ("courtes_aucune", "Aucune vente à découvert déclarée", lambda x, b: x.get("courtes") == "aucune"),
]


def facteurs(v, sens, contraire, bornes):
    """Pour des resultats d'un sens donne : part de reactions contraires
    avec et sans chaque facteur."""
    w = [x for x in v if x["sens"] == sens and x["reaction"]]
    res = []
    for fid, libelle, f in FACTEURS:
        avec = [x for x in w if f(x, bornes)]
        sans = [x for x in w if not f(x, bornes)]
        if len(avec) < 5:
            continue
        k1 = sum(1 for x in avec if x["reaction"] == contraire)
        k2 = sum(1 for x in sans if x["reaction"] == contraire)
        res.append({"id": fid, "libelle": libelle, "n_avec": len(avec), "taux_avec": _part(k1, len(avec)),
                    "n_sans": len(sans), "taux_sans": _part(k2, len(sans)),
                    "p": arrondi(p_deux_proportions(k1, len(avec), k2, len(sans)), 4)})
    res.sort(key=lambda r: -((r["taux_avec"] or 0) - (r["taux_sans"] or 0)))
    return res


# ---------------------------------------------------------------------------
# Prevision : P(sens) x P(hausse | sens, cours avant)
# ---------------------------------------------------------------------------

def _resoudre(a, b):
    """Systeme lineaire (elimination de Gauss avec pivot partiel)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _sig(z):
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def logistique(xs, ys, l2=2.0):
    """Regression logistique (Newton-Raphson, penalite L2 hors constante)."""
    k = len(xs[0])
    b = [0.0] * k
    for _ in range(30):
        g = [0.0] * k
        h = [[0.0] * k for _ in range(k)]
        for x, y in zip(xs, ys):
            p = _sig(sum(bj * xj for bj, xj in zip(b, x)))
            w = p * (1 - p)
            for i in range(k):
                g[i] += (y - p) * x[i]
                for j in range(i, k):
                    h[i][j] += w * x[i] * x[j]
        for i in range(k):
            for j in range(i):
                h[i][j] = h[j][i]
        for i in range(1, k):
            g[i] -= l2 * b[i]
            h[i][i] += l2
        d = _resoudre(h, g)
        b = [bj + dj for bj, dj in zip(b, d)]
        if max(abs(x) for x in d) < 1e-7:
            break
    return b


def _zc(z):
    return max(-4.0, min(4.0, z))


def _variables(sens, z, avec_cours=True):
    pos, neg = float(sens == "positif"), float(sens == "negatif")
    zc = _zc(z) if avec_cours else 0.0
    return [1.0, pos, neg, zc, zc * pos, zc * neg]


class Modele:
    """Prevision estimee sur des publications passees (sens connu, cours
    avant connu, reaction connue)."""

    def __init__(self, v, avec_cours=True):
        self.avec_cours = avec_cours
        v = [x for x in v if x["sens"] in SENS_RESULTATS and x["reaction"] and x.get("z_avant") is not None]
        self.n = len(v)
        self.bornes = _tiers(v)
        # P(sens | tiers du cours avant), lissee.
        self.p_sens = {}
        for t in ("baisse", "stable", "hausse", None):
            w = [x for x in v if t is None or tiers_de(x["z_avant"], self.bornes) == t]
            self.p_sens[t] = {s: (sum(1 for x in w if x["sens"] == s) + 1) / (len(w) + 3) for s in SENS_RESULTATS}
        self.coef = logistique([_variables(x["sens"], x["z_avant"], avec_cours) for x in v],
                               [1 if x["reaction"] == "hausse" else 0 for x in v])
        # Ecart de chaque entreprise a la prevision commune (hausse plus ou
        # moins frequente que prevu), ramene vers zero quand elle a peu de
        # publications.
        residus = {}
        for x in v:
            r = residus.setdefault(x["ticker"], [0.0, 0])
            r[0] += (1 if x["reaction"] == "hausse" else 0) - self.p_hausse_commun(x["sens"], x["z_avant"])
            r[1] += 1
        self.decalage = {t: r[0] / (r[1] + FORCE_REACTION) for t, r in residus.items()}
        self.sens_entreprise = {}
        for x in v:
            self.sens_entreprise.setdefault(x["ticker"], []).append(x["sens"])
        self.frequent = max(((s, r) for s in SENS_RESULTATS for r in REACTIONS),
                            key=lambda sr: sum(1 for x in v if (x["sens"], x["reaction"]) == sr))
        self.hausse_frequente = sum(1 for x in v if x["reaction"] == "hausse") >= self.n / 2

    def p_hausse_commun(self, sens, z):
        return _sig(sum(b * x for b, x in zip(self.coef, _variables(sens, z, self.avec_cours))))

    def p_hausse(self, ticker, sens, z):
        return min(0.98, max(0.02, self.p_hausse_commun(sens, z) + self.decalage.get(ticker, 0.0)))

    def probas_sens(self, ticker, z):
        """P(sens) de la prochaine publication : frequences de l'entreprise
        ramenees vers celles de l'ensemble (au meme cours avant)."""
        base = self.p_sens[tiers_de(z, self.bornes) if self.avec_cours else None]
        hist = self.sens_entreprise.get(ticker, [])
        n = len(hist)
        return {s: (hist.count(s) + FORCE_SENS * base[s]) / (n + FORCE_SENS) for s in SENS_RESULTATS}

    def scenarios(self, ticker, z):
        ps = self.probas_sens(ticker, z)
        res = {}
        for s in SENS_RESULTATS:
            ph = self.p_hausse(ticker, s, z)
            res[f"{s}_hausse"] = ps[s] * ph
            res[f"{s}_baisse"] = ps[s] * (1 - ph)
        return res


def valider(v, debut=DEBUT_VALIDATION):
    """Chaque publication depuis `debut` prevue avec les seules publications
    anterieures (modele reestime chaque trimestre). Compare au scenario le plus
    frequent jusque-la et au modele sans le cours avant."""
    v = sorted((x for x in v if x["sens"] in SENS_RESULTATS and x["reaction"] and x.get("z_avant") is not None),
               key=lambda x: x["jour"])
    tests = [x for x in v if x["jour"] >= debut]
    if len(tests) < 50:
        return None
    trimestres = sorted({(x["jour"][:4], (int(x["jour"][5:7]) - 1) // 3) for x in tests})
    stats = {"n": 0, "scenario": 0, "scenario_base": 0, "reaction": 0, "reaction_base": 0,
             "reaction_connue": 0, "reaction_connue_sans_cours": 0, "reaction_connue_base": 0,
             "brier": 0.0, "brier_sans_cours": 0.0, "brier_base": 0.0}
    calib = [[0.0, 0, 0] for _ in range(5)]
    for annee, tri in trimestres:
        debut_t = f"{annee}-{3 * tri + 1:02d}-01"
        appr = [x for x in v if x["jour"] < debut_t]
        if len(appr) < 200:
            continue
        m, m0 = Modele(appr), Modele(appr, avec_cours=False)
        base_h = sum(1 for x in appr if x["reaction"] == "hausse") / len(appr)
        base_sens = {s: (sum(1 for x in appr if x["sens"] == s and x["reaction"] == "hausse") + 1)
                     / (sum(1 for x in appr if x["sens"] == s) + 2) for s in SENS_RESULTATS}
        for x in tests:
            if (x["jour"][:4], (int(x["jour"][5:7]) - 1) // 3) != (annee, tri):
                continue
            y = 1 if x["reaction"] == "hausse" else 0
            sc = m.scenarios(x["ticker"], x["z_avant"])
            prevu = max(sc, key=sc.get)
            stats["n"] += 1
            stats["scenario"] += prevu == f"{x['sens']}_{x['reaction']}"
            stats["scenario_base"] += (x["sens"], x["reaction"]) == m.frequent
            ph_total = sum(p for k, p in sc.items() if k.endswith("_hausse"))
            stats["reaction"] += (ph_total >= 0.5) == bool(y)
            stats["reaction_base"] += (base_h >= 0.5) == bool(y)
            # Resultats connus : la reaction suit-elle la prevision ?
            ph = m.p_hausse(x["ticker"], x["sens"], x["z_avant"])
            ph0 = m0.p_hausse(x["ticker"], x["sens"], x["z_avant"])
            pb = base_sens[x["sens"]]
            stats["reaction_connue"] += (ph >= 0.5) == bool(y)
            stats["reaction_connue_sans_cours"] += (ph0 >= 0.5) == bool(y)
            stats["reaction_connue_base"] += (pb >= 0.5) == bool(y)
            stats["brier"] += (ph - y) ** 2
            stats["brier_sans_cours"] += (ph0 - y) ** 2
            stats["brier_base"] += (pb - y) ** 2
            c = calib[min(4, int(ph * 5))]
            c[0] += ph
            c[1] += y
            c[2] += 1
    n = stats["n"]
    if not n:
        return None
    return {
        "depuis": debut, "n": n,
        "scenario": _part(stats["scenario"], n), "scenario_base": _part(stats["scenario_base"], n),
        "reaction": _part(stats["reaction"], n), "reaction_base": _part(stats["reaction_base"], n),
        "reaction_connue": _part(stats["reaction_connue"], n),
        "reaction_connue_sans_cours": _part(stats["reaction_connue_sans_cours"], n),
        "reaction_connue_base": _part(stats["reaction_connue_base"], n),
        "brier": arrondi(stats["brier"] / n), "brier_sans_cours": arrondi(stats["brier_sans_cours"] / n),
        "brier_base": arrondi(stats["brier_base"] / n),
        "calibration": [{"prevu": arrondi(c[0] / c[2]), "observe": arrondi(c[1] / c[2]), "n": c[2]}
                        for c in calib if c[2]],
    }


def prevision_entreprise(hist):
    """Scenarios de la prochaine publication d'apres le seul passe de
    l'entreprise (pas les autres entreprises) : P(sens) = frequence de chaque
    sens dans ses publications, P(hausse | sens) = part de ses publications
    de ce sens suivies d'une hausse. Lissage minimal (un cas fictif par issue)
    pour ne jamais afficher 0 % ou 100 % sur quelques publications. None sans
    publication jugee."""
    v = [x for x in hist if x["sens"] in SENS_RESULTATS and x["reaction"]]
    if not v:
        return None
    n = len(v)
    p_sens = {s: (sum(1 for x in v if x["sens"] == s) + 1) / (n + len(SENS_RESULTATS)) for s in SENS_RESULTATS}
    p_hausse_si = {}
    for s in SENS_RESULTATS:
        w = [x for x in v if x["sens"] == s]
        p_hausse_si[s] = (sum(1 for x in w if x["reaction"] == "hausse") + 1) / (len(w) + 2)
    sc = {}
    for s in SENS_RESULTATS:
        sc[f"{s}_hausse"] = p_sens[s] * p_hausse_si[s]
        sc[f"{s}_baisse"] = p_sens[s] * (1 - p_hausse_si[s])
    return {"n": n, "scenarios": sc, "p_sens": p_sens, "p_hausse_si": p_hausse_si,
            "n_par_sens": {s: sum(1 for x in v if x["sens"] == s) for s in SENS_RESULTATS}}


def perf_12m(serie, s):
    """Evolution de l'action face a l'indice sur les 12 mois (250 seances)
    precedant la seance s, en points de %. None sans assez d'historique."""
    if serie is None or s - 1 - UN_AN < 0:
        return None
    a = serie.clo[s - 1] / serie.clo[s - 1 - UN_AN] - 1
    m = serie.clo_m[s - 1] / serie.clo_m[s - 1 - UN_AN] - 1
    return pct(a - m)


def table_forte_hausse(res):
    """Resultats face au consensus apres une forte hausse sur 12 mois (20 %
    des publications ayant le plus monte face a l'indice) : part de hausse le
    jour de la reaction, pour ces publications et pour toutes. Mesure sur 14 890
    publications americaines (octobre 2026) : resultats conformes au consensus
    apres une forte annee, 34 % de hausses contre 47 % en general."""
    v = [x for x in res if x.get("perf_12m_pct") is not None and x.get("surprise") and x["reaction"]]
    if len(v) < 50:
        return None
    valeurs = sorted(x["perf_12m_pct"] for x in v)
    seuil = valeurs[int(len(valeurs) * QUANTILE_FORTE_HAUSSE)]
    res_t = {"seuil_pct": arrondi(seuil, 1), "par_surprise": {}}
    for s in ("positive", "conforme", "negative"):
        w = [x for x in v if x["surprise"] == s]
        haut = [x for x in w if x["perf_12m_pct"] >= seuil]
        res_t["par_surprise"][s] = {
            "tous": {"n": len(w), "part_hausse": _part(sum(1 for x in w if x["reaction"] == "hausse"), len(w))},
            "haut": {"n": len(haut), "part_hausse": _part(sum(1 for x in haut if x["reaction"] == "hausse"),
                                                           len(haut))}}
    return res_t


def cours_recent(serie):
    """Ecart cumule a l'indice des 20 dernieres seances (en % et en sigma),
    et evolution face a l'indice sur 12 mois."""
    s = len(serie.dates)
    mod = serie.modele(s - AVANT) if s - AVANT > 0 else None
    if mod is None:
        return None
    car = serie.ecart(mod, s - AVANT, s - 1)
    res = {"au": serie.dates[-1], "avant_pct": pct(car), "z_avant": round(car / (mod[2] * math.sqrt(AVANT)), 2)}
    perf = perf_12m(serie, s)
    if perf is not None:
        res["perf_12m_pct"] = perf
    return res


def prochaines_dates(referentiel, resultats, calendrier, aujourd_hui):
    """Prochaine publication de resultats par action : date Yahoo (a
    confirmer), sinon meme periode que l'an dernier (estimee)."""
    res = {}
    for ev in calendrier or []:
        if ev.get("type") != "resultats" or ev["date"] < aujourd_hui:
            continue
        for t in ev.get("tickers") or []:
            if t not in res or ev["date"] < res[t]["date"]:
                res[t] = {"date": ev["date"], "source": ev.get("source") or "Yahoo Finance",
                          "estimee": not ev.get("fiable", False)}
    j = dt.date.fromisoformat(aujourd_hui)
    recent = (j - dt.timedelta(days=45)).isoformat()
    for e in referentiel:
        passees = sorted({x["jour"] for x in resultats if x["ticker"] == e["ticker"]})
        if passees and passees[-1] >= recent:
            continue  # vient de publier : la date de l'an dernier ne vaut plus
        for d in passees:
            prevue = dt.date.fromisoformat(d) + dt.timedelta(days=364)
            if j <= prevue <= j + dt.timedelta(days=HORIZON_PROCHAINES + 60):
                if e["ticker"] not in res:
                    res[e["ticker"]] = {"date": prevue.isoformat(), "source": "même période l'an dernier",
                                        "estimee": True}
                break
    return res


# ---------------------------------------------------------------------------
# Etude complete
# ---------------------------------------------------------------------------

def _evenement_public(x):
    """Publication de resultats telle qu'affichee dans l'application."""
    cles = ["jour", "periode", "titre", "sens", "origine", "activite", "rentabilite", "perspectives", "attentes",
            "exceptionnel", "actionnaires", "avant_pct", "z_avant", "perf_12m_pct", "rendement_pct", "ecart_pct", "z",
            "suite_pct",
            "reaction", "courtes_pct", "surprise", "surprise_bpa_pct", "revision_30j_pct", "objectifs_vs"]
    # Sans les valeurs nulles ni fausses (l'application les lit par defaut) :
    # le fichier reste leger malgre des milliers de publications.
    ev = {k: x[k] for k in cles if x.get(k) is not None and x.get(k) is not False}
    if len(ev.get("titre", "")) > 140:
        ev["titre"] = ev["titre"][:139].rstrip() + "…"
    return ev


def etude(referentiel, cal, series, depuis, calendrier=None, aujourd_hui=None):
    doc = charger_communiques()
    if doc is None:
        raise RuntimeError("communiques.json introuvable : lancer communiques.py")
    communiques = [c for c in doc["communiques"] if c["publie_le"] >= depuis]
    classes = charger_classes()["classes"]
    evts, jours, en_attente = construire_evenements(communiques, classes, referentiel, cal, series)
    pos_doc = positions_mod.charger()
    positions = pos_doc.get("entreprises") if pos_doc else None
    consensus = consensus_mod.charger().get("entreprises", {})
    isin_de = {e["isin"]: e["ticker"] for e in referentiel}
    n_par_ticker = {}
    for c in communiques:
        t = isin_de.get(c["isin"])
        n_par_ticker[t] = n_par_ticker.get(t, 0) + 1
    return assembler(referentiel, evts, jours, en_attente, cal, series, depuis, len(communiques), n_par_ticker,
                     positions, consensus, calendrier, aujourd_hui, indice="CAC 40",
                     recentes=publications_recentes(communiques, classes, referentiel, aujourd_hui))


def publications_recentes(communiques, classes, referentiel, aujourd_hui=None):
    """Publications de resultats (ou revisions) jugees par Claude depuis
    RECENTES jours : une par entreprise et par jour, celle qui porte l'avis."""
    aujourd_hui = aujourd_hui or dt.date.today().isoformat()
    limite = (dt.date.fromisoformat(aujourd_hui) - dt.timedelta(days=RECENTES)).isoformat()
    noms = {e["isin"]: (e["ticker"], e["nom"]) for e in referentiel}
    vues, res = set(), []
    for c in sorted(communiques, key=lambda c: c["publie_le"]):
        a = classes.get(c["id"])
        if c["publie_le"][:10] < limite or c["isin"] not in noms or not a or a.get("type") not in (
                "resultats", "revision"):
            continue
        cle = (c["isin"], c["publie_le"][:10])
        if cle in vues:
            continue
        vues.add(cle)
        ticker, nom = noms[c["isin"]]
        res.append(({"id": c["id"], "ticker": ticker, "nom": nom, "publie_le": c["publie_le"], "url": c["url"],
                     "titre": _titre(c), "type_periode": c.get("periode")}, a))
    return res


def assembler(referentiel, evts, jours, en_attente, cal, series, depuis, n_communiques, n_par_ticker,
              positions, consensus, calendrier=None, aujourd_hui=None, indice="CAC 40", recentes=None):
    """Etude a partir des evenements mesures (communiques AMF, ou publications
    de resultats americaines : etude_usa.py) : resumes, entreprises,
    prochaines publications."""
    aujourd_hui = aujourd_hui or dt.date.today().isoformat()
    enrichir(evts, positions, consensus)
    base = jour_ordinaire(evts, series, jours)

    # Types de communiques.
    categories = []
    for cid in ORDRE:
        v = [x for x in evts if x["categorie"] == cid]
        if v:
            categories.append(dict(mouvements(v), **sens_reaction(v), id=cid, libelle=LIBELLES[cid]))
    categories.sort(key=lambda c: -(c["part_fort"] or 0))

    # Publications de resultats.
    res = [x for x in evts if x["categorie"] == "resultats"]
    bornes = _tiers([x for x in res if x["sens"] in SENS_RESULTATS])
    avec_sens = [x for x in res if x["sens"] in SENS_RESULTATS]
    resultats = {
        "n": len(res), "n_avis_claude": sum(1 for x in res if x["origine"] == "claude"),
        "n_sans_sens": sum(1 for x in res if x["sens"] not in SENS_RESULTATS),
        "mouvements": mouvements(res),
        "matrice": matrice(avec_sens),
        # Scenarios face au consensus des analystes (historique du meme marche).
        "matrice_consensus": matrice([x for x in res if x.get("surprise")], "surprise", POSITIONS_CONSENSUS),
        "concordance": sens_reaction(avec_sens),
        "tiers_avant": {"bas": bornes[0], "haut": bornes[1]} if bornes else None,
        "par_cours_avant": par_cours_avant(avec_sens, bornes),
        "par_periode": par_valeur(avec_sens, "periode",
                                  ["annuels", "semestriels", "trimestriels", "chiffre_affaires", "autres"]),
        "par_perspectives": par_valeur(avec_sens, "perspectives", ["relevees", "confirmees", "nouvelles",
                                                                    "abaissees"]),
        # Part de hausse par perspectives et sens des resultats (ajustement
        # des probabilites le jour de la publication, jour_j.py).
        "par_perspectives_sens": croise(avec_sens, "perspectives", ["relevees", "confirmees", "nouvelles",
                                                                   "abaissees"]),
        "par_objectifs_sens": croise(avec_sens, "objectifs_vs", ["superieurs", "conformes", "inferieurs"]),
        "bons_puis_baisse": facteurs(avec_sens, "positif", "baisse", bornes),
        "mauvais_puis_hausse": facteurs(avec_sens, "negatif", "hausse", bornes),
        "attentes_marche": attentes_marche(avec_sens, consensus),
        "forte_hausse_12m": table_forte_hausse(res),
    }
    modele = Modele(avec_sens) if len(avec_sens) >= 200 else None
    if modele is not None:
        resultats["prevision"] = {
            "coefficients": [arrondi(b, 3) for b in modele.coef],
            "validation": valider(avec_sens),
        }

    # Revisions d'objectifs (hors publication de resultats).
    rev = [x for x in evts if x["categorie"] == "revision"]
    revisions = dict(mouvements(rev), **sens_reaction(rev))

    # Par entreprise.
    dates = prochaines_dates(referentiel, res, calendrier, aujourd_hui)
    entreprises, prochaines = [], []
    for e in referentiel:
        v = [x for x in evts if x["ticker"] == e["ticker"]]
        n_comm = n_par_ticker.get(e["ticker"], 0)
        r = sorted((x for x in v if x["categorie"] == "resultats"), key=lambda x: x["jour"], reverse=True)
        rs = [x for x in r if x["sens"] in SENS_RESULTATS]
        fiche = {
            "ticker": e["ticker"], "nom": e["nom"], "indice": e.get("indice"), "secteur": e.get("secteur"),
            "n_communiques": n_comm, "n_evenements": len(v),
            "categories": [dict(mouvements(w), **sens_reaction(w), id=cid)
                           for cid in ORDRE for w in [[x for x in v if x["categorie"] == cid]] if w],
            "resultats": {"n": len(r), "matrice": matrice(rs),
                          "matrice_consensus": matrice([x for x in r if x.get("surprise")], "surprise",
                                                       POSITIONS_CONSENSUS),
                          "concordance": sens_reaction(rs),
                          "mouvements": mouvements(r),
                          # Propres a l'entreprise (peu de cas au debut : se
                          # completent au fil des publications).
                          "par_surprise": [x for x in croise(r, "surprise", ["positive", "conforme", "negative"])
                                           if x["n"]],
                          "par_perspectives": par_valeur(r, "perspectives", ["relevees", "confirmees", "nouvelles",
                                                                             "abaissees"]),
                          "evenements": [_evenement_public(x) for x in r]},
            "marche": marche_actuel(e["ticker"], positions, consensus, aujourd_hui, r),
        }
        serie = series.get(e["ticker"])
        prochaine = dates.get(e["ticker"])
        recent = cours_recent(serie) if serie is not None else None
        prev = prevision_entreprise(r)
        if prev is not None:  # part de hausse par sens, meme sans prochaine date connue
            fiche["p_hausse_si"] = {s: arrondi(p) for s, p in prev["p_hausse_si"].items()}
        if prochaine and recent and prev is not None:
            z = recent["z_avant"]
            sc = prev["scenarios"]
            probable = max(sc, key=sc.get)
            prochaine = dict(prochaine, **recent, tiers=tiers_de(z, bornes),
                             scenarios={k: arrondi(p) for k, p in sc.items()}, probable=probable,
                             p_sens={s: arrondi(p) for s, p in prev["p_sens"].items()},
                             p_hausse_si={s: arrondi(p) for s, p in prev["p_hausse_si"].items()},
                             p_hausse=arrondi(sum(p for k, p in sc.items() if k.endswith("_hausse"))),
                             n_historique=prev["n"], n_par_sens=prev["n_par_sens"])
            fiche["prochaine"] = prochaine
            if prochaine["date"] <= (dt.date.fromisoformat(aujourd_hui)
                                     + dt.timedelta(days=HORIZON_PROCHAINES)).isoformat():
                prochaines.append({"ticker": e["ticker"], "nom": e["nom"], "date": prochaine["date"],
                                   "estimee": prochaine["estimee"], "probable": probable,
                                   "p_probable": arrondi(sc[probable]), "p_hausse": prochaine["p_hausse"],
                                   "z_avant": z, "avant_pct": recent["avant_pct"]})
        elif recent:
            fiche["cours_recent"] = recent
        entreprises.append(fiche)
    prochaines.sort(key=lambda p: (p["date"], p["nom"]))

    # Publications des derniers jours : fiche du jour de publication.
    t_marche = jour_j.tables(resultats)
    mesures = {ev["id"]: ev for ev in evts}
    jour_pub = []
    for pub, avis in recentes or []:
        ev = mesures.get(pub["id"])
        # Probabilite de depart : seul passe de l'entreprise (hors cette publication).
        prev = prevision_entreprise([x for x in evts if x["ticker"] == pub["ticker"] and x["categorie"] == "resultats"
                                     and x["id"] != pub["id"]])
        p_base = prev["p_hausse_si"] if prev else None
        fe = consensus.get(pub["ticker"]) or {}
        perf = ev.get("perf_12m_pct") if ev else None
        if perf is None and series.get(pub["ticker"]) is not None:
            perf = (cours_recent(series[pub["ticker"]]) or {}).get("perf_12m_pct")
        f = jour_j.fiche(pub, avis, consensus_mod.avant(fe, pub["publie_le"][:10]), p_base, t_marche,
                         surprises=fe.get("surprises"), perf_12m=perf)
        if ev:
            f.update({k: ev.get(k) for k in ("jour", "rendement_pct", "indice_pct", "ecart_pct", "z")})
        jour_pub.append(f)
    jour_pub.sort(key=lambda x: x["publie_le"], reverse=True)

    decales = [x for x in evts if x.get("z_veille") is not None]
    top = sorted(evts, key=lambda x: -abs(x["z"]))[:15]
    return {
        "version": 2,
        "indice": indice,
        "depuis": depuis,
        "jusqu_a": cal.dates[-1],
        "n_communiques": n_communiques,
        "n_evenements": len(evts),
        "n_entreprises": len({x["ticker"] for x in evts}),
        "en_attente": en_attente,
        "jour_ordinaire": base,
        "tous": mouvements(evts),
        "categories": categories,
        "resultats": resultats,
        "revisions": revisions,
        "entreprises": entreprises,
        "prochaines": prochaines,
        "apres_cloture": {"n": len(decales),
                          "z_abs_jour_publication": arrondi(moyenne([abs(x["z_veille"]) for x in decales]), 2),
                          "z_abs_lendemain": arrondi(moyenne([abs(x["z"]) for x in decales]), 2)},
        "publications_recentes": jour_pub,
        "plus_fortes_reactions": [
            dict({k: x[k] for k in ("jour", "ticker", "nom", "categorie", "titre", "rendement_pct", "indice_pct",
                                    "ecart_pct", "z")}, sens=x["sens"]) for x in top],
    }
