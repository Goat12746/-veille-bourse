"""Outils communs aux statistiques (statistiques.py, etude_amf.py) : heure de
Paris et seances de cotation, cours Yahoo, modele de marche et tests
statistiques. Bibliotheque standard uniquement.

Methode : etude d'evenements (MacKinlay, 1997). Modele de marche estime sur
les 250 seances qui precedent l'evenement (rendement de l'action = alpha +
beta x rendement du CAC 40) ; ecart (rendement anormal) = rendement observe
moins rendement attendu d'apres le CAC 40, exprime aussi en sigma (ecart
habituel d'une seance, residus du modele).
"""

import bisect
import datetime as dt
import json
import math
import os
import time
import urllib.parse

from collecte import _yahoo_get

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, ".cache")
INDICE = "^FCHI"  # CAC 40 (Yahoo n'a pas d'historique du SBF 120)

ESTIMATION = 250  # seances du modele de marche
ESTIMATION_MIN = 120
ECART_ESTIMATION = 10  # seances ecartees juste avant l'evenement (fuites)
CLOTURE = (17, 35)  # fin du fixing de cloture d'Euronext Paris
OUVERTURE = (9, 0)
SEUIL_NET, SEUIL_FORT = 1.0, 2.0


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


def mediane(v):
    v = sorted(v)
    if not v:
        return None
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


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


def p_deux_proportions(k1, n1, k2, n2):
    """Test bilateral d'egalite de deux proportions (approximation normale)."""
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    if p in (0, 1):
        return None
    z = (k1 / n1 - k2 / n2) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return p_normale(z)


def arrondi(x, n=3):
    return None if x is None else round(x, n)
