#!/usr/bin/env python3
"""Statistiques de la veille : les alertes annoncent-elles la reaction des
cours ? Ecrit statistiques.json, lu par l'onglet Statistiques de
l'application.

Methode : etude d'evenements (MacKinlay, 1997), voir mesures.py. Pour chaque
entreprise d'une alerte :
  - ecart (rendement anormal) le jour de la reaction : rendement observe
    moins rendement attendu d'apres le CAC 40 et le beta de l'action ;
  - ecart en sigma (ecart habituel d'une seance, residus du modele) :
    bruit sous 1, net de 1 a 2, fort au-dela de 2 ;
  - points : sens juste +1, faux -1, dans le bruit 0 ; ampleur conforme +1,
    un cran d'ecart 0, fausse alerte ou impact sous-estime -1 ;
  - tests : t sur les ecarts standardises (Boehmer, Musumeci et Poulsen,
    1991), test du signe, test de rang (Corrado, 1989), correlation de rang
    entre ampleur annoncee et mouvement observe (Spearman).
Jour de la reaction : premiere seance qui cloture (17 h 35) apres la
publication ; sans heure connue, le jour de publication et le suivant.

Etude des communiques AMF (etude_amf.py) : reaction des cours a chaque
communique des entreprises suivies depuis 2019, par entreprise et par type,
selon le sens du communique ; publications de resultats (bons ou mauvais
resultats, hausse ou baisse) et prevision de la reaction.

Usage : python statistiques.py [--depuis-historique 2019-01-01] [--sans-historique]
Bibliotheque standard uniquement.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys

from etude_amf import etude
from mesures import (CLOTURE, ESTIMATION, INDICE, OUVERTURE, SEUIL_FORT, SEUIL_NET, Calendrier, Serie, arrondi,
                     heure_paris, limite_cotation, moyenne, p_binomiale, p_normale, pct, spearman,
                     telecharger_cours, test_t)

ICI = os.path.dirname(os.path.abspath(__file__))
MACRO = 10  # alerte touchant au moins 10 entreprises : comparee a l'indice
APRES = 5  # seances mesurees apres la publication de l'alerte
SUIVI = (5, 20)  # ecarts cumules affiches a J+5 et J+20
DETAIL_MAX = 150  # alertes detaillees dans le fichier (les indicateurs portent sur toutes)
SENS_NOTES = ("positif", "negatif")
AMPLEURS_NOTEES = ("faible", "moyenne", "forte")
# Etapes d'une mesure encore incertaine au moment de l'alerte : leur
# probabilite est jugee une fois la mesure adoptee ou rejetee.
ETAPES_INCERTAINES = ("rumeur", "annonce", "depot", "adopte_commission", "adopte_seance")


def apres_publication(serie, mod, e0):
    """Ecart de l'ouverture de la seance e0 a la cloture APRES seances
    plus tard : ce qu'on pouvait gagner en suivant l'alerte."""
    beta, sigma = mod[1], mod[2]
    k = min(APRES, len(serie.dates) - e0)
    if k <= 0 or not serie.ouv[e0] or not serie.ouv_m[e0]:
        return None
    car = (serie.clo[e0] / serie.ouv[e0] - 1) - beta * (serie.clo_m[e0] / serie.ouv_m[e0] - 1)
    if k > 1:
        car += serie.ecart(mod, e0 + 1, e0 + k - 1)
    return {"debut": serie.dates[e0], "seances": k, "complet": k == APRES,
            "rendement_pct": pct(serie.clo[e0 + k - 1] / serie.ouv[e0] - 1),
            "ecart_pct": pct(car), "z": round(car / (sigma * math.sqrt(k - 0.5)), 2)}


# ---------------------------------------------------------------------------
# Notation d'une alerte
# ---------------------------------------------------------------------------

def niveau(z_dir):
    """Mouvement observe, dans le sens annonce."""
    if z_dir >= SEUIL_FORT:
        return "fort"
    if z_dir >= SEUIL_NET:
        return "net"
    return "contraire" if z_dir <= -SEUIL_NET else "bruit"


def niveau_absolu(z):
    return "fort" if abs(z) >= SEUIL_FORT else "net" if abs(z) >= SEUIL_NET else "bruit"


# Points d'ampleur : (ampleur annoncee, mouvement dans le sens annonce).
POINTS_AMPLEUR = {
    "forte": {"fort": 1, "net": 0, "rien": -1},
    "moyenne": {"net": 1, "fort": 0, "rien": -1},
    "faible": {"rien": 0, "net": 0, "fort": -1},
}


def points(ampleur, z_dir):
    """Sens : +1 mouvement net ou fort dans le sens annonce, -1 dans le sens
    contraire, 0 dans le bruit. Ampleur : +1 si le mouvement correspond
    (forte : fort, moyenne : net), -1 pour une fausse alerte (forte ou
    moyenne sans mouvement) ou un impact faible suivi d'un mouvement fort, 0
    sinon (un cran d'ecart ; faible sans mouvement ne rapporte rien, sinon
    repondre toujours "faible" gagnerait des points sans rien prevoir)."""
    sens = 1 if z_dir >= SEUIL_NET else -1 if z_dir <= -SEUIL_NET else 0
    n = niveau(z_dir)
    amp = POINTS_AMPLEUR.get(ampleur, {}).get(n if n in ("fort", "net") else "rien")
    return {"sens": sens, "ampleur": amp, "total": sens + (amp or 0)}


def fenetre_reaction(cal, alerte):
    """Premiere et derniere seance de reaction : l'information est publique
    au plus tot a la premiere source. Source sans heure : le jour meme ou le
    lendemain (avant ou apres la cloture), d'ou une fenetre de deux seances."""
    bas = haut = None
    for s in alerte.get("sources") or [{"date": alerte["date"]}]:
        if s.get("publie_le"):
            b = h = heure_paris(s["publie_le"])
        else:
            b = dt.datetime.fromisoformat(s["date"])
            h = b.replace(hour=23, minute=59)
        bas = b if bas is None or b < bas else bas
        haut = h if haut is None or h < haut else haut
    return cal.reaction(bas), cal.reaction(haut), bas


