#!/usr/bin/env python3
"""Actions des autres indices de l'application (hors SBF 120, deja suivi par
la veille) pour les scores des objectifs de cours : STOXX Europe 600
(positions de l'ETF iShares, comme l'application), S&P 500 et Nasdaq-100
(Wikipedia) ; DAX et Euro Stoxx 50 servent a etiqueter les actions (elles sont
toutes dans le STOXX 600).

Ecrit univers_objectifs.json, lu par objectifs.py et scores.py avec
--univers monde. A relancer a la main (composition des indices).

Usage : python univers.py
Bibliotheque standard uniquement.
"""

import datetime as dt
import html
import json
import os
import re
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(ICI, "univers_objectifs.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
ISHARES_600 = ("https://www.ishares.com/ch/individual/en/products/251931/"
               "ishares-stoxx-europe-600-ucits-etf-de-fund/1495092304805.ajax"
               "?fileType=csv&fileName=EXSA_holdings&dataType=fund")
WIKI = "https://en.wikipedia.org/wiki/"

# Bourse (libelle iShares) -> (suffixe Yahoo, drapeau Zonebourse), comme
# l'application (wikipedia_service.dart).
BOURSES = {
    "london stock exchange": (".L", "gb"),
    "nyse euronext - euronext paris": (".PA", "fr"),
    "xetra": (".DE", "de"),
    "deutsche boerse xetra": (".DE", "de"),
    "six swiss exchange": (".SW", "ch"),
    "nasdaq omx nordic": (".ST", "se"),
    "borsa italiana": (".MI", "it"),
    "euronext amsterdam": (".AS", "nl"),
    "bolsa de madrid": (".MC", "es"),
    "omx nordic exchange copenhagen a/s": (".CO", "dk"),
    "oslo bors asa": (".OL", "no"),
    "nasdaq omx helsinki ltd.": (".HE", "fi"),
    "warsaw stock exchange/equities/main market": (".WA", "pl"),
    "nyse euronext - euronext brussels": (".BR", "be"),
    "athens exchange s.a. cash market": (".AT", "gr"),
    "wiener boerse ag": (".VI", "at"),
    "irish stock exchange - all market": (".IR", "ie"),
    "nyse euronext - euronext lisbon": (".LS", "pt"),
}
SECTEURS = {  # GICS / ICB -> libelle francais
    "Information Technology": "Technologie", "Technology": "Technologie",
    "Health Care": "Santé", "Financials": "Finance", "Financial": "Finance",
    "Consumer Discretionary": "Consommation discrétionnaire", "Consumer Staples": "Consommation de base",
    "Industrials": "Industrie", "Energy": "Énergie", "Materials": "Matériaux", "Basic Materials": "Matériaux",
    "Utilities": "Services publics", "Real Estate": "Immobilier",
    "Communication Services": "Communication", "Telecommunications": "Communication",
    "Consumer Services": "Consommation discrétionnaire", "Consumer Goods": "Consommation de base",
}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _csv(ligne):
    out, buf, guill = [], [], False
    for c in ligne:
        if c == '"':
            guill = not guill
        elif c == "," and not guill:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    out.append("".join(buf))
    return out


def stoxx600():
    lignes = get(ISHARES_600).replace("﻿", "").splitlines()
    i = next(k for k, l in enumerate(lignes) if l.startswith("Ticker,"))
    entete = _csv(lignes[i])
    col = {n: entete.index(n) for n in ("Ticker", "Name", "Sector", "Asset Class", "Weight (%)", "Exchange",
                                        "Location")}
    out = []
    for l in lignes[i + 1:]:
        c = _csv(l)
        if len(c) <= col["Exchange"] or c[col["Asset Class"]] != "Equity":
            continue
        try:
            if float(re.sub(r"[^0-9.\-]", "", c[col["Weight (%)"]]) or 0) <= 0:
                continue  # action en cours de sortie de l'indice
        except ValueError:
            continue
        bourse = BOURSES.get(c[col["Exchange"]].lower())
        if not bourse:
            continue
        code = c[col["Ticker"]].strip().rstrip(".")
        base = re.sub(r"[\s.]+", "-", code)
        if bourse[0] == ".SW":
            base = base.replace("-", "")
        out.append({"ticker": base + bourse[0], "nom": c[col["Name"]].strip().title(), "zone": "europe",
                    "pays": c[col["Location"]], "secteur": SECTEURS.get(c[col["Sector"]], c[col["Sector"]]),
                    "zb_code": code, "zb_drapeau": bourse[1], "indices": ["STOXX 600"]})
    return out


def tables(page):
    out = []
    for t in re.findall(r"<table[^>]*>.*?</table>", page, re.S):
        out.append([[html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
                    for r in re.findall(r"<tr.*?</tr>", t, re.S)])
    return out


def table_avec(page, colonne):
    """Plus grande table dont l'en-tete contient `colonne`."""
    cands = [t for t in tables(page) if t and colonne in t[0]]
    return max(cands, key=len) if cands else []


def americaines():
    out = {}
    t = table_avec(get(WIKI + "List_of_S%26P_500_companies"), "Symbol")
    i, n, s = t[0].index("Symbol"), t[0].index("Security"), t[0].index("GICS Sector")
    for r in t[1:]:
        if len(r) > s and r[i]:
            out[r[i]] = {"ticker": r[i].replace(".", "-"), "nom": r[n], "zone": "usa", "pays": "United States",
                         "secteur": SECTEURS.get(r[s], r[s]), "zb_code": r[i], "zb_drapeau": "us",
                         "indices": ["S&P 500"]}
    t = table_avec(get(WIKI + "List_of_NASDAQ-100_companies"), "Ticker")
    i, n = t[0].index("Ticker"), t[0].index("Company")
    s = next((k for k, h in enumerate(t[0]) if h.startswith("ICB Industry") or h.startswith("GICS Sector")), None)
    for r in t[1:]:
        if len(r) <= n or not r[i]:
            continue
        e = out.setdefault(r[i], {"ticker": r[i].replace(".", "-"), "nom": r[n], "zone": "usa",
                                  "pays": "United States", "secteur": SECTEURS.get(r[s], r[s]) if s else None,
                                  "zb_code": r[i], "zb_drapeau": "us", "indices": []})
        e["indices"].append("Nasdaq-100")
    return list(out.values())


def etiquettes(page, nom_indice, liste):
    """Ajoute `nom_indice` aux actions dont le ticker Yahoo est dans la table."""
    t = table_avec(page, "Ticker")
    if not t:
        return 0
    i = t[0].index("Ticker")
    codes = {r[i].strip() for r in t[1:] if len(r) > i}
    n = 0
    for e in liste:
        if e["ticker"] in codes and nom_indice not in e["indices"]:
            e["indices"].append(nom_indice)
            n += 1
    return n


def main():
    with open(os.path.join(ICI, "referentiel.json"), encoding="utf-8") as f:
        suivies = {e["ticker"] for e in json.load(f)["entreprises"]}
    europe = [e for e in stoxx600() if e["ticker"] not in suivies]
    usa = americaines()
    n_dax = etiquettes(get(WIKI + "DAX"), "DAX", europe)
    n_sx5e = etiquettes(get(WIKI + "EURO_STOXX_50"), "Euro Stoxx 50", europe)
    vus, liste = set(), []
    for e in europe + usa:
        if e["ticker"] not in vus:
            vus.add(e["ticker"])
            liste.append(e)
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write('{\n "genere_le": %s,\n "entreprises": [\n' % json.dumps(dt.date.today().isoformat()))
        f.write(",\n".join("  " + json.dumps(e, ensure_ascii=False) for e in liste))
        f.write("\n ]\n}\n")
    print(f"univers_objectifs.json : {len(europe)} européennes (dont DAX {n_dax}, Euro Stoxx 50 {n_sx5e}), "
          f"{len(usa)} américaines, {len(liste)} au total.")


if __name__ == "__main__":
    main()
