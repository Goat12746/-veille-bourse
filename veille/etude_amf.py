"""Etude des communiques AMF (onglet Statistiques > Historique AMF) : reaction
des cours a chaque communique des entreprises du referentiel depuis 2019.

  - evenement = une entreprise et une seance de reaction (plusieurs
    communiques le meme jour ne comptent qu'une fois), de type celui du
    communique le plus important (revision d'objectifs, resultats...) ;
  - reaction : ecart au CAC 40 corrige du beta le jour de la reaction
    (hausse s'il est positif, baisse s'il est negatif), et en sigma ;
  - sens du communique : avis de Claude pour les revisions d'objectifs
    (resultats_classes.json), mots-cles sinon ;
  - publications de resultats : jugees seulement face aux attentes, c'est-a-
    dire au consensus des analystes (au-dessus, conforme, en dessous) :
    scenarios position x reaction, perspectives et objectifs ;
  - prevision de la prochaine publication : hausse ou baisse, d'apres la
    reaction du cours aux publications passees de l'entreprise ; le jour de
    la publication, la position face au consensus est comparee a celle des
    publications de son grand secteur (meme marche).
"""

import datetime as dt
import json
import math
import os

from communiques import (CATEGORIES, LIBELLES, ORDRE, charger_classes, charger_communiques, generique, normaliser,
                         rapports_redondants)
import consensus as consensus_mod
import jour_j
import positions as positions_mod
from secteurs import grand_secteur
from etude_avant import DEBUT_SUIVI
from mesures import SEUIL_FORT, SEUIL_NET, arrondi, heure_paris, mediane, moyenne, p_binomiale, pct

PRIORITE = {cid: i for i, cid in enumerate(ORDRE)}


