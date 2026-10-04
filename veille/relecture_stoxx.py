#!/usr/bin/env python3
"""Extraits pour la relecture des perspectives des publications du STOXX 600
(encadre « Ce qui compte : les perspectives » de l'onglet Statistiques >
Communiques > STOXX 600). Les perspectives sont jugees par Claude a la lecture
(valeurs : relevees, confirmees, abaissees, nouvelles, ou rien), comme pour la
France et les Etats-Unis.

Texte lu pour chaque publication :
  - Royaume-Uni (Investegate) et pays nordiques (Nasdaq Nordic) : le communique
    de resultats lui-meme ; on garde son titre, les paragraphes qui suivent un
    intertitre « Outlook » ou « Guidance » et les phrases sur les perspectives ;
  - autres pays : pas de source gratuite du communique ; les titres de la presse
    du jour de la publication (Google Actualites) qui parlent de l'entreprise et
    de ses objectifs.

Ecrit relecture_stoxx/extraits_communiques.json et extraits_presse.json ({id:
{entreprise, publication, source, extrait}}) ; les verdicts sont dans
relecture_stoxx/perspectives_claude.json ({id: valeur ou null}), lu par
etude_stoxx.py. Les communiques telecharges sont gardes dans .cache/stoxx_textes
(les extraits se recalculent sans reseau).

Usage :
  python relecture_stoxx.py --communiques    extraits des communiques (Royaume-Uni, Nordiques) ; reprise possible
  python relecture_stoxx.py --communiques --refaire   recalcule tous les extraits (textes en cache)
  python relecture_stoxx.py --presse         titres de presse (autres pays) ; reprise possible
  python relecture_stoxx.py --integrer F     ajoute les verdicts {id: valeur} du fichier F
  python relecture_stoxx.py --etat
Bibliotheque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
DOSSIER = os.path.join(ICI, "relecture_stoxx")
EXTRAITS_COMMUNIQUES = os.path.join(DOSSIER, "extraits_communiques.json")
EXTRAITS_PRESSE = os.path.join(DOSSIER, "extraits_presse.json")
CACHE = os.path.join(ICI, ".cache", "stoxx_textes")
VERDICTS = os.path.join(DOSSIER, "perspectives_claude.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
VALEURS = ("relevees", "confirmees", "abaissees", "nouvelles")
MAXI = 1000  # caracteres d'extrait

# Phrases juridiques ou de contact : sans information sur les perspectives.
_BRUIT = re.compile(r"forward-looking|safe harbo|pursuant to|risks and uncertainties|reconciliation|webcast|"
                    r"conference call|investor contact|media contact|@|undue reliance|this announcement|"
                    r"market abuse|inside information|LEI|ISIN|disclaimer|cautionary|www\.|click here|"
                    r"copyright|cookies", re.I)
# Sujets annexes ou mentions juridiques, jamais retenus comme perspectives.
_ANNEXE = re.compile(r"These statements are often|No statement in this|profit forecast or a profit estimate|"
                     r"does not represent any guidance|forward[- ]looking|should be construed|safe harbo|"
                     r"past performance|not an indicator|not an estimate|share buy-?back|repurchas|"
                     r"dividend policy|scope [123]|emission|net zero|biodiversity|credit rating|"
                     r"stable outlook|negative outlook|positive outlook|rating agenc|moody|standard (?:&|and) poor",
                     re.I)
# Intertitres qui introduisent les perspectives.
_SECTION = re.compile(r"^\W*(?:[\w&,/ -]{0,40}\b)?(outlook|guidance|prospects|looking ahead|looking forward|"
                      r"current trading|expectations|financial targets|forecasts?|trading update|"
                      r"financial guidance|(?:20\d\d|fy ?\d\d) (?:outlook|guidance|targets?))\b[\w&,/ -]{0,40}:?\W*$",
                      re.I)
# Mots qui annoncent une perspective (formules precises : « lower », « reduce » ou
# « raise » seuls designent surtout des chiffres passes).
_PROCHE = (r"(?:20\d\d|full[- ]year|fy ?\d\d|financial year|fiscal year|current year|this year|second half|h2|"
           r"next year|coming (?:year|quarter)|quarter|growth|margin|revenue|sales|profit|ebit|earnings|eps|cash|"
           r"dividend|orders|volumes?|capex|capital expenditure|leverage|cost)")
_INDICE = re.compile(
    r"outlook|guidance|guided|forecast|prospects|medium[- ]term|mid[- ]term|long[- ]term (?:target|ambition)|"
    r"(?:we|group|company|management|board)\s+(?:now\s+|still\s+|continue to\s+|also\s+)?(?:expect|anticipate|"
    r"project|target|aim|plan)s?\b|(?:is|are|was|were) (?:now )?(?:expected|anticipated|forecast|projected|"
    r"targeted) to|now (?:expect|see|anticipate)s?|(?:expect|anticipate)s? [^.]{0,80}" + _PROCHE + r"|"
    r"(?:confirm|reiterat|reaffirm|maintain|upgrad|downgrad|rais|lift|narrow|cut|lower|reduc|increas|updat|"
    r"revis|rebas|withdr[ae]w|suspend)(?:e|es|ed|ing|s)?\s+(?:its |our |the |full[- ]year |fy ?\d\d |20\d\d )*"
    r"(?:guidance|outlook|target|forecast|expectation|ambition|range|view|prospects)|"
    r"in line with (?:our |market |consensus |analyst |company[- ]compiled )?(?:expectations|forecasts|guidance)|"
    r"(?:ahead of|above|below|behind|better than|worse than|lower than|higher than) (?:our |market |consensus |"
    r"previous |prior )?(?:expectations|guidance|forecast)|on track to|(?:on|off) track|upper end|lower end|"
    r"top end|bottom end|(?:profit|earnings) warning|trading (?:in line|ahead|behind)", re.I)


def lire_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def ecrire_json(chemin, doc, indent=None):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
        f.write("\n")


def get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(2)
    raise OSError(url)


def texte_brut(b):
    b = re.sub(r"<script.*?</script>|<style.*?</style>", "", b, flags=re.S)
    b = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", b)
    b = html.unescape(re.sub(r"<[^>]+>", " ", b))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", b))


def extrait_perspectives(texte, titre="", maxi=MAXI):
    """Titre, puis les paragraphes qui suivent un intertitre « Outlook » ou
    « Guidance », puis les phrases a signal fort (sans mentions juridiques ni
    contacts), dans l'ordre du communique."""
    t = re.sub(r"[ \t]*[•●▪·§][ \t]*", "\n", texte or "")
    lignes = [" ".join(ligne.split()) for ligne in t.splitlines()]
    lignes = [ligne for ligne in lignes if ligne]
    garde, total = [], len(titre)

    def ajouter(p):
        nonlocal total
        if p and p not in garde and p != titre and total < maxi:
            garde.append(p)
            total += len(p) + 1

    # Sections de perspectives : les lignes qui suivent l'intertitre.
    sections = 0
    for i, ligne in enumerate(lignes):
        if len(ligne) <= 80 and _SECTION.match(ligne) and not _BRUIT.search(ligne):
            bloc, n = [], 0
            for suivante in lignes[i + 1:i + 8]:
                if len(suivante) <= 40 and not suivante.endswith((".", ";")) and suivante[:1].isupper():
                    break  # intertitre suivant
                if _BRUIT.search(suivante) or _ANNEXE.search(suivante):
                    continue
                bloc.append(suivante)
                n += len(suivante)
                if n >= 650:
                    break
            if bloc:
                ajouter("[" + ligne[:40] + "] " + " ".join(bloc)[:650])
                sections += 1
        if sections >= 2:
            break
    morceaux = [m for ligne in lignes
                for m in re.split(r"(?<=[.;!?])\s+(?=[A-Z\"“(])", ligne)]
    for p in morceaux:
        if 30 < len(p) < 600 and not _BRUIT.search(p) and not _ANNEXE.search(p) and _INDICE.search(p):
            ajouter(p)
    return (titre + (" [...] " if titre and garde else "") + " ".join(garde))[:maxi]


