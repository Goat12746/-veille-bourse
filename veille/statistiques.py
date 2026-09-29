#!/usr/bin/env python3
"""Statistiques de la veille : les alertes annoncent-elles la reaction des
cours ? Ecrit statistiques.json, lu par l'onglet Statistiques de
l'application.

Methode : etude d'evenements (MacKinlay, 1997), pour chaque entreprise d'une
alerte :
  - modele de marche estime sur les 250 seances qui precedent l'evenement
    (rendement de l'action = alpha + beta x rendement du CAC 40) ;
  - ecart (rendement anormal) le jour de la reaction : rendement observe
    moins rendement attendu d'apres le CAC 40 ;
  - ecart en sigma (ecart habituel d'une seance, residus du modele) :
    bruit sous 1, net de 1 a 2, fort au-dela de 2 ;
  - points : sens juste +1, faux -1, dans le bruit 0 ; ampleur conforme +1,
    un cran d'ecart 0, fausse alerte ou impact sous-estime -1 ;
  - tests : t sur les ecarts standardises (Boehmer, Musumeci et Poulsen,
    1991), test du signe, test de rang (Corrado, 1989), correlation de rang
    entre ampleur annoncee et mouvement observe (Spearman).
Jour de la reaction : premiere seance qui cloture (17 h 35) apres la
publication ; sans heure connue, le jour de publication et le suivant.

Reference sans IA : meme mesure sur l'historique des communiques AMF des
entreprises suivies (depuis 2019), par type de communique d'apres le titre.

Usage : python statistiques.py [--depuis-historique 2019-01-01] [--sans-historique]
Bibliotheque standard uniquement.
"""

import argparse
import bisect
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse

from collecte import _yahoo_get, http_get, normaliser

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, ".cache")
INDICE = "^FCHI"  # CAC 40 (Yahoo n'a pas d'historique du SBF 120)
AMF_EXPORT = ("https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/"
              "datasets/flux-amf-new-prod/exports/json")

ESTIMATION = 250  # seances du modele de marche
ESTIMATION_MIN = 120
ECART_ESTIMATION = 10  # seances ecartees juste avant l'evenement (fuites)
CLOTURE = (17, 35)  # fin du fixing de cloture d'Euronext Paris
OUVERTURE = (9, 0)
SEUIL_NET, SEUIL_FORT = 1.0, 2.0
MACRO = 10  # alerte touchant au moins 10 entreprises : comparee a l'indice
APRES = 5  # seances mesurees apres la publication de l'alerte
SUIVI = (5, 20)  # ecarts cumules affiches a J+5 et J+20
DETAIL_MAX = 150  # alertes detaillees dans le fichier (les indicateurs portent sur toutes)
SENS_NOTES = ("positif", "negatif")
AMPLEURS_NOTEES = ("faible", "moyenne", "forte")
# Etapes d'une mesure encore incertaine au moment de l'alerte : leur
# probabilite est jugee une fois la mesure adoptee ou rejetee.
ETAPES_INCERTAINES = ("rumeur", "annonce", "depot", "adopte_commission", "adopte_seance")

AMF_TYPES = ["Informations privilégiées", "Information financière trimestrielle",
             "Rapports financiers et d'audit semestriels/examens réduits",
             "Rapports financiers et d'audit annuels",
             "Communiqués publiés en période d'offre publique d'acquisition"]
AMF_RESULTATS = {"Information financière trimestrielle",
                 "Rapports financiers et d'audit semestriels/examens réduits",
                 "Rapports financiers et d'audit annuels"}