def publication_alerte(alerte):
    """Moment ou l'alerte est disponible (fin de journee si seule la date
    est connue)."""
    if alerte.get("publie_a"):
        return heure_paris(alerte["publie_a"])
    return dt.datetime.fromisoformat(alerte["ajoute_le"]).replace(hour=23, minute=59)


def evaluer_alertes(alertes, cal, series):
    """Mesure de chaque (alerte, entreprise) et statut : note, attente,
    confondu (autre alerte sur la meme action aux memes seances), macro,
    non_directionnel (sens neutre ou incertain), donnees (cours manquants)."""
    details, fenetres = [], {}
    for a in alertes:
        debut, fin, info = fenetre_reaction(cal, a)
        pub = publication_alerte(a)
        macro = len(a["entreprises"]) >= MACRO
        d = {
            "id": a["id"], "titre": a["titre"], "date": a["date"], "source": a["source"],
            "etape": (a.get("initial") or {}).get("etape", a["etape"]),
            "probabilite": (a.get("initial") or {}).get("probabilite", a["probabilite"]),
            "publie_a": pub.isoformat(timespec="minutes"),
            "information": info.isoformat(timespec="minutes"),
            "reaction": {"debut": debut, "fin": fin,
                         "seances": cal.pos[fin] - cal.pos[debut] + 1 if debut and fin else None},
            # Alerte disponible avant l'ouverture de la seance de reaction :
            # seul ce cas teste une vraie prevision (sinon l'analyse a pu
            # voir le cours bouger).
            "avant_reaction": bool(debut) and pub < dt.datetime.fromisoformat(debut).replace(
                hour=OUVERTURE[0], minute=OUVERTURE[1]),
            "macro": macro,
            "nb_entreprises": len(a["entreprises"]),
            # Retiree des actualites apres publication : reste comptee.
            **({"retrait": {"le": a["retiree_le"], "motif": a.get("motif_retrait", "")}}
               if a.get("retiree_le") else {}),
            "observations": [],
        }
        details.append((a, d))
        if macro or not debut or not fin:
            continue
        for e in a["entreprises"]:
            fenetres.setdefault(e["ticker"], []).append((debut, fin, a["id"]))

    for a, d in details:
        debut, fin = d["reaction"]["debut"], d["reaction"]["fin"]
        if d["macro"]:
            d["indice"] = mesure_indice(a, cal, series, debut, fin)
            continue
        e0 = cal.ouverture(publication_alerte(a))
        for e in a["entreprises"]:
            o = {"ticker": e["ticker"], "nom": e.get("nom", e["ticker"]), "sens": e["sens"],
                 "ampleur": e["ampleur"]}
            d["observations"].append(o)
            serie = series.get(e["ticker"])
            if not debut or not fin:
                o["statut"] = "attente"
                continue
            if serie is None or debut not in serie.pos or fin not in serie.pos:
                o["statut"] = "donnees"
                continue
            s, f = serie.pos[debut], serie.pos[fin]
            mod = serie.modele(s)
            if mod is None:
                o["statut"] = "donnees"
                continue
            o.update(serie.mesure(mod, s, f))
            # Suivi : ecart cumule avant l'information, puis jusqu'a J+5 et J+20.
            if s - 5 >= 1:
                o["avant_pct"] = pct(serie.ecart(mod, s - 5, s - 1))
            for j in SUIVI:
                if s + j < len(serie.dates):
                    o[f"j{j}_pct"] = pct(serie.ecart(mod, s, s + j))
            if e0 and e0 in serie.pos:
                o["apres"] = apres_publication(serie, mod, serie.pos[e0])
            autres = [x for x in fenetres.get(e["ticker"], [])
                      if x[2] != a["id"] and x[0] <= fin and debut <= x[1]]
            if e["sens"] not in SENS_NOTES:
                o["statut"] = "non_directionnel"
                o["niveau"] = niveau_absolu(o["z"])
            elif autres:
                o["statut"] = "confondu"
                o["confondu_avec"] = [x[2] for x in autres]
                o["niveau"] = niveau(o["z"] if e["sens"] == "positif" else -o["z"])
            else:
                o["statut"] = "note"
                z_dir = o["z"] if e["sens"] == "positif" else -o["z"]
                o["niveau"] = niveau(z_dir)
                o["points"] = points(e["ampleur"], z_dir)
    return [d for _, d in details]