def _historique(nom):
    """{ticker: [[date, estime, publie]]} lu dans un fichier de la veille ({} s'il manque)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), nom), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# BPA estime par les analystes avant chaque publication et BPA publie, depuis
# 2015 (calendrier des resultats de Yahoo).
HISTORIQUE_BPA = _historique("consensus_historique.json")
# A defaut de Yahoo, BPA attendu et publie d'Investing.com, valeurs francaises
# (meme releve que le chiffre d'affaires, attendu egal au publie ecarte).
HISTORIQUE_BPA_INVESTING = _historique("consensus_bpa_investing.json")
# Chiffre d'affaires attendu et publie, depuis 2014, valeurs francaises
# (Investing.com, releve a la main ; lignes ou l'attendu egale le publie et
# periodes melangees ecartees).
HISTORIQUE_CA = _historique("consensus_ca_historique.json")
# BPA publié vérifié dans les communiqués ("ticker|jour" : corrige = BPA ajusté
# du communiqué à la place de celui de Yahoo ou d'Investing, exclu = écart
# inutilisable : BPA comptable face à un consensus ajusté sans ajusté connu,
# chiffre d'affaires seul, attendu incohérent...).
BPA_VERIFIE = _historique("bpa_verifie.json").get("publications", {})
# Google Finance (google_finance.py) : BPA et chiffre d'affaires publies face
# a l'estimation, dernier rapport de chaque entreprise releve au fil des jours.
GOOGLE = _historique("consensus_google.json").get("entreprises", {})
# Publications depuis le debut du suivi (l'application a l'historique d'avant) :
# memes filtres que cet historique.
ECART_COURS_MAX = 5.0  # % : BPA ecarte si |publie - attendu| depasse 5 % du cours de la veille
CA_RAPPORT = (0.6, 1.6)  # chiffre d'affaires publie / attendu hors de ces bornes : periodes melangees
BPA_PERIODES_EUROPE = ((1.65, 2.5), (0.4, 0.61))  # BPA publie / attendu : semestre face a un trimestre
AVANT = 20  # seances avant la publication (le cours "recent")
SUITE = 5  # seances apres la reaction (la baisse ou la hausse se prolonge-t-elle ?)
UN_AN = 250  # seances de l'evolution sur 12 mois face a l'indice
QUANTILE_FORTE_HAUSSE = 0.8  # 20 % des publications ayant le plus monte sur 12 mois
REACTIONS = ("hausse", "baisse")
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
POSITIONS_CONSENSUS = ("positive", "conforme", "negative")  # face au consensus des analystes


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
            # Publications de resultats : jugees face au consensus seulement,
            # pas de sens (bons ou mauvais face a l'an dernier).
            sens, origine = (avis.get("sens") if cat == "revision" else None), "claude"
        elif cat == "resultats":
            sens, origine = None, "mots"
        else:
            vus = {c["sens_mots"] for c in membres} - {None}
            sens, origine = (vus.pop() if len(vus) == 1 else None), "mots"
        principal = membres[0]
        ev = {"ticker": ticker, "nom": par_isin[principal["isin"]]["nom"], "jour": jour, "categorie": cat,
              "periode": next((c["periode"] for c in membres if c.get("periode")), None),
              "titre": _titre(principal), "id": principal["id"], "publie_le": cs[0][1]["publie_le"],
              "n_communiques": len(cs), "sens": sens, "origine": origine}
        if avis is not None and avis["type"] in ("resultats", "revision"):
            for k in ("perspectives", "attentes", "consensus"):
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
    return sans_doublons(evts), jours, en_attente


DOUBLON_JOURS = 4  # jours calendaires


def sans_doublons(evts):
    """Une publication de resultats suivie, quelques jours plus tard, de la
    mise en ligne du rapport financier (ou du communique en anglais) ne compte
    qu'une fois : on garde la premiere seance de reaction. Le jour ecarte reste
    exclu de la base "jour ordinaire"."""
    derniere, garde = {}, []
    for ev in sorted(evts, key=lambda x: (x["ticker"], x["jour"])):
        if ev["categorie"] == "resultats":
            j = dt.date.fromisoformat(ev["jour"])
            avant = derniere.get(ev["ticker"])
            derniere[ev["ticker"]] = j
            if avant is not None and (j - avant).days <= DOUBLON_JOURS:
                continue
        garde.append(ev)
    return garde


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


def enrichir(evts, positions, consensus, series=None):
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
            # Jugement d'ensemble de Claude (tous les indicateurs face au
            # consensus) ; a defaut, l'ecart chiffre du BPA (Yahoo, ci-dessous).
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
            if ev.get("consensus") in SURPRISES:
                continue  # jugement de Claude
            # Ecart exact (avant l'arrondi de surprise_pct) face au seuil.
            ev["surprise"] = (jour_j.position_consensus(s.get("publie"), s.get("estime"))
                              or ("positive" if s["surprise_pct"] > SEUIL_SURPRISE
                                  else "negative" if s["surprise_pct"] < -SEUIL_SURPRISE else "conforme"))
        # Historique complet de Yahoo pour les publications sans jugement ni surprise recente,
        # puis celui d'Investing pour celles qui restent.
        for historique in (HISTORIQUE_BPA, HISTORIQUE_BPA_INVESTING):
            for ev, (_, estime, publie) in _rapprocher(
                    [x for x in v if x["categorie"] == "resultats" and not x.get("surprise")], historique.get(ticker)):
                if not estime:
                    continue
                ev["surprise_bpa_pct"] = round((publie - estime) / abs(estime) * 100, 1)
                ev["surprise"] = jour_j.position_consensus(publie, estime)
        # Verification dans les communiques (le jugement de Claude, s'il existe, reste).
        for ev in v:
            verif = BPA_VERIFIE.get(f"{ticker}|{ev['jour']}")
            if verif is None or ev.get("surprise_bpa_pct") is None:
                continue
            claude = ev.get("consensus") in SURPRISES
            if verif["verdict"] == "exclu":
                ev.pop("surprise_bpa_pct")
                if not claude:
                    ev.pop("surprise", None)
            elif verif["verdict"] == "corrige" and verif.get("estime"):
                estime, publie = verif["estime"], verif["publie_corrige"]
                ev["surprise_bpa_pct"] = round((publie - estime) / abs(estime) * 100, 1)
                if not claude:
                    ev["surprise"] = jour_j.position_consensus(publie, estime)
        # Chiffre d'affaires face au consensus (memes seuils que le BPA).
        for ev, (_, estime, publie) in _rapprocher([x for x in v if x["categorie"] == "resultats"],
                                                   HISTORIQUE_CA.get(ticker)):
            if not estime:
                continue
            ev["surprise_ca_pct"] = round((publie - estime) / abs(estime) * 100, 1)
            ev["surprise_ca"] = jour_j.position_consensus(publie, estime)
        suivi(ticker, [x for x in v if x["categorie"] == "resultats" and x["jour"] >= DEBUT_SUIVI],
              fiche, (series or {}).get(ticker))


def suivi(ticker, v, fiche, serie):
    """Publications depuis DEBUT_SUIVI : chiffre d'affaires face au consensus
    (Google Finance ; estimation Yahoo du trimestre a defaut), BPA de Google a
    defaut de Yahoo, puis filtres de l'historique de l'application (BPA ecarte
    au-dela de 5 % du cours, periodes melangees)."""
    google = (GOOGLE.get(ticker) or {}).get("rapports") or []
    europe = "." in ticker
    lignes_ca, lignes_bpa = [], []
    for date, _, _, bpa_pub, bpa_est, ca_pub, ca_est, _ in google:
        if ca_pub is not None and ca_est is None:
            releve = consensus_mod.avant(fiche, date) if fiche else None
            q = ((releve or {}).get("ca") or {}).get("0q")
            if q and q[0] and ((releve.get("fin") or {}).get("0q") or "9") <= date:
                ca_est = q[0]
        lignes_ca.append([date, ca_est, ca_pub])
        lignes_bpa.append([date, bpa_est, bpa_pub])
    for ev, (_, estime, publie) in _rapprocher([x for x in v if x.get("surprise_ca_pct") is None], lignes_ca):
        if estime and publie is not None:
            ev["surprise_ca_pct"] = round((publie - estime) / abs(estime) * 100, 1)
            ev["surprise_ca"] = jour_j.position_consensus(publie, estime)
    for ev, (_, estime, publie) in _rapprocher([x for x in v if x.get("surprise_bpa_pct") is None], lignes_bpa):
        if estime and publie is not None:
            ev["surprise_bpa_pct"] = round((publie - estime) / abs(estime) * 100, 1)
            ev["bpa_suivi"] = (estime, publie)
            if ev.get("consensus") not in SURPRISES:
                ev["surprise"] = jour_j.position_consensus(publie, estime)
    for ev in v:
        # Chiffre d'affaires d'une autre periode que le consensus.
        e = ev.get("surprise_ca_pct")
        if e is not None and not CA_RAPPORT[0] <= 1 + e / 100 <= CA_RAPPORT[1]:
            ev.pop("surprise_ca_pct")
            ev.pop("surprise_ca", None)
        e = ev.get("surprise_bpa_pct")
        if e is None:
            continue
        ecarte = europe and any(a <= 1 + e / 100 <= b for a, b in BPA_PERIODES_EUROPE)
        # Ecart au consensus rapporte au cours de la veille.
        estime, publie = ev.get("bpa_suivi") or _bpa_yahoo(fiche, ev)
        if not ecarte and estime is not None and serie is not None and ev["jour"] in serie.pos:
            s = serie.pos[ev["jour"]]
            cours = serie.clo[s - 1] if s > 0 else None
            if cours and ticker.endswith(".L") and cours / max(abs(estime), abs(publie), 1e-9) > 600:
                cours /= 100  # cours en pence, BPA en livres
            ecarte = bool(cours) and abs(publie - estime) / cours * 100 > ECART_COURS_MAX
        ev.pop("bpa_suivi", None)
        if ecarte:
            ev.pop("surprise_bpa_pct")
            if ev.get("consensus") not in SURPRISES:
                ev.pop("surprise", None)


def _bpa_yahoo(fiche, ev):
    """(estime, publie) du BPA trimestriel Yahoo rapproche d'une publication
    (meme regle qu'enrichir), (None, None) sinon."""
    for s in (fiche or {}).get("surprises", []):
        fin = (dt.date.fromisoformat(s["trimestre"]) + dt.timedelta(days=110)).isoformat()
        if s["trimestre"] < ev["jour"] <= fin and s.get("estime") and s.get("publie") is not None:
            return s["estime"], s["publie"]
    return None, None


def _rapprocher(v, lignes):
    """(publication, ligne) : pour chaque publication, la ligne de l'historique la
    plus proche (3 jours au plus) de sa date, chaque ligne servant une fois."""
    if not lignes:
        return []
    prises, res = set(), []
    for ev in v:
        pub = dt.date.fromisoformat(ev["publie_le"][:10])
        ecarts = [(abs((dt.date.fromisoformat(r[0]) - pub).days), i) for i, r in enumerate(lignes)
                  if i not in prises]
        if not ecarts or min(ecarts)[0] > 3:
            continue
        i = min(ecarts)[1]
        prises.add(i)
        res.append((ev, lignes[i]))
    return res


def croise(v, cle, valeurs):
    """Part de hausse selon la valeur d'un champ, en tout et par position
    face au consensus (au-dessus, conforme, en dessous)."""
    res = []
    for val in valeurs:
        w = [x for x in v if x.get(cle) == val and x["reaction"]]
        par_surprise = {}
        for s in POSITIONS_CONSENSUS:
            u = [x for x in w if x.get("surprise") == s]
            par_surprise[s] = {"n": len(u),
                               "part_hausse": _part(sum(1 for x in u if x["reaction"] == "hausse"), len(u))}
        suites = [x["suite_pct"] for x in w if x.get("suite_pct") is not None]
        res.append({"valeur": val, "n": len(w),
                    "part_hausse": _part(sum(1 for x in w if x["reaction"] == "hausse"), len(w)),
                    "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in w]), 2) if w else None,
                    "suite_moyenne_pct": arrondi(moyenne(suites), 2) if suites else None,
                    "par_surprise": par_surprise})
    return res


def attentes_marche(res, consensus):
    """Resume des attentes du marche : positions vendeuses (historique AMF
    complet) et consensus des analystes (historique en construction)."""
    courtes = [x for x in res if x.get("courtes") is not None]
    surpr = [x for x in res if x.get("surprise")]
    revis = [x for x in res if x.get("revision")]
    concord = [x for x in surpr if x["surprise"] != "conforme" and x["reaction"]]
    ok = sum(1 for x in concord if (x["surprise"], x["reaction"]) in (("positive", "hausse"), ("negative", "baisse")))
    releves = [r["jour"] for f in consensus.values() for r in f.get("releves", []) if not r.get("reconstitue")]
    return {
        "positions_courtes": {"n": len(courtes),
                              "par_tranche": croise(courtes, "courtes", [t for t, _, _ in TRANCHES_COURTES])},
        "consensus": {
            "n_entreprises": len({t for t, f in consensus.items() if f.get("releves")}
                                 | {x["ticker"] for x in surpr}),
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


def concordance_consensus(v, cle="surprise"):
    """Publications au-dessus du consensus suivies d'une hausse, en dessous
    suivies d'une baisse : parts et test de concordance (meme forme que
    sens_reaction)."""
    pos = [x for x in v if x.get(cle) == "positive" and x["reaction"]]
    neg = [x for x in v if x.get(cle) == "negative" and x["reaction"]]
    h = sum(1 for x in pos if x["reaction"] == "hausse")
    b = sum(1 for x in neg if x["reaction"] == "baisse")
    return {"n_positif": len(pos), "n_negatif": len(neg),
            "hausse_si_positif": _part(h, len(pos)), "baisse_si_negatif": _part(b, len(neg)),
            "concordance": _part(h + b, len(pos) + len(neg)),
            "p": arrondi(p_binomiale(h + b, len(pos) + len(neg)), 4),
            "ecart_moyen_positif_pct": arrondi(moyenne([x["ecart_pct"] for x in pos]), 2),
            "ecart_moyen_negatif_pct": arrondi(moyenne([x["ecart_pct"] for x in neg]), 2)}


SEUILS_AUTRES = (2, 10, 15)  # autres matrices : meme etude avec un ecart de plus de 2, 10 ou 15 %


def avec_surprise(v, seuil, ecart="surprise_bpa_pct", cle="surprise"):
    """Publications dont l'ecart au consensus est connu (surprise_bpa_pct, ou
    surprise_ca_pct), avec leur position face au consensus au seuil donne
    (champ <cle><seuil>)."""
    sortie = []
    for x in v:
        e = x.get(ecart)
        if e is None:
            continue
        x[f"{cle}{seuil}"] = "positive" if e > seuil else "negative" if e < -seuil else "conforme"
        sortie.append(x)
    return sortie


def matrices_seuils(v, ecart="surprise_bpa_pct", cle="surprise", prefixe="matrice_consensus"):
    """{"matrice_consensus2": ..., "matrice_consensus10": ..., ...}"""
    return {f"{prefixe}{t}": matrice(avec_surprise(v, t, ecart, cle), f"{cle}{t}") for t in SEUILS_AUTRES}


def matrice(v, cle="surprise", valeurs=POSITIONS_CONSENSUS):
    """Scenarios : position face au consensus x reaction, nombre, ecart moyen
    a l'indice sur les 20 seances avant, le jour de la reaction et les 5
    seances apres."""
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


def part_hausse_passee(hist):
    """Part de hausse des publications de resultats passees de l'entreprise
    (point de depart de la probabilite du jour de publication), lissee d'un
    cas fictif par issue. None sans publication mesuree."""
    v = [x for x in hist if x["reaction"]]
    if not v:
        return None
    return {"p": arrondi((sum(1 for x in v if x["reaction"] == "hausse") + 1) / (len(v) + 2)), "n": len(v)}


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
    cles = ["jour", "periode", "titre", "perspectives", "attentes", "exceptionnel", "actionnaires", "avant_pct", "z_avant", "perf_12m_pct", "rendement_pct", "ecart_pct", "z",
            "suite_pct",
            "reaction", "courtes_pct", "surprise", "surprise_bpa_pct", "surprise_ca_pct", "surprise_ca", "revision_30j_pct", "objectifs_vs"]
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
    enrichir(evts, positions, consensus, series)
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
    mesurees = [x for x in res if x["reaction"]]
    resultats = {
        "n": len(res), "n_avis_claude": sum(1 for x in res if x["origine"] == "claude"),
        "n_consensus": sum(1 for x in res if x.get("surprise")),
        "part_hausse": _part(sum(1 for x in mesurees if x["reaction"] == "hausse"), len(mesurees)),
        "mouvements": mouvements(res),
        # Scenarios face au consensus des analystes (historique du meme marche).
        "matrice_consensus": matrice([x for x in res if x.get("surprise")]),
        **matrices_seuils(res),
        "concordance": concordance_consensus(res),
        # Meme etude pour le chiffre d'affaires (France : historique Investing).
        "n_consensus_ca": sum(1 for x in res if x.get("surprise_ca")),
        "n_entreprises_ca": len({x["ticker"] for x in res if x.get("surprise_ca")}),
        "matrice_consensus_ca": matrice([x for x in res if x.get("surprise_ca")], "surprise_ca"),
        **matrices_seuils(res, "surprise_ca_pct", "surprise_ca", "matrice_consensus_ca"),
        "concordance_ca": concordance_consensus(res, "surprise_ca"),
        "par_periode": par_valeur(res, "periode",
                                  ["annuels", "semestriels", "trimestriels", "chiffre_affaires", "autres"]),
        "par_perspectives": par_valeur(res, "perspectives", ["relevees", "confirmees", "nouvelles", "abaissees"]),
        # Part de hausse par perspectives (et objectifs) x position face au
        # consensus (ajustement des probabilites le jour de la publication,
        # jour_j.py).
        "par_perspectives_surprise": croise(res, "perspectives", ["relevees", "confirmees", "nouvelles",
                                                                  "abaissees"]),
        "par_objectifs_surprise": croise(res, "objectifs_vs", ["superieurs", "conformes", "inferieurs"]),
        "attentes_marche": attentes_marche(res, consensus),
        "forte_hausse_12m": table_forte_hausse(res),
    }

    # Revisions d'objectifs (hors publication de resultats).
    rev = [x for x in evts if x["categorie"] == "revision"]
    revisions = dict(mouvements(rev), **sens_reaction(rev))

    # Par entreprise.
    dates = prochaines_dates(referentiel, res, calendrier, aujourd_hui)
    # Scenarios face au consensus de chaque grand secteur (meme marche) : le
    # jour de la publication, la position face au consensus est comparee a
    # celle des publications du secteur (jour_j.py).
    secteur_de = {e["ticker"]: grand_secteur(e.get("secteur")) for e in referentiel}
    par_secteur = {}
    for x in res:
        if x.get("surprise"):
            par_secteur.setdefault(secteur_de.get(x["ticker"]), []).append(x)
    resultats["consensus_par_secteur"] = {g: matrice(v) for g, v in sorted(par_secteur.items()) if g}
    entreprises, prochaines = [], []
    for e in referentiel:
        v = [x for x in evts if x["ticker"] == e["ticker"]]
        n_comm = n_par_ticker.get(e["ticker"], 0)
        r = sorted((x for x in v if x["categorie"] == "resultats"), key=lambda x: x["jour"], reverse=True)
        fiche = {
            "ticker": e["ticker"], "nom": e["nom"], "indice": e.get("indice"), "secteur": e.get("secteur"),
            "n_communiques": n_comm, "n_evenements": len(v),
            "categories": [dict(mouvements(w), **sens_reaction(w), id=cid)
                           for cid in ORDRE for w in [[x for x in v if x["categorie"] == cid]] if w],
            "resultats": {"n": len(r), "matrice_consensus": matrice([x for x in r if x.get("surprise")]),
                          **matrices_seuils(r),
                          "concordance": concordance_consensus(r),
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
        gs = secteur_de.get(e["ticker"])
        fiche["grand_secteur"] = gs
        passe = part_hausse_passee(r)
        if passe is not None:  # depart de la probabilite du jour de publication
            fiche["p_hausse_passee"] = passe
        if prochaine and recent:
            # Deux scenarios, hausse ou baisse, d'apres la reaction du cours
            # aux publications passees de l'entreprise.
            z = recent["z_avant"]
            prochaine = dict(prochaine, **recent, n_historique=passe["n"] if passe else 0)
            probable = None
            if passe is not None:
                sc = {"hausse": passe["p"], "baisse": arrondi(1 - passe["p"])}
                probable = max(sc, key=sc.get)
                prochaine.update(scenarios=sc, probable=probable, p_hausse=passe["p"])
            fiche["prochaine"] = prochaine
            if prochaine["date"] <= (dt.date.fromisoformat(aujourd_hui)
                                     + dt.timedelta(days=HORIZON_PROCHAINES)).isoformat():
                prochaines.append({k: v for k, v in {
                    "ticker": e["ticker"], "nom": e["nom"], "date": prochaine["date"],
                    "estimee": prochaine["estimee"], "probable": probable,
                    "p_probable": prochaine["scenarios"][probable] if probable else None,
                    "p_hausse": prochaine.get("p_hausse"), "z_avant": z, "avant_pct": recent["avant_pct"]}.items()
                    if v is not None})
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
        p_base = part_hausse_passee([x for x in evts if x["ticker"] == pub["ticker"]
                                     and x["categorie"] == "resultats" and x["id"] != pub["id"]])
        fe = consensus.get(pub["ticker"]) or {}
        perf = ev.get("perf_12m_pct") if ev else None
        if perf is None and series.get(pub["ticker"]) is not None:
            perf = (cours_recent(series[pub["ticker"]]) or {}).get("perf_12m_pct")
        f = jour_j.fiche(pub, avis, consensus_mod.avant(fe, pub["publie_le"][:10]), p_base, t_marche,
                         surprises=fe.get("surprises"), perf_12m=perf, secteur=secteur_de.get(pub["ticker"]))
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
                                    "ecart_pct", "z")},
                 sens=x["sens"] if x["categorie"] != "resultats" else None, surprise=x.get("surprise"))
            for x in top],
    }
