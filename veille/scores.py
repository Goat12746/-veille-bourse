#!/usr/bin/env python3
"""Scores des objectifs de cours des analystes (onglet Statistiques > Scores) :
a l'horizon de 12 mois, le cours a-t-il rejoint l'objectif ?

Pour chaque entreprise et chaque debut de mois (premiere seance du mois),
l'objectif moyen du consensus ce jour-la (objectifs.json : Zonebourse, puis
releve quotidien Yahoo) est compare au cours 12 mois plus tard (premiere
seance a 365 jours ou plus) :
  - potentiel : objectif / cours du jour - 1 (ce qu'annoncaient les analystes) ;
  - variation : cours a l'echeance / cours du jour - 1 (ce qui s'est passe),
    hors dividendes, comme l'objectif ; les dividendes detaches pendant la
    periode sont donnes a part ;
  - ecart a l'objectif : cours a l'echeance / objectif - 1 (negatif : objectif
    trop optimiste) ;
  - objectif atteint : le cours a touche l'objectif au moins une fois (cloture)
    pendant les 12 mois ;
  - bon sens : le cours a bouge dans le sens annonce (hausse si l'objectif
    etait au-dessus du cours, baisse sinon) ;
  - face au CAC 40 : variation de l'action moins celle de l'indice.
Les observations mensuelles d'une meme entreprise se chevauchent (fenetres de
12 mois) : elles ne sont pas independantes, d'ou des medianes et des
proportions sans test statistique.

Cours : Yahoo Finance, clotures non ajustees des dividendes (comparables a un
objectif de cours), ajustees des divisions d'actions.

L'objectif median n'a pas d'historique gratuit : il est releve chaque jour
depuis le 1er octobre 2026 (Yahoo) et sera note a partir d'octobre 2027.

Ecrit scores.json. Usage : python scores.py
  python scores.py --univers monde   autres indices de l'application (objectifs_monde.json
  -> scores_monde.json et scores_monde/), a la main
Bibliotheque standard uniquement.
"""

import bisect
import datetime as dt
import json
import os
import sys
import time
import urllib.parse

from collecte import _yahoo_get
from mesures import INDICE, arrondi, limite_cotation, mediane, moyenne

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, ".cache", "cours_bruts")
HORIZON = 365  # jours
ECHEANCE_MAX = 10  # jours de tolerance pour trouver la seance de l'echeance
MIN_OBS_CORRECTION = 12  # observations echues pour corriger le potentiel actuel
POTENTIEL_ABERRANT = (-0.7, 2.0)  # lecture ou division d'actions douteuse : observation ecartee
TRANCHES = [("baisse", -1e9, 0), ("0-10", 0, 10), ("10-20", 10, 20), ("20-30", 20, 30), ("30+", 30, 1e9)]
EN_COURS_MOIS = 12


# ---------------------------------------------------------------------------
# Cours
# ---------------------------------------------------------------------------