# ---------------------------------------------------------------------------
# Communiques (Royaume-Uni, pays nordiques)
# ---------------------------------------------------------------------------

def _corps(p, b):
    t = texte_brut(b)
    if p["source"].startswith("investegate"):
        # Le texte de l'annonce suit les menus du site et le resume automatique de l'annonce :
        # on repart de « RNS Number » ou, a defaut, de « Close X ».
        i = t.find("RNS Number")
        if i < 0:
            i = t.find("Close X")
        t = t[i:] if i >= 0 else t
    return t


def texte_cache(p):
    """Texte de la publication (toutes les parties d'un communique britannique),
    lu sur le disque s'il est deja telecharge."""
    base = re.sub(r"[^A-Za-z0-9.-]", "_", p["id"])
    urls = p.get("urls") or [p["url"]]
    morceaux = []
    for i, url in enumerate(urls):
        chemin = os.path.join(CACHE, base + (".txt.gz" if i == 0 else f".{i}.txt.gz"))
        if os.path.exists(chemin):
            with gzip.open(chemin, "rt", encoding="utf-8") as f:
                morceaux.append(f.read())
            continue
        texte = _corps(p, get(url))
        os.makedirs(CACHE, exist_ok=True)
        with gzip.open(chemin, "wt", encoding="utf-8") as f:
            f.write(texte[:120000])
        morceaux.append(texte)
    return "\n".join(morceaux)