def mesure_indice(a, cal, series, debut, fin):
    """Alerte macro : reaction du CAC 40 lui-meme, dans le sens majoritaire."""
    sens = [e["sens"] for e in a["entreprises"]]
    majoritaire = max(set(sens), key=sens.count)
    r = {"sens": majoritaire if sens.count(majoritaire) >= 0.75 * len(sens) else "mixte"}
    serie = series.get(INDICE)
    if not debut or not fin or serie is None:
        r["statut"] = "attente"
        return r
    s, f = serie.pos[debut], serie.pos[fin]
    mod = serie.modele(s)
    if mod is None:
        r["statut"] = "donnees"
        return r
    ret = serie.clo_m[f] / serie.clo_m[s - 1] - 1
    z = ret / (mod[4] * math.sqrt(f - s + 1))
    r.update(statut="mesure", rendement_pct=pct(ret), z=round(z, 2),
             niveau=niveau(z if r["sens"] == "positif" else -z) if r["sens"] in SENS_NOTES
             else niveau_absolu(z))
    return r


def synthese(obs):
    """Indicateurs d'un ensemble d'observations notees."""
    n = len(obs)
    res = {"n": n}
    if not n:
        return res
    total = [o["points"]["total"] for o in obs]
    z_dir = [o["z"] if o["sens"] == "positif" else -o["z"] for o in obs]
    ecart_dir = [o["ecart_pct"] if o["sens"] == "positif" else -o["ecart_pct"] for o in obs]
    justes = sum(1 for z in z_dir if z >= SEUIL_NET)
    faux = sum(1 for z in z_dir if z <= -SEUIL_NET)
    m, t, p = test_t(z_dir)
    positifs = sum(1 for z in z_dir if z > 0)
    rangs = [o["rang"] if o["sens"] == "positif" else 1 - o["rang"] for o in obs]
    rang_moyen = moyenne(rangs)
    res.update({
        "points": {"total": sum(total), "moyenne": arrondi(moyenne(total), 2),
                   "repartition": {str(k): total.count(k) for k in (2, 1, 0, -1, -2)}},
        "sens": {"justes": justes, "faux": faux, "bruit": n - justes - faux,
                 "taux": arrondi(justes / (justes + faux)) if justes + faux else None,
                 "p": arrondi(p_binomiale(justes, justes + faux), 4)},
        "ecart_moyen_pct": arrondi(moyenne(ecart_dir), 2),
        "z_moyen": arrondi(m, 2), "t": arrondi(t, 2), "p_t": arrondi(p, 4),
        "signe": {"positifs": positifs, "p": arrondi(p_binomiale(positifs, n), 4)},
        "rang": {"moyen": arrondi(rang_moyen), "p": arrondi(p_normale((rang_moyen - 0.5) * math.sqrt(12 * n)), 4)},
    })
    # Ampleur annoncee et mouvement observe.
    amp = [o for o in obs if o["ampleur"] in AMPLEURS_NOTEES]
    matrice = {a: {c: 0 for c in ("contraire", "bruit", "net", "fort")} for a in AMPLEURS_NOTEES}
    for o in amp:
        matrice[o["ampleur"]][o["niveau"]] += 1
    rho, p_rho = spearman([AMPLEURS_NOTEES.index(o["ampleur"]) for o in amp], [abs(o["z"]) for o in amp])
    res["ampleur"] = {
        "n": len(amp), "matrice": matrice,
        "z_abs_moyen": {a: arrondi(moyenne([abs(o["z"]) for o in amp if o["ampleur"] == a]), 2)
                        for a in AMPLEURS_NOTEES},
        "spearman": arrondi(rho), "p": arrondi(p_rho, 4)}
    # Apres la publication de l'alerte (fenetre complete seulement).
    ap = [o for o in obs if o.get("apres") and o["apres"]["complet"]]
    ap_ecart = [o["apres"]["ecart_pct"] if o["sens"] == "positif" else -o["apres"]["ecart_pct"] for o in ap]
    ap_z = [o["apres"]["z"] if o["sens"] == "positif" else -o["apres"]["z"] for o in ap]
    m, t, p = test_t(ap_z)
    res["apres_publication"] = {"n": len(ap), "ecart_moyen_pct": arrondi(moyenne(ap_ecart), 2),
                                "z_moyen": arrondi(m, 2), "p": arrondi(p, 4)}
    return res