# Type de communique d'apres le titre (sans accents, minuscules), par ordre
# de priorite : un communique de resultats qui releve les objectifs est
# classe en revision.
CATEGORIES = [
    ("revision", "Révision des objectifs, pré-annonces", [
        "releve ses", "releve son", "releve sa", "releve l'ensemble", "rehausse", "a la hausse ses",
        "abaisse", "a la baisse ses", "revise ses", "revise son", "revise sa", "revoit ses", "revoit son",
        "revoit sa", "ajuste ses", "ajuste son", "ajuste sa", "actualise ses", "avertissement",
        "preliminaire", "nouveaux objectifs", "objectifs revus", "perspectives revues"]),
    ("resultats", "Résultats et chiffre d'affaires", [
        "resultat", "chiffre d'affaires", "ventes", "trimestre", "semestre", "annuel", "9 mois",
        "neuf mois", "information financiere", "performance", "activite du", "activite au"]),
    ("trafic", "Trafic et activité mensuelle", ["trafic"]),
    ("operation", "Acquisitions, cessions, fusions", [
        "acquisition", "acquiert", "acquerir", "cession", "cede ", "ceder", "fusion", "offre publique",
        "rapprochement", "negociations exclusives", "prise de participation", "rachat de la", "rachat du",
        "rachat des activites", "scission", "introduction en bourse", "entree en negociation", "rumeur"]),
    ("juridique", "Juridique et réglementaire", [
        "enquete", "amende", "litige", "condamn", "proces", "tribunal", "sanction", "autorite de la concurrence",
        "commission europeenne", "plainte", "contentieux", "arbitrage", "cour d'appel", "jugement"]),
    ("financement", "Financement et capital", [
        "emission", "obligat", "augmentation de capital", "placement", "financement", "credit", "notation",
        "oceane", "ornane", "subordonne", "hybride", "emprunt", "souscription"]),
    ("actionnaires", "Dividendes et rachats d'actions", [
        "dividende", "rachat d'actions", "actions propres", "retour aux actionnaires", "annulation d'actions",
        "programme de rachat", "distribution"]),
    ("strategie", "Stratégie et restructuration", [
        "strategi", "feuille de route", "capital markets day", "journee investisseurs", "restructuration",
        "reorganisation", "transformation", "plan social", "suppression de postes"]),
    ("commercial", "Contrats, produits et essais cliniques", [
        "commande", "contrat", "remporte", "selectionne", "partenariat", "accord", "lance ", "lancement",
        "inaugure", "approbation", "approuv", "autorisation", "fda", "clinique", "essai", "phase ", "patients",
        "brevet", "fournira", "livre ", "s'associe", "collabor"]),
    ("gouvernance", "Gouvernance et dirigeants", [
        "nomination", "nomme", "directeur general", "president", "conseil d'administration",
        "assemblee generale", "gouvernance", "depart", "succession", "administrat", "dirigeant"]),
]
# Declarations recurrentes classees par erreur en information privilegiee :
# pas une nouvelle, ecartees de l'etude.
TITRES_EXCLUS = ["nombre total de droits de vote", "transactions sur actions propres",
                 "document d'enregistrement universel", "document de reference", "mise a disposition",
                 "version anglaise"]
# "resultats de l'offre", "resultat des votes"... ne sont pas des resultats.
FAUX_RESULTATS = ["resultat de l'offre", "resultats de l'offre", "resultat du rachat", "resultats du rachat",
                  "resultat des votes", "resultats des votes", "resultat de l'assemblee",
                  "resultats de l'assemblee", "resultat de l'augmentation", "resultats de l'augmentation"]
# Sens d'un titre de communique (methode simple, sans IA).
POSITIF = ["releve", "rehausse", "a la hausse", "en hausse", "hausse de", "croissance", "record", "progression",
           "progresse", "solide", "superieur", "depasse", "amelior", "succes", "remporte", "accelere",
           "acceleration", "robuste", "excellent"]
NEGATIF = ["abaisse", "a la baisse", "en baisse", "baisse de", "recul", "repli", "perte", "deprecia",
           "avertissement", "difficile", "degrad", "ralentissement", "suspend", "inferieur", "decline",
           "chute", "contraction"]


# ---------------------------------------------------------------------------
# Dates : heure de Paris, seances de cotation
# ---------------------------------------------------------------------------