def communiques(refaire=False):
    doc = lire_json(os.path.join(ICI, "resultats_stoxx.json"), {"entreprises": {}})
    extraits = lire_json(EXTRAITS_COMMUNIQUES, {})
    a_lire = [(t, f["nom"], p) for t, f in doc["entreprises"].items() for p in f["publications"]
              if p.get("url") and (refaire or p["id"] not in extraits)]
    print(f"{len(a_lire)} communiqué(s) à lire…", file=sys.stderr)

    def lire(x):
        t, nom, p = x
        try:
            corps = texte_cache(p)
        except OSError:
            return p["id"], None
        return p["id"], {"entreprise": nom, "publication": p.get("titre"), "source": p["source"].split("+")[0],
                         "date": p["publie_le"][:10],
                         "extrait": extrait_perspectives(corps, p.get("titre") or "")}

    erreurs = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for i, (id_, r) in enumerate(ex.map(lire, a_lire), 1):
            if r is None:
                erreurs += 1
            else:
                extraits[id_] = r
            if i % 200 == 0:
                ecrire_json(EXTRAITS_COMMUNIQUES, extraits)
                print(f"  {i}/{len(a_lire)}", file=sys.stderr)
    ecrire_json(EXTRAITS_COMMUNIQUES, extraits)
    print(f"extraits_communiques.json : {len(extraits)} extraits ; {erreurs} communiqué(s) illisible(s).")


# ---------------------------------------------------------------------------
# Presse (autres pays)
# ---------------------------------------------------------------------------

LEGAL = re.compile(r"\b(aktiengesellschaft|aktieselskab|aktiebolag|aktiebolaget|aktien|ag|se|s\.?a\.?|n\.?v\.?|plc|"
                   r"p\.l\.c\.|s\.?p\.?a\.?|a/s|asa|ab|oyj|abp|corporation|corp|company|co|group|holdings?|limited|"
                   r"ltd|bv|kgaa|publ|inc|sab|spa|oy|as|in münchen|in m.nchen|sociedad anonima|s\.a\.u\.)\b\.?",
                   re.I)


def nom_recherche(ticker, noms):
    x = noms.get(ticker) or {}
    nom = re.sub(r"[^\w &\-.]", " ", x.get("long") or x.get("short") or ticker)
    return " ".join(LEGAL.sub(" ", nom).split())


def gnews(requete, d1, d2):
    u = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{requete} after:{d1} before:{d2}", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    b = get(u, 30)
    res = []
    for it in re.findall(r"<item>(.*?)</item>", b, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it)
        if t and p:
            res.append((email.utils.parsedate_to_datetime(p.group(1)).strftime("%Y-%m-%d"),
                        html.unescape(t.group(1)).strip()))
    return res


_PRESSE = re.compile(r"outlook|guidance|forecast|target|expect|raise|lift|upgrade|cut|lower|confirm|reiterat|"
                     r"maintain|sees|ambition|warn|profit|earnings|results|revenue|sales|quarter|half|year|"
                     r"beat|miss|ebit|dividend|buyback|orders", re.I)