def resume_groupe(obs):
    """Resume court par categorie (source, etape, ampleur)."""
    z_dir = [o["z"] if o["sens"] == "positif" else -o["z"] for o in obs]
    justes = sum(1 for z in z_dir if z >= SEUIL_NET)
    faux = sum(1 for z in z_dir if z <= -SEUIL_NET)
    return {"n": len(obs), "points_moyen": arrondi(moyenne([o["points"]["total"] for o in obs]), 2),
            "justes": justes, "faux": faux, "z_moyen": arrondi(moyenne(z_dir), 2)}


def probabilites(alertes):
    """Score de Brier des probabilites initiales, pour les mesures dont le
    sort est connu (adoptees definitivement ou rejetees)."""
    tranchees, en_cours = [], 0
    for a in alertes:
        initial = a.get("initial") or {"etape": a["etape"], "probabilite": a["probabilite"]}
        if initial["etape"] not in ETAPES_INCERTAINES:
            continue
        if a["etape"] in ("adopte_definitif", "rejete"):
            tranchees.append((initial["probabilite"], 1 if a["etape"] == "adopte_definitif" else 0))
        else:
            en_cours += 1
    return {"n_tranchees": len(tranchees), "n_en_cours": en_cours,
            "brier": arrondi(moyenne([(p - o) ** 2 for p, o in tranchees]))}


def statistiques_alertes(alertes, cal, series):
    details = evaluer_alertes(alertes, cal, series)
    obs = [o for d in details for o in d["observations"]]
    notees = [o for o in obs if o["statut"] == "note"]
    avant = [o for d in details if d["avant_reaction"] for o in d["observations"] if o["statut"] == "note"]
    statuts = {}
    for o in obs:
        statuts[o["statut"]] = statuts.get(o["statut"], 0) + 1
    macro = [d for d in details if d["macro"]]
    if macro:
        statuts["macro"] = sum(d["nb_entreprises"] for d in macro)

    def par(cle):
        groupes = {}
        for d in details:
            for o in d["observations"]:
                if o["statut"] == "note":
                    groupes.setdefault(d[cle] if cle in d else o[cle], []).append(o)
        return [dict(resume_groupe(v), cle=k) for k, v in sorted(groupes.items(), key=lambda x: -len(x[1]))]

    return {
        "n_alertes": len(details),
        "n_observations": len(obs) + statuts.get("macro", 0),
        "statuts": statuts,
        "synthese": synthese(notees),
        "avant_reaction": synthese(avant),
        "par_source": par("source"),
        "par_etape": par("etape"),
        "par_ampleur": par("ampleur"),
        "probabilites": probabilites(alertes),
        "detail": sorted(details, key=lambda d: (d["date"], d["information"]), reverse=True)[:DETAIL_MAX],
    }