def _dernier_dimanche(annee, mois):
    d = dt.datetime(annee, mois + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def heure_paris(iso):
    """Instant ISO 8601 -> date et heure de Paris (sans fuseau). Regle de
    l'heure d'ete europeenne codee ici : zoneinfo n'a pas de base de fuseaux
    sous Windows sans paquet supplementaire."""
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        return t
    u = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    ete = _dernier_dimanche(u.year, 3).replace(hour=1) <= u < _dernier_dimanche(u.year, 10).replace(hour=1)
    return u + dt.timedelta(hours=2 if ete else 1)


def limite_cotation():
    """Derniere date dont la cloture est connue : aujourd'hui apres 17 h 45
    (heure de Paris), hier sinon (la barre du jour est provisoire)."""
    maintenant = heure_paris(dt.datetime.now(dt.timezone.utc).isoformat())
    jour = maintenant.date()
    if (maintenant.hour, maintenant.minute) < (17, 45):
        jour -= dt.timedelta(days=1)
    return jour.isoformat()


class Calendrier:
    """Seances de cotation (celles du CAC 40)."""

    def __init__(self, dates):
        self.dates = dates
        self.pos = {d: i for i, d in enumerate(dates)}

    def suivante(self, jour):
        """Premiere seance strictement apres `jour` (None : pas encore cotee)."""
        i = bisect.bisect_right(self.dates, jour)
        return self.dates[i] if i < len(self.dates) else None

    def reaction(self, t):
        """Premiere seance dont la cloture suit l'instant t (heure de Paris)."""
        jour = t.date().isoformat()
        if jour in self.pos and (t.hour, t.minute) < CLOTURE:
            return jour
        return self.suivante(jour)

    def ouverture(self, t):
        """Premiere seance dont l'ouverture suit l'instant t."""
        jour = t.date().isoformat()
        if jour in self.pos and (t.hour, t.minute) < OUVERTURE:
            return jour
        return self.suivante(jour)


# ---------------------------------------------------------------------------
# Cours (Yahoo Finance), modele de marche
# ---------------------------------------------------------------------------

def telecharger_cours(ticker, debut, limite):
    """Seances d'un titre : {date: [ouverture, cloture]} ajustees des
    dividendes et divisions, jusqu'a `limite` incluse. Cache d'un jour de
    cotation dans .cache/cours."""
    dossier = os.path.join(CACHE, "cours")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, ticker.replace("^", "_") + ".json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("limite") == limite and doc.get("debut", "9999") <= debut:
            return doc["seances"]
    p1 = int(dt.datetime.fromisoformat(debut).replace(tzinfo=dt.timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
           f"?period1={p1}&period2={int(time.time())}&interval=1d&events=div%2Csplit")
    r = json.loads(_yahoo_get(url, None))["chart"]["result"][0]
    decalage = r["meta"].get("gmtoffset") or 0
    q = r["indicators"]["quote"][0]
    ajustees = ((r["indicators"].get("adjclose") or [{}])[0].get("adjclose")) or q["close"]
    seances = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        o, c, a = q["open"][i], q["close"][i], ajustees[i]
        jour = dt.datetime.fromtimestamp(ts + decalage, dt.timezone.utc).date().isoformat()
        if not c or not a or jour > limite:
            continue
        k = a / c  # facteur d'ajustement du jour, applique aussi a l'ouverture
        seances[jour] = [round(o * k, 6) if o else None, round(a, 6)]
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump({"debut": debut, "limite": limite, "seances": seances}, f)
    time.sleep(0.2)  # Yahoo limite vite le nombre de requetes
    return seances


class Serie:
    """Rendements quotidiens d'une action et du CAC 40, sur leurs seances
    communes."""

    def __init__(self, action, indice):
        self.dates = sorted(set(action) & set(indice))
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self.clo = [action[d][1] for d in self.dates]
        self.ouv = [action[d][0] for d in self.dates]
        self.clo_m = [indice[d][1] for d in self.dates]
        self.ouv_m = [indice[d][0] for d in self.dates]
        self.r = [0.0] + [self.clo[i] / self.clo[i - 1] - 1 for i in range(1, len(self.dates))]
        self.m = [0.0] + [self.clo_m[i] / self.clo_m[i - 1] - 1 for i in range(1, len(self.dates))]

    def modele(self, s):
        """Modele de marche estime avant la seance d'indice s : (alpha, beta,
        sigma, residus, sigma de l'indice), None si l'historique est trop court."""
        debut, fin = max(1, s - ESTIMATION - ECART_ESTIMATION), s - ECART_ESTIMATION
        xs, ys = self.m[debut:fin], self.r[debut:fin]
        n = len(xs)
        if n < ESTIMATION_MIN:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            return None
        beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        alpha = my - beta * mx
        residus = [y - alpha - beta * x for x, y in zip(xs, ys)]
        sigma = math.sqrt(sum(e * e for e in residus) / (n - 2))
        sigma_m = math.sqrt(sxx / (n - 1))
        return alpha, beta, sigma, residus, sigma_m

    def ecart(self, mod, s, e):
        """Ecart cumule (rendement anormal) des seances s a e incluses."""
        alpha, beta = mod[0], mod[1]
        return sum(self.r[t] - alpha - beta * self.m[t] for t in range(s, e + 1))

    def mesure(self, mod, s, e):
        """Reaction sur les seances s a e : rendements, ecart, sigma, rang."""
        alpha, beta, sigma, residus, _ = mod
        n = e - s + 1
        car = self.ecart(mod, s, e)
        # Rang de l'ecart parmi les sommes de n residus consecutifs de la
        # periode d'estimation (test de rang, robuste aux queues epaisses).
        sommes = [sum(residus[i:i + n]) for i in range(len(residus) - n + 1)]
        rang = (1 + sum(1 for x in sommes if x < car)) / (len(sommes) + 2)
        return {
            "rendement_pct": pct(self.clo[e] / self.clo[s - 1] - 1),
            "indice_pct": pct(self.clo_m[e] / self.clo_m[s - 1] - 1),
            "beta": round(beta, 2),
            "sigma_pct": pct(sigma),
            "ecart_pct": pct(car),
            "z": round(car / (sigma * math.sqrt(n)), 2),
            "rang": round(rang, 3),
        }

    def apres(self, mod, e0):
        """Ecart de l'ouverture de la seance e0 a la cloture APRES seances
        plus tard : ce qu'on pouvait gagner en suivant l'alerte."""
        alpha, beta, sigma = mod[0], mod[1], mod[2]
        k = min(APRES, len(self.dates) - e0)
        if k <= 0 or not self.ouv[e0] or not self.ouv_m[e0]:
            return None
        car = (self.clo[e0] / self.ouv[e0] - 1) - beta * (self.clo_m[e0] / self.ouv_m[e0] - 1)
        if k > 1:
            car += self.ecart(mod, e0 + 1, e0 + k - 1)
        return {"debut": self.dates[e0], "seances": k, "complet": k == APRES,
                "rendement_pct": pct(self.clo[e0 + k - 1] / self.ouv[e0] - 1),
                "ecart_pct": pct(car), "z": round(car / (sigma * math.sqrt(k - 0.5)), 2)}


def pct(x):
    return round(100 * x, 2)


# ---------------------------------------------------------------------------
# Tests statistiques
# ---------------------------------------------------------------------------

def _betacf(a, b, x):
    """Fraction continue de la fonction beta incomplete (Numerical Recipes)."""
    petit = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > petit else petit)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)),
                   -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > petit else petit)
            c = 1 + aa / c
            c = c if abs(c) > petit else petit
            h *= d * c
        if abs(d * c - 1) < 3e-14:
            break
    return h