def presse(lent=False):
    doc = lire_json(os.path.join(ICI, "resultats_stoxx.json"), {"entreprises": {}})
    noms = lire_json(os.path.join(ICI, "stoxx", "noms.json"), {})
    extraits = lire_json(EXTRAITS_PRESSE, {})
    a_lire = [(t, f["nom"], p) for t, f in doc["entreprises"].items() for p in f["publications"]
              if not p.get("url") and p["id"] not in extraits]
    print(f"{len(a_lire)} publication(s) à chercher dans la presse…", file=sys.stderr)

    def chercher(x):
        t, nom, p = x
        n = nom_recherche(t, noms)
        cle = n.lower().split()[0] if n else ""
        d = dt.date.fromisoformat(p["publie_le"][:10])
        try:
            r = gnews(f'"{n}" (results OR outlook OR guidance OR earnings)', (d - dt.timedelta(days=1)).isoformat(),
                      (d + dt.timedelta(days=3)).isoformat())
        except OSError:
            if lent:
                time.sleep(45)  # Google limite le debit : on laisse retomber
            return p["id"], None
        pertinents = [(j, ti) for j, ti in r if cle and cle in ti.lower() and _PRESSE.search(ti)]
        time.sleep(2.5 if lent else 0.4)
        lignes = []
        for j, ti in pertinents[:7]:
            ti = re.sub(r"\s+", " ", ti)[:150]
            if ti not in lignes:
                lignes.append(ti)
        return p["id"], {"entreprise": nom, "publication": None, "source": "presse", "date": p["publie_le"][:10],
                         "extrait": " | ".join(lignes)}

    erreurs = 0
    with cf.ThreadPoolExecutor(1 if lent else 4) as ex:
        for i, (id_, r) in enumerate(ex.map(chercher, a_lire), 1):
            if r is None:
                erreurs += 1
            else:
                extraits[id_] = r
            if i % 100 == 0:
                ecrire_json(EXTRAITS_PRESSE, extraits)
                print(f"  {i}/{len(a_lire)}", file=sys.stderr)
    ecrire_json(EXTRAITS_PRESSE, extraits)
    avec = sum(1 for v in extraits.values() if v["extrait"])
    print(f"extraits_presse.json : {len(extraits)} recherches dont {avec} avec des titres ; "
          f"{erreurs} recherche(s) échouée(s).")


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def integrer(fichier):
    nouveaux = lire_json(fichier, {})
    verdicts = lire_json(VERDICTS, {}).get("publications", {})
    mauvais = {k: v for k, v in nouveaux.items() if v is not None and v not in VALEURS}
    if mauvais:
        sys.exit(f"valeurs inconnues : {list(mauvais.items())[:5]}")
    verdicts.update(nouveaux)
    ecrire_json(VERDICTS, {"version": 1, "source": "relecture par Claude (communiqués, presse)",
                           "publications": dict(sorted(verdicts.items()))})
    print(f"{VERDICTS} : {len(verdicts)} verdicts.")


def etat():
    extraits = dict(lire_json(EXTRAITS_COMMUNIQUES, {}), **lire_json(EXTRAITS_PRESSE, {}))
    verdicts = lire_json(VERDICTS, {}).get("publications", {})
    pr = sum(1 for k in extraits if k in verdicts)
    print(f"{len(extraits)} extraits, {pr} relus, {len(extraits) - pr} restants ; "
          f"{sum(1 for v in verdicts.values() if v)} verdicts non nuls.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--communiques", action="store_true")
    p.add_argument("--refaire", action="store_true", help="recalcule les extraits des communiques deja lus")
    p.add_argument("--presse", action="store_true")
    p.add_argument("--lent", action="store_true", help="presse : une requete a la fois, pauses longues")
    p.add_argument("--integrer")
    p.add_argument("--etat", action="store_true")
    a = p.parse_args()
    if a.communiques:
        communiques(a.refaire)
    if a.presse:
        presse(a.lent)
    if a.integrer:
        integrer(a.integrer)
    if a.etat:
        etat()


if __name__ == "__main__":
    main()