def cours_bruts(ticker, debut, limite):
    """({jour: cloture}, {jour: dividende}, devise) : clotures non ajustees
    des dividendes. Cache d'un jour de cotation dans .cache/cours_bruts."""
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, ticker.replace("^", "_") + ".json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("limite") == limite and doc.get("debut", "9999") <= debut:
            return doc["clotures"], doc["dividendes"], doc.get("devise")
    p1 = int(dt.datetime.fromisoformat(debut).replace(tzinfo=dt.timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
           f"?period1={p1}&period2={int(time.time())}&interval=1d&events=div%2Csplit")
    r = json.loads(_yahoo_get(url, None))["chart"]["result"][0]
    decalage = r["meta"].get("gmtoffset") or 0

    def jour(ts):
        return dt.datetime.fromtimestamp(ts + decalage, dt.timezone.utc).date().isoformat()

    q = r["indicators"]["quote"][0]
    clotures = {jour(ts): round(c, 4) for ts, c in zip(r.get("timestamp") or [], q["close"])
                if c and jour(ts) <= limite}
    dividendes = {}
    for d in ((r.get("events") or {}).get("dividends") or {}).values():
        j = jour(d["date"])
        dividendes[j] = round(dividendes.get(j, 0) + d["amount"], 4)
    devise = r["meta"].get("currency")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump({"debut": debut, "limite": limite, "devise": devise, "clotures": clotures,
                   "dividendes": dividendes}, f)
    time.sleep(0.2)
    return clotures, dividendes, devise


class Escalier:
    """Valeur en vigueur a une date, d'apres une liste de changements."""

    def __init__(self, changements):
        self.jours = [j for j, _ in changements]
        self.valeurs = [v for _, v in changements]

    def __call__(self, jour):
        i = bisect.bisect_right(self.jours, jour) - 1
        return self.valeurs[i] if i >= 0 else None


def objectif_moyen(fiche, mult=None, mult_yahoo=1):
    """Changements de l'objectif moyen, dans la devise du cours : Zonebourse,
    puis le releve Yahoo au-dela de la derniere date lue chez Zonebourse.
    `mult(jour)` convertit la devise de Zonebourse dans celle du cours ;
    `mult_yahoo` corrige un objectif Yahoo en livres pour un cours en pence."""
    zb = [[j, round(v * mult(j), 2) if mult else v] for j, v in fiche.get("moyen", [])]
    zb = [x for x in zb if x[1] and x[1] == x[1]]
    fin = zb[-1][0] if zb else ""
    yahoo = [[r[0], round(r[1] * mult_yahoo, 2)] for r in fiche.get("yahoo", []) if r[0] > fin and r[1]]
    return zb + yahoo


def conversion(devise, devise_cours, debut, limite):
    """Fonction jour -> nombre d'unites de la devise du cours pour une unite
    de `devise` (Yahoo, ex. USDEUR=X ; cours de Londres en pence : GBp), ou
    None si les devises sont les memes."""
    devise = "GBp" if devise == "GBX" else devise
    if devise == devise_cours:
        return None
    if {devise, devise_cours} == {"GBP", "GBp"}:
        f = 100 if devise_cours == "GBp" else 0.01
        return lambda j: f
    base = "GBP" if devise_cours == "GBp" else devise_cours
    source, f_source = ("GBP", 0.01) if devise == "GBp" else (devise, 1)
    taux = Escalier(sorted(cours_bruts(f"{source}{base}=X", debut, limite)[0].items()))
    f = (100 if devise_cours == "GBp" else 1) * f_source
    return lambda j: (taux(j) or float("nan")) * f


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def observations(cours, dividendes, indice, objectif, debut_objectif):
    """Une observation par mois : premiere seance du mois ou l'objectif est
    connu. Retourne (echues, en_cours)."""
    jours = sorted(cours)
    if not jours:
        return [], []
    echues, en_cours, vus = [], [], set()
    for j in jours:
        if j < debut_objectif or j[:7] in vus:
            continue
        vus.add(j[:7])
        t, p0 = objectif(j), cours[j]
        if not t or not p0:
            continue
        pot = t / p0 - 1
        if not POTENTIEL_ABERRANT[0] < pot < POTENTIEL_ABERRANT[1]:
            continue
        cible = (dt.date.fromisoformat(j) + dt.timedelta(days=HORIZON)).isoformat()
        k = bisect.bisect_left(jours, cible)
        obs = {"jour": j, "objectif": t, "cours": p0, "potentiel": pot}
        if k >= len(jours) or (dt.date.fromisoformat(jours[k]) - dt.date.fromisoformat(cible)).days > ECHEANCE_MAX:
            obs["cours_actuel"] = cours[jours[-1]]
            obs["variation"] = cours[jours[-1]] / p0 - 1
            obs["atteint"] = _atteint(cours, jours, j, jours[-1], t, p0)
            en_cours.append(obs)
            continue
        fin = jours[k]
        pe = cours[fin]
        i0, i1 = indice.get(j), indice.get(fin)
        obs.update({
            "echeance": fin, "cours_echeance": pe,
            "variation": pe / p0 - 1,
            "ecart": pe / t - 1,
            "atteint": _atteint(cours, jours, j, fin, t, p0),
            "dividendes": sum(v for d, v in dividendes.items() if j < d <= fin) / p0,
            "cac": (i1 / i0 - 1) if i0 and i1 else None,
        })
        obs["bon_sens"] = (obs["variation"] > 0) == (pot > 0)
        echues.append(obs)
    return echues, en_cours


def _atteint(cours, jours, debut, fin, t, p0):
    a, b = bisect.bisect_right(jours, debut), bisect.bisect_right(jours, fin)
    periode = [cours[d] for d in jours[a:b]]
    if not periode:
        return False
    return max(periode) >= t if t >= p0 else min(periode) <= t


def pct(x, n=1):
    return None if x is None else round(x * 100, n)


def part(v):
    return None if not v else round(100 * sum(1 for x in v if x) / len(v), 1)


def quartiles(v):
    v = sorted(v)
    if len(v) < 4:
        return None
    return [round(v[len(v) // 4] * 100, 1), round(mediane(v) * 100, 1), round(v[(3 * len(v)) // 4] * 100, 1)]


def resume(obs):
    """Indicateurs d'un ensemble d'observations echues."""
    if not obs:
        return {"n": 0}
    exces = [o["variation"] - o["cac"] for o in obs if o.get("cac") is not None]
    return {
        "n": len(obs),
        "n_entreprises": len({o.get("ticker") for o in obs}),
        "potentiel_median": pct(mediane([o["potentiel"] for o in obs])),
        "variation_mediane": pct(mediane([o["variation"] for o in obs])),
        "ecart_median": pct(mediane([o["ecart"] for o in obs])),
        "ecart_moyen": pct(moyenne([o["ecart"] for o in obs])),
        "ecart_absolu_median": pct(mediane([abs(o["ecart"]) for o in obs])),
        "ecart_quartiles": quartiles([o["ecart"] for o in obs]),
        "atteint_pct": part([o["atteint"] for o in obs]),
        "au_dessus_echeance_pct": part([o["ecart"] >= 0 for o in obs]),
        "bon_sens_pct": part([o["bon_sens"] for o in obs]),
        "dividendes_median": pct(mediane([o["dividendes"] for o in obs])),
        "face_cac_median": pct(mediane(exces)) if exces else None,
    }


def _public(o):
    out = {"jour": o["jour"], "objectif": round(o["objectif"], 2), "cours": round(o["cours"], 2),
           "potentiel": pct(o["potentiel"]), "variation": pct(o["variation"]), "atteint": o["atteint"]}
    if "echeance" in o:
        out.update({"echeance": o["echeance"], "cours_echeance": round(o["cours_echeance"], 2),
                    "ecart": pct(o["ecart"]), "bon_sens": o["bon_sens"], "dividendes": pct(o["dividendes"])})
    else:
        out["cours_actuel"] = round(o["cours_actuel"], 2)
    return out


# ---------------------------------------------------------------------------

def calculer(entreprises, objectifs, indices, limite):
    """Fiches et observations echues des entreprises. `indices` : {zone:
    {jour: cloture}} de l'indice de comparaison."""
    debut = min([f["moyen"][0][0] for f in objectifs["entreprises"].values() if f.get("moyen")] + [limite])
    toutes, fiches, erreurs = [], [], {}
    for e in entreprises:
        t = e["ticker"]
        fo = objectifs["entreprises"].get(t) or {}
        yahoo = fo.get("yahoo") or []
        if not fo.get("moyen") and not yahoo:
            continue
        depart = min([x[0] for x in fo.get("moyen", [])[:1]] + [r[0] for r in yahoo[:1]] + [limite])
        try:
            cours, dividendes, devise = cours_bruts(t, depart, limite)
        except Exception as ex:
            erreurs[t] = f"cours illisibles : {str(ex)[:100]}"
            continue
        devise = devise or "EUR"
        # Historique Zonebourse garde seulement si son dernier objectif rejoint
        # (a 10 % pres) l'objectif moyen de Yahoo : ecarte une lecture fausse
        # du graphique ou un historique non ajuste (augmentation de capital,
        # restructuration). Consensus tenu dans une autre devise (dollar pour
        # TotalEnergies, HSBC, Novartis...) : converti au cours du jour.
        mult, mult_yahoo, source = None, 1, fo
        ref = next((r[1] for r in reversed(yahoo) if r[1]), None)
        if fo.get("moyen"):
            devise_zb = fo.get("devise") or devise
            dernier_zb = fo["moyen"][-1]
            try:
                mult = conversion(devise_zb, devise, debut, limite)
                converti = dernier_zb[1] * (mult(dernier_zb[0]) if mult else 1)
                if converti != converti:
                    raise ValueError("pas de cours de change")
            except Exception as ex:
                converti = None
                erreurs[t] = f"historique en {devise_zb}, change illisible : {str(ex)[:80]}"
            r = converti / ref if converti and ref else None
            if r and 90 < r < 110:
                mult_yahoo = 100  # Yahoo donne l'objectif en livres, le cours en pence
            elif r and 0.009 < r < 0.011:
                mult_yahoo = 0.01
            elif converti is None or (r and not 0.9 < r < 1.1):
                if converti is not None:
                    erreurs[t] = (f"historique écarté : dernier objectif {dernier_zb[1]} {devise_zb}"
                                  + (f" (soit {converti:.2f} {devise})" if mult else "")
                                  + f" contre {ref} {devise} chez Yahoo")
                source, mult = {"yahoo": yahoo}, None
        changements = objectif_moyen(source, mult, mult_yahoo)
        if not changements:
            continue
        indice = indices.get(e.get("zone", "france"), {})
        echues, en_cours = observations(cours, dividendes, indice, Escalier(changements), changements[0][0])
        for o in echues:
            o["ticker"] = t
        toutes += echues
        jours = sorted(cours)
        fiche = {"ticker": t, "nom": e["nom"], "indice": e.get("indice") or ", ".join(e.get("indices", [])),
                 "secteur": e.get("secteur"), "devise": devise, "depuis": changements[0][0], **resume(echues)}
        if e.get("zone"):
            fiche["zone"] = e["zone"]
            fiche["indices"] = e.get("indices", [])
        fiche.pop("n_entreprises", None)
        # Consensus actuel (dernier releve Yahoo, sinon Zonebourse).
        if jours:
            p = cours[jours[-1]]
            dernier = yahoo[-1] if yahoo else None

            def y(x):
                return round(x * mult_yahoo, 2) if isinstance(x, (int, float)) else x

            moyen = (y(dernier[1]) if dernier else None) or changements[-1][1]
            actuel = {"jour": jours[-1], "cours": round(p, 2), "objectif_moyen": moyen,
                      "potentiel_moyen": pct(moyen / p - 1)}
            if dernier:
                actuel.update({"releve": dernier[0], "objectif_median": y(dernier[2]), "haut": y(dernier[3]),
                               "bas": y(dernier[4]), "n_analystes": dernier[5], "note": dernier[6],
                               "recommandations": dernier[7]})
                if dernier[2]:
                    actuel["potentiel_median"] = pct(y(dernier[2]) / p - 1)
            # Si l'objectif se trompe comme d'habitude pour cette entreprise.
            if len(echues) >= MIN_OBS_CORRECTION:
                corr = mediane([o["ecart"] for o in echues])
                actuel["potentiel_corrige"] = pct(moyen * (1 + corr) / p - 1)
            fiche["actuel"] = actuel
        fiche["derniere_echue"] = _public(echues[-1]) if echues else None
        fiche["en_cours"] = [_public(o) for o in en_cours[-EN_COURS_MOIS:]]
        # Courbes (une valeur par mois) : cours et objectif moyen.
        mois = {}
        for j in jours:
            if j >= changements[0][0]:
                mois.setdefault(j[:7], j)
        fiche["courbe"] = [[j, round(cours[j], 2), Escalier(changements)(j)] for j in sorted(mois.values())]
        fiche["observations"] = [_public(o) for o in echues]
        fiches.append(fiche)
    return toutes, fiches, erreurs


def groupes(toutes, fiches):
    """Tranches de potentiel, annees et secteurs."""
    tranches = [dict(resume([o for o in toutes if a <= o["potentiel"] * 100 < b]), id=nom)
                for nom, a, b in TRANCHES]
    annees = [dict(resume([o for o in toutes if o["jour"][:4] == an]), annee=an)
              for an in sorted({o["jour"][:4] for o in toutes})]
    secteur_de = {f["ticker"]: f.get("secteur") or "" for f in fiches}
    secteurs = []
    for s in sorted(set(secteur_de.values())):
        w = [o for o in toutes if secteur_de.get(o["ticker"]) == s]
        if w:
            secteurs.append(dict(resume(w), secteur=s or "Autre"))
    secteurs.sort(key=lambda x: -x["n"])
    return tranches, annees, secteurs


def methode(objectifs, sources):
    return {
        "horizon_jours": HORIZON,
        "objectif": "moyen",
        "sources": sources,
        "median_depuis": min((f["yahoo"][0][0] for f in objectifs["entreprises"].values() if f.get("yahoo")),
                             default=None),
        "maj_zonebourse": objectifs.get("maj_zonebourse"),
        "maj_yahoo": objectifs.get("maj_yahoo"),
    }


def ligne(nom, toutes, fiches, erreurs):
    s = resume(toutes)
    return (f"{nom} : {s['n']} objectif(s) échu(s) sur {len(fiches)} entreprise(s)"
            + (f", écart médian à l'objectif {s['ecart_median']} %, atteint {s['atteint_pct']} %, "
               f"bon sens {s['bon_sens_pct']} %" if s["n"] else "")
            + (f" ; écartées : {sorted(erreurs)}" if erreurs else "") + ".")


def main_veille():
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        entreprises = json.load(f)["entreprises"]
    with open(os.path.join(ICI, "objectifs.json"), encoding="utf-8") as f:
        objectifs = json.load(f)
    limite = limite_cotation()
    debut = min([f["moyen"][0][0] for f in objectifs["entreprises"].values() if f.get("moyen")] + [limite])
    print(f"Cours depuis {debut} (dernière clôture : {limite})…", file=sys.stderr)
    indice, _, _ = cours_bruts(INDICE, debut, limite)
    toutes, fiches, erreurs = calculer(entreprises, objectifs, {"france": indice}, limite)
    tranches, annees, secteurs = groupes(toutes, fiches)
    sortie = {
        "version": 1,
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "derniere_seance": indice and max(indice),
        "methode": methode(objectifs, "Objectif moyen : Zonebourse / S&P Global Market Intelligence (historique), "
                                      "Yahoo Finance (relevé quotidien). Cours : Yahoo Finance."),
        "ecartees": erreurs,
        "synthese": resume(toutes),
        "tranches": tranches,
        "annees": annees,
        "secteurs": secteurs,
        "entreprises": sorted(fiches, key=lambda f: f["nom"]),
    }
    with open(os.path.join(ICI, "scores.json"), "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(ligne("scores.json", toutes, fiches, erreurs))


RESERVES = {"CON", "PRN", "AUX", "NUL"} | {f"{x}{i}" for x in ("COM", "LPT") for i in range(1, 10)}


def fichier_detail(ticker):
    """CON.DE -> CON_DE.json ; noms reserves de Windows (CON, AUX...) suivis
    d'un _. Meme regle dans l'application (scores_service.dart)."""
    base = ticker.replace(".", "_")
    return base + ("_" if base.upper() in RESERVES else "") + ".json"


ZONES = {"europe": ("Europe", "^STOXX", "STOXX Europe 600"), "usa": ("États-Unis", "^GSPC", "S&P 500")}
DETAIL = ("derniere_echue", "en_cours", "courbe", "observations")


def main_monde():
    """Autres indices de l'application : scores_monde.json (synthese par zone et
    resume de chaque entreprise) et scores_monde/<ticker>.json (detail, charge
    par l'application a l'ouverture de la page de l'entreprise)."""
    with open(os.path.join(ICI, "univers_objectifs.json"), encoding="utf-8") as f:
        entreprises = json.load(f)["entreprises"]
    with open(os.path.join(ICI, "objectifs_monde.json"), encoding="utf-8") as f:
        objectifs = json.load(f)
    limite = limite_cotation()
    debut = min([f["moyen"][0][0] for f in objectifs["entreprises"].values() if f.get("moyen")] + [limite])
    indices = {z: cours_bruts(sym, debut, limite)[0] for z, (_, sym, _) in ZONES.items()}
    zones, toutes_fiches, erreurs = [], [], {}
    for z, (libelle, _, nom_indice) in ZONES.items():
        print(f"{libelle}…", file=sys.stderr)
        toutes, fiches, err = calculer([e for e in entreprises if e["zone"] == z], objectifs, indices, limite)
        erreurs.update(err)
        tranches, annees, secteurs = groupes(toutes, fiches)
        zones.append({"id": z, "libelle": libelle, "indice_reference": nom_indice, "synthese": resume(toutes),
                      "tranches": tranches, "annees": annees, "secteurs": secteurs})
        toutes_fiches += fiches
        s = resume(toutes)
        print(f"{libelle} : {s['n']} objectif(s) échu(s) sur {len(fiches)} entreprise(s)"
              + (f", écart médian {s['ecart_median']} %, atteint {s['atteint_pct']} %" if s["n"] else "")
              + f" ; écartées : {len(err)}.")
    dossier = os.path.join(ICI, "scores_monde")
    os.makedirs(dossier, exist_ok=True)
    for nom in os.listdir(dossier):
        os.remove(os.path.join(dossier, nom))
    for fiche in toutes_fiches:
        with open(os.path.join(dossier, fichier_detail(fiche["ticker"])), "w", encoding="utf-8") as f:
            json.dump({k: fiche[k] for k in DETAIL}, f, ensure_ascii=False, separators=(",", ":"))
    sortie = {
        "version": 1,
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "derniere_seance": max(max(v) for v in indices.values() if v),
        "methode": methode(objectifs, "Objectif moyen : Zonebourse / S&P Global Market Intelligence (lu une "
                                      "fois). Cours et consensus du jour : Yahoo Finance. Calcul figé, relancé "
                                      "à la main."),
        "ecartees": erreurs,
        "zones": zones,
        "entreprises": sorted(({k: v for k, v in f.items() if k not in DETAIL} for f in toutes_fiches),
                              key=lambda f: f["nom"]),
    }
    with open(os.path.join(ICI, "scores_monde.json"), "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"scores_monde.json : {len(toutes_fiches)} entreprise(s), détail dans scores_monde/.")


def main():
    if sys.argv[1:3] == ["--univers", "monde"]:
        main_monde()
    else:
        main_veille()


if __name__ == "__main__":
    main()