def _beta_incomplete(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(ln) * _betacf(a, b, x) / a
    return 1 - math.exp(ln) * _betacf(b, a, 1 - x) / b


def p_student(t, ddl):
    """p-valeur bilaterale d'une statistique t de Student."""
    return _beta_incomplete(ddl / 2, 0.5, ddl / (ddl + t * t))


def p_normale(z):
    return math.erfc(abs(z) / math.sqrt(2))


def p_binomiale(k, n):
    """Test bilateral de k succes sur n contre une chance sur deux."""
    if n == 0:
        return None
    queue = sum(math.comb(n, i) for i in range(min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * queue)


def moyenne(v):
    return sum(v) / len(v) if v else None


def test_t(v):
    """Moyenne, t et p-valeur (H0 : moyenne nulle)."""
    n = len(v)
    if n < 2:
        return moyenne(v), None, None
    m = moyenne(v)
    s = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    if s == 0:
        return m, None, None
    t = m / (s / math.sqrt(n))
    return m, t, p_student(t, n - 1)


def _rangs(v):
    ordre = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[ordre[j + 1]] == v[ordre[i]]:
            j += 1
        for k in range(i, j + 1):
            r[ordre[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    """Correlation de rang et p-valeur (approximation de Student)."""
    n = len(x)
    if n < 3:
        return None, None
    rx, ry = _rangs(x), _rangs(y)
    mx, my = moyenne(rx), moyenne(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None, None
    rho = sxy / math.sqrt(sxx * syy)
    if abs(rho) >= 1:
        return rho, 0.0
    return rho, p_student(rho * math.sqrt((n - 2) / (1 - rho * rho)), n - 2)


def arrondi(x, n=3):
    return None if x is None else round(x, n)


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
                o["apres"] = serie.apres(mod, serie.pos[e0])
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
# Reference : historique des communiques AMF
# ---------------------------------------------------------------------------

def communiques_amf(entreprises, depuis):
    """Communiques AMF en francais des entreprises suivies depuis `depuis`.
    Le passe ne change pas : une fois l'historique en cache
    (.cache/amf_historique.json), seuls les communiques des derniers jours
    sont telecharges (7 jours de recouvrement pour les depots tardifs).
    Historique complet si le cache manque ou si le referentiel a change."""
    chemin = os.path.join(CACHE, "amf_historique.json")
    isins = sorted(e["isin"] for e in entreprises)
    cle = lambda c: c.get("uin_idt_uin") or (c.get("informationdeposee_inf_dat_emt"),
                                            c.get("identificationsociete_iso_cd_isi"),
                                            c.get("informationdeposee_inf_tit_inf"))
    connus, debut = {}, depuis
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("depuis") == depuis and doc.get("isins") == isins and doc["communiques"]:
            connus = {cle(c): c for c in doc["communiques"]}
            if time.time() - os.path.getmtime(chemin) < 3600:
                return list(connus.values())
            dernier = max(c["informationdeposee_inf_dat_emt"] for c in connus.values())[:10]
            debut = (dt.date.fromisoformat(dernier) - dt.timedelta(days=7)).isoformat()
    liste = ", ".join(f"'{i}'" for i in isins)
    types = ", ".join(f'"{t}"' for t in AMF_TYPES)
    where = (f"identificationsociete_iso_cd_isi IN ({liste}) AND informationdeposee_inf_dat_emt >= '{debut}'"
             f" AND sous_type_d_information IN ({types}) AND informationdeposee_inf_lng_inf != 'Anglais'")
    params = {"where": where, "select": "uin_idt_uin,informationdeposee_inf_dat_emt,identificationsociete_iso_cd_isi,"
                                        "informationdeposee_inf_tit_inf,sous_type_d_information"}
    nouveaux = json.loads(http_get(AMF_EXPORT + "?" + urllib.parse.urlencode(params), timeout=300))
    connus.update({cle(c): c for c in nouveaux})
    os.makedirs(CACHE, exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump({"depuis": depuis, "isins": isins, "communiques": list(connus.values())}, f, ensure_ascii=False)
    print(f"Communiqués AMF : {len(nouveaux)} téléchargé(s) depuis le {debut}, {len(connus)} au total.",
          file=sys.stderr)
    return list(connus.values())


def generique(titre):
    """Titre sans contenu ("Informations privilegiees / Autres communiques") ;
    les autres rubriques AMF ("... / Communique sur comptes, resultats")
    indiquent le type."""
    return normaliser(titre).startswith("informations privilegiees / autres communiques")


def categorie(titre, sous_type):
    n = normaliser(titre)
    for cid, _, mots in CATEGORIES:
        if cid == "resultats":
            if sous_type in AMF_RESULTATS:
                return cid
            if any(f in n for f in FAUX_RESULTATS):
                continue
        if any(m in n for m in mots):
            return cid
    return "autre"


def sens_titre(titre):
    n = normaliser(titre)
    p = any(m in n for m in POSITIF)
    q = any(m in n for m in NEGATIF)
    return "positif" if p and not q else "negatif" if q and not p else None


def historique_amf(entreprises, cal, series, depuis):
    par_isin = {e["isin"]: e for e in entreprises}
    communiques = communiques_amf(entreprises, depuis)
    # Evenement = (action, seance de reaction) : plusieurs communiques le
    # meme jour (versions, annexes) ne comptent qu'une fois.
    evenements = {}
    for c in communiques:
        e = par_isin.get(c.get("identificationsociete_iso_cd_isi"))
        if e is None or not c.get("informationdeposee_inf_dat_emt"):
            continue
        titre = normaliser(c.get("informationdeposee_inf_tit_inf") or "")
        if (c.get("sous_type_d_information") not in AMF_RESULTATS
                and any(x in titre for x in TITRES_EXCLUS)):
            continue
        t = heure_paris(c["informationdeposee_inf_dat_emt"])
        jour = cal.reaction(t)
        if jour is None:
            continue
        ev = evenements.setdefault((e["ticker"], jour), {"ticker": e["ticker"], "nom": e["nom"], "jour": jour,
                                                          "communiques": [], "apres_cloture": None})
        ev["communiques"].append((t, c.get("informationdeposee_inf_tit_inf") or "",
                                  c.get("sous_type_d_information") or ""))
    ordre = [c[0] for c in CATEGORIES] + ["autre"]
    jours_evenement = {}
    for (ticker, jour) in evenements:
        jours_evenement.setdefault(ticker, set()).add(jour)

    mesures = []
    base = {"n": 0, "abs": 0.0, "net": 0, "fort": 0}
    vus_base = set()
    for (ticker, jour), ev in evenements.items():
        serie = series.get(ticker)
        if serie is None or jour not in serie.pos:
            continue
        s = serie.pos[jour]
        mod = serie.modele(s)
        if mod is None:
            continue
        m = serie.mesure(mod, s, s)
        titres = [c for c in ev["communiques"] if not generique(c[1])]
        cats = [categorie(c[1], c[2]) for c in titres] or ["autre"]
        cat = min(cats, key=ordre.index)
        sens = {sens_titre(c[1]) for c in titres} - {None}
        premier = min(ev["communiques"])[0]
        # Publication apres la cloture d'une seance : reaction attendue le
        # lendemain ; on mesure aussi la seance de publication pour comparer.
        z_veille = None
        if premier.date().isoformat() in serie.pos and premier.date().isoformat() != jour and s >= 1:
            z_veille = round(serie.ecart(mod, s - 1, s - 1) / mod[2], 2)
        mesures.append({"ticker": ticker, "nom": ev["nom"], "jour": jour, "categorie": cat,
                        "sens": sens.pop() if len(sens) == 1 else None,
                        "titre": (titres or ev["communiques"])[0][1], "z_veille": z_veille, **m})
        # Seances ordinaires : periode d'estimation hors jours de communique
        # (chaque seance comptee une fois par action).
        debut = max(1, s - ESTIMATION - ECART_ESTIMATION)
        for i, res in enumerate(mod[3]):
            d = serie.dates[debut + i]
            if (ticker, d) in vus_base or d in jours_evenement[ticker]:
                continue
            vus_base.add((ticker, d))
            z = abs(res) / mod[2]
            base["n"] += 1
            base["abs"] += z
            base["net"] += z >= SEUIL_NET
            base["fort"] += z >= SEUIL_FORT

    def resume(v):
        zs = [abs(x["z"]) for x in v]
        ecarts = sorted(abs(x["ecart_pct"]) for x in v)
        return {"n": len(v), "z_abs_moyen": arrondi(moyenne(zs), 2),
                "part_net": arrondi(sum(1 for z in zs if z >= SEUIL_NET) / len(v)),
                "part_fort": arrondi(sum(1 for z in zs if z >= SEUIL_FORT) / len(v)),
                "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] for x in v]), 2),
                "ecart_abs_median_pct": arrondi(ecarts[len(ecarts) // 2], 2)}

    libelles = {c[0]: c[1] for c in CATEGORIES}
    libelles["autre"] = "Autres communiqués"
    categories = []
    for cid in ordre:
        v = [x for x in mesures if x["categorie"] == cid]
        if v:
            categories.append(dict(resume(v), id=cid, libelle=libelles[cid]))
    categories.sort(key=lambda c: -c["part_fort"])

    # Sens d'apres le titre (mots-cles) : le meme test que pour les alertes.
    orientes = [x for x in mesures if x["sens"]]
    z_dir = [x["z"] if x["sens"] == "positif" else -x["z"] for x in orientes]
    justes = sum(1 for z in z_dir if z >= SEUIL_NET)
    faux = sum(1 for z in z_dir if z <= -SEUIL_NET)
    m, t, p = test_t(z_dir)
    rangs = [x["rang"] if x["sens"] == "positif" else 1 - x["rang"] for x in orientes]
    sens_mc = {
        "n": len(orientes), "justes": justes, "faux": faux,
        "taux": arrondi(justes / (justes + faux)) if justes + faux else None,
        "p": arrondi(p_binomiale(justes, justes + faux), 4),
        "ecart_moyen_pct": arrondi(moyenne([x["ecart_pct"] if x["sens"] == "positif" else -x["ecart_pct"]
                                            for x in orientes]), 2),
        "z_moyen": arrondi(m, 2), "p_t": arrondi(p, 4),
        "rang_p": arrondi(p_normale((moyenne(rangs) - 0.5) * math.sqrt(12 * len(rangs))), 4) if rangs else None,
        "par_sens": {s: dict(zip(("n", "ecart_moyen_pct"),
                                 (len(v), arrondi(moyenne([x["ecart_pct"] for x in v]), 2))))
                     for s in SENS_NOTES for v in [[x for x in orientes if x["sens"] == s]]},
    }
    decales = [x for x in mesures if x["z_veille"] is not None]
    top = sorted(mesures, key=lambda x: -abs(x["z"]))[:15]
    return {
        "depuis": depuis,
        "jusqu_a": cal.dates[-1],
        "n_communiques": sum(len(ev["communiques"]) for ev in evenements.values()),
        "n_evenements": len(mesures),
        "jour_ordinaire": {"n": base["n"], "z_abs_moyen": arrondi(base["abs"] / base["n"], 2) if base["n"] else None,
                           "part_net": arrondi(base["net"] / base["n"]) if base["n"] else None,
                           "part_fort": arrondi(base["fort"] / base["n"]) if base["n"] else None},
        "tous": resume(mesures) if mesures else {"n": 0},
        "categories": categories,
        "sens_mots_cles": sens_mc,
        "apres_cloture": {"n": len(decales),
                          "z_abs_jour_publication": arrondi(moyenne([abs(x["z_veille"]) for x in decales]), 2),
                          "z_abs_lendemain": arrondi(moyenne([abs(x["z"]) for x in decales]), 2)},
        "plus_fortes_reactions": [
            {k: x[k] for k in ("jour", "ticker", "nom", "categorie", "titre", "rendement_pct", "indice_pct",
                               "ecart_pct", "z")} for x in top],
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
        print("Historique des communiqués AMF…", file=sys.stderr)
        try:
            histo = historique_amf(entreprises, cal, series, args.depuis_historique)
        except Exception as e:
            print(f"Historique AMF indisponible : {e}", file=sys.stderr)
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
          + (f" Historique AMF : {histo['n_evenements']} événements depuis {histo['depuis']}." if histo else ""))


if __name__ == "__main__":
    main()