# ---------------------------------------------------------------------------

def charger(nom, defaut=None):
    chemin = os.path.join(ICI, nom)
    if not os.path.exists(chemin):
        return defaut
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--depuis-historique", default="2019-01-01", help="debut de l'etude des communiques AMF")
    p.add_argument("--sans-historique", action="store_true",
                   help="ne recalcule pas l'etude des communiques AMF (reprend la precedente)")
    args = p.parse_args()

    entreprises = charger("referentiel.json")["entreprises"]
    # Alertes : archive complete, completee par alertes.json (au cas ou
    # l'archive serait en retard).
    alertes = {a["id"]: a for a in (charger("historique.json") or {}).get("alertes", [])}
    for a in (charger("alertes.json") or {}).get("alertes", []):
        alertes.setdefault(a["id"], a)
    alertes = list(alertes.values())

    limite = limite_cotation()
    debut = min([args.depuis_historique] + [a["date"] for a in alertes])
    debut = (dt.date.fromisoformat(debut) - dt.timedelta(days=420)).isoformat()
    print(f"Cours depuis {debut} (dernière clôture prise en compte : {limite})…", file=sys.stderr)
    indice = telecharger_cours(INDICE, debut, limite)
    cal = Calendrier(sorted(indice))
    tickers = sorted({e["ticker"] for e in entreprises} | {e["ticker"] for a in alertes for e in a["entreprises"]})
    series, erreurs = {INDICE: Serie(indice, indice)}, {}
    for t in tickers:
        try:
            series[t] = Serie(telecharger_cours(t, debut, limite), indice)
        except Exception as e:  # une action illisible n'arrete pas le calcul
            erreurs[t] = str(e)[:200]
    if erreurs:
        print(f"Cours indisponibles : {erreurs}", file=sys.stderr)

    precedent = charger("statistiques.json") or {}
    if args.sans_historique:
        histo = precedent.get("historique_amf")
    else:
        print("Étude des communiqués AMF…", file=sys.stderr)
        try:
            calendrier = (charger("alertes.json") or {}).get("calendrier", [])
            histo = etude(entreprises, cal, series, args.depuis_historique, calendrier=calendrier)
        except Exception as e:
            print(f"Étude des communiqués AMF indisponible : {e}", file=sys.stderr)
            histo = precedent.get("historique_amf")

    stats = statistiques_alertes(alertes, cal, series)
    sortie = {
        "version": 1,
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "derniere_seance": cal.dates[-1],
        "methode": {"indice": "CAC 40", "estimation": ESTIMATION, "seuil_net": SEUIL_NET,
                    "seuil_fort": SEUIL_FORT, "cloture": "%02d:%02d" % CLOTURE, "macro": MACRO,
                    "seances_apres": APRES},
        "cours_indisponibles": sorted(erreurs),
        "alertes": stats,
        "historique_amf": histo,
    }
    with open(os.path.join(ICI, "statistiques.json"), "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
        f.write("\n")

    s = stats["synthese"]
    sens = s.get("sens") or {}
    print(f"statistiques.json : {stats['n_alertes']} alerte(s), {s['n']} entreprise(s) notée(s)"
          + (f", {s['points']['total']:+d} point(s), sens juste {sens['justes']}/{sens['justes'] + sens['faux']}"
             if s["n"] else "")
          + f" ; en attente : {stats['statuts'].get('attente', 0)}."
          + (f" Communiqués AMF : {histo['n_evenements']} événements depuis {histo['depuis']}, "
             f"{histo['resultats']['n']} publications de résultats." if histo and histo.get("version") == 2
             else ""))


if __name__ == "__main__":
    main()
