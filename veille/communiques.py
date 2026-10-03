#!/usr/bin/env python3
"""Communiques AMF des entreprises du referentiel depuis 2019 : type et sens
de chaque communique, base de l'etude de la reaction des cours
(statistiques.py, onglet Statistiques > Historique AMF).

  - liste des communiques (info-financiere.gouv.fr), version francaise, a
    defaut anglaise (quelques societes ne publient qu'en anglais) ;
  - texte des deux premieres pages du PDF quand un extracteur est disponible
    (pdftotext, sinon pypdf ou PyPDF2), garde en cache dans .cache/textes ;
  - type d'apres le titre et le debut du texte (resultats, revision des
    objectifs, operation...) et sens estime par mots-cles, sans IA ;
  - sens des publications de resultats (et des revisions d'objectifs) juge
    par Claude : resultats_classes.json, alimente par la veille quotidienne
    (voir CONSIGNES.md). Il prime sur les mots-cles.

Ecrit communiques.json. Le passe ne change pas : seuls les communiques des
derniers jours sont recherches et seuls les nouveaux sont lus.

Usage :
  python communiques.py                      mise a jour
  python communiques.py --complet            tout l'historique (--depuis 2019-01-01)
  python communiques.py --reclasser          recalcule type et sens des textes en cache
  python communiques.py --a-classer          resultats sans avis de Claude -> sortie/resultats_a_classer.json
  python communiques.py --integrer FICHIER   ajoute des avis de Claude a resultats_classes.json
Bibliotheque standard uniquement (pdftotext ou pypdf facultatifs).
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

from collecte import http_get, normaliser

ICI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ICI, ".cache")
TEXTES = os.path.join(CACHE, "textes")
SORTIE = os.path.join(ICI, "sortie")
FICHIER = os.path.join(ICI, "communiques.json")
CLASSES = os.path.join(ICI, "resultats_classes.json")
AMF_EXPORT = ("https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/"
              "datasets/flux-amf-new-prod/exports/json")
DEPUIS = "2019-01-01"
RECOUVREMENT = 7  # jours relus a chaque mise a jour (depots tardifs)
TEXTE_MAX = 6000  # caracteres gardes par communique (deux premieres pages)

AMF_TYPES = ["Informations privilégiées", "Information financière trimestrielle",
             "Rapports financiers et d'audit semestriels/examens réduits",
             "Rapports financiers et d'audit annuels",
             "Communiqués publiés en période d'offre publique d'acquisition"]
AMF_RAPPORTS = {"Information financière trimestrielle",
                "Rapports financiers et d'audit semestriels/examens réduits",
                "Rapports financiers et d'audit annuels"}

# Type de communique d'apres le titre ou l'en-tete du texte (sans accents,
# minuscules), par ordre de priorite (voir categorie()). Mots anglais pour
# les societes qui publient en anglais.
CATEGORIES = [
    ("revision", "Révisions d'objectifs, avertissements", [
        "releve ses", "releve son", "releve sa", "releve l'ensemble", "rehausse", "a la hausse ses",
        "abaisse", "a la baisse ses", "revise ses", "revise son", "revise sa", "revoit ses", "revoit son",
        "revoit sa", "ajuste ses", "ajuste son", "ajuste sa", "actualise ses", "avertissement",
        "preliminaire", "nouveaux objectifs", "objectifs revus", "perspectives revues",
        "raises guidance", "raises its", "lowers guidance", "lowers its", "cuts guidance", "profit warning",
        "revises guidance", "revises its", "updates guidance", "updates its", "preliminary"]),
    # Pas "performance" (Teleperformance) ni "information financiere" (en-tete
    # de nombreux communiques sans chiffres).
    ("resultats", "Résultats et chiffre d'affaires", [
        "resultat", "chiffre d'affaires", "ventes", "trimestre", "semestre", "9 mois",
        "neuf mois", "activite du", "activite au", "produit net bancaire", "results", "revenue", "sales",
        "quarter", "half-year", "half year", "full-year", "full year", "first half", "nine months", "9 months",
        "earnings", "trading update"]),
    ("trafic", "Trafic et activité mensuelle", ["trafic", "traffic"]),
    ("operation", "Acquisitions, cessions, fusions", [
        "acquisition", "acquiert", "acquerir", "cession", "cede ", "ceder", "fusion", "offre publique",
        "rapprochement", "negociations exclusives", "prise de participation", "rachat de la", "rachat du",
        "rachat des activites", "scission", "introduction en bourse", "entree en negociation", "rumeur",
        "acquire", "divest", "disposal", "merger", "tender offer", "takeover", "exclusive negotiations",
        "spin-off"]),
    ("juridique", "Juridique et réglementaire", [
        "enquete", "amende", "litige", "condamn", "proces", "tribunal", "sanction", "autorite de la concurrence",
        "commission europeenne", "plainte", "contentieux", "arbitrage", "cour d'appel", "jugement",
        "investigation", "lawsuit", "litigation", "court", "fine ", "settlement"]),
    ("financement", "Financement et capital", [
        "emission", "obligat", "augmentation de capital", "placement", "financement", "credit", "notation",
        "oceane", "ornane", "subordonne", "hybride", "emprunt", "souscription", "bond", "notes ",
        "rating", "capital increase", "refinancing", "financing"]),
    ("actionnaires", "Dividendes et rachats d'actions", [
        "dividende", "rachat d'actions", "actions propres", "retour aux actionnaires", "annulation d'actions",
        "programme de rachat", "distribution", "acompte", "dividend", "buyback", "share repurchase"]),
    ("strategie", "Stratégie et restructuration", [
        "strategi", "feuille de route", "capital markets day", "journee investisseurs", "restructuration",
        "reorganisation", "transformation", "plan social", "suppression de postes", "strategy",
        "restructuring", "investor day"]),
    ("commercial", "Contrats, produits et essais cliniques", [
        "commande", "contrat", "remporte", "selectionne", "partenariat", "accord", "lance ", "lancement",
        "inaugure", "approbation", "approuv", "autorisation", "fda", "clinique", "essai", "phase ", "patients",
        "brevet", "fournira", "livre ", "s'associe", "collabor", "contract", "order", "partnership",
        "agreement", "launch", "approval", "trial"]),
    ("gouvernance", "Gouvernance et dirigeants", [
        "nomination", "nomme", "directeur general", "president", "conseil d'administration",
        "assemblee generale", "gouvernance", "depart", "succession", "administrat", "dirigeant",
        "appoint", "chief executive", "ceo", "board", "general meeting"]),
]
LIBELLES = dict([(c[0], c[1]) for c in CATEGORIES] + [("autre", "Autres communiqués")])
ORDRE = [c[0] for c in CATEGORIES] + ["autre"]

# Rubriques AMF des titres generiques ("Informations privilegiees / ...").
RUBRIQUES = {
    "communique sur comptes, resultat": "resultats",
    "information sur chiffre d'affaires": "resultats",
    "operations de l'emetteur": "operation",
    "declarations d'intention": "operation",
}
GENERIQUES = ["informations privilegiees / autres communiques", "inside information / other news releases",
              "communiques au titre de l'obligation d'information permanente"]
# Declarations recurrentes : pas une nouvelle, ecartees de l'etude.
TITRES_EXCLUS = ["nombre total de droits de vote", "transactions sur actions propres",
                 "document d'enregistrement universel", "document de reference", "mise a disposition",
                 "version anglaise", "declaration des transactions", "total number of voting rights",
                 "transactions in own shares", "availability of", "universal registration document",
                 "declaration hebdomadaire", "contrat de liquidite", "liquidity contract"]
# "resultats de l'offre", annonce d'une date de publication... : pas des
# resultats financiers.
FAUX_RESULTATS = ["resultat de l'offre", "resultats de l'offre", "resultat du rachat", "resultats du rachat",
                  "resultat des votes", "resultats des votes", "resultat de l'assemblee",
                  "resultats de l'assemblee", "resultat de l'augmentation", "resultats de l'augmentation",
                  "seront publies", "sera publie", "date de publication", "heure de diffusion",
                  "calendrier financier", "conference telephonique", "webcast", "invitation",
                  "will be published", "will publish", "conference call", "results of the offer",
                  "resultats de l'etude", "resultats de l'essai", "resultats positifs de l'essai",
                  "resultats cliniques", "results of the study", "trial results", "topline",
                  "declaration des transactions", "transactions realisees", "programme de rachat",
                  "rachat d'actions", "droits de vote", "mise a disposition", "modalites de mise"]

# Sens d'un texte par mots-cles (methode simple, sans IA, a titre indicatif :
# les entreprises choisissent leurs mots).
POSITIF = ["releve", "rehausse", "a la hausse", "en hausse", "hausse de", "croissance", "record", "progression",
           "progresse", "solide", "superieur", "depasse", "amelior", "succes", "remporte", "accelere",
           "acceleration", "robuste", "excellent", "rebond", "renforce", "dynamique", "augmentation",
           "increase", "growth", "strong", "solid", "improve", "raise", "exceed", "outperform",
           "robust", "awarded", "higher"]
NEGATIF = ["abaisse", "a la baisse", "en baisse", "baisse de", "recul", "repli", "perte", "deprecia",
           "avertissement", "difficile", "degrad", "ralentissement", "suspend", "inferieur", "decline",
           "chute", "contraction", "diminution", "deficit", "defavorable", "decevant", "retard",
           "decrease", "lower", "loss", "weak", "impairment", "warning", "below", "slowdown", "downturn",
           "challenging", "headwind"]
SEUIL_SENS = 0.34  # score (positifs - negatifs) / total au-dela duquel le texte est oriente


# ---------------------------------------------------------------------------
# Texte des PDF
# ---------------------------------------------------------------------------

def extracteur():
    """Outil disponible pour lire les PDF : "pdftotext", "pypdf", "PyPDF2" ou None."""
    if shutil.which("pdftotext"):
        return "pdftotext"
    for module in ("pypdf", "PyPDF2"):
        try:
            __import__(module)
            return module
        except ImportError:
            pass
    return None


def texte_pdf(data, outil):
    """Texte des deux premieres pages d'un PDF (chaine vide si illisible)."""
    if outil == "pdftotext":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(data)
            chemin = f.name
        try:
            r = subprocess.run(["pdftotext", "-f", "1", "-l", "2", "-enc", "UTF-8", "-q", chemin, "-"],
                               capture_output=True, timeout=60)
            return r.stdout.decode("utf-8", "replace")
        finally:
            os.unlink(chemin)
    if outil in ("pypdf", "PyPDF2"):
        import io
        lecteur = __import__(outil).PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in lecteur.pages[:2])
    return ""


def nettoyer(texte):
    lignes = [re.sub(r"[ \t ]+", " ", l).strip() for l in texte.replace("\f", "\n").split("\n")]
    return "\n".join(l for l in lignes if l)[:TEXTE_MAX]


def lire_texte(c, outil):
    """Texte en cache, sinon telecharge et extrait (None si pas d'extracteur
    ou PDF illisible)."""
    chemin = os.path.join(TEXTES, c["id"] + ".txt")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return f.read()
    if outil is None or not c.get("url"):
        return None
    texte = ""  # PDF illisible apres trois essais : on ne reessaie pas a chaque fois
    for essai in range(3):
        try:
            texte = nettoyer(texte_pdf(http_get(c["url"], timeout=90), outil))
            break
        except Exception:
            if essai < 2:
                time.sleep(2 * (essai + 1))
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(texte)
    return texte


# Lignes sans contenu en tete des communiques.
_BANALES = re.compile(
    r"^(communique( de presse)?|press release|information reglementee|regulated information|"
    r"ne pas (diffuser|publier)|not for (release|distribution)|paris|information financiere|"
    r"communique financier|financial (release|information)|embargo|page \d|\d{1,2} \w+ 20\d\d)",
    re.I)


def entete(texte):
    """Premiere phrase porteuse du texte (titre reel du communique), pour les
    titres AMF generiques et l'affichage."""
    for ligne in (texte or "").split("\n")[:25]:
        n = normaliser(ligne)
        if len(n) < 20 or _BANALES.match(n) or not re.search(r"[a-z]{3}", n):
            continue
        if re.match(r"^[\w .,'&-]{0,40}, le \d", n):  # "Paris, le 24 juillet 2025 - ..."
            continue
        return ligne[:180]
    return ""


def generique(titre):
    n = normaliser(titre)
    return any(n.startswith(g) for g in GENERIQUES)


def exclu(c):
    """Declaration recurrente sans information (droits de vote, rachats
    d'actions propres...)."""
    if c["sous_type"] in AMF_RAPPORTS:
        return False
    n = normaliser(c["titre"])
    return any(x in n for x in TITRES_EXCLUS)


def _trouve(n, mots):
    return any(m in n for m in mots)


# Chiffre cle suivi d'une variation signee en % dans les premieres lignes :
# un communique de resultats, meme si son titre parle de strategie, de
# dividende ou de financement (Coface, Nexans, Sanofi...). Les calendriers
# n'ont pas de %, les objectifs d'un plan ("croissance de 5 a 7 %") pas de
# hausse ou de baisse.
_CHIFFRE_CLE = re.compile(
    r"(chiffre d'affaires|resultat net|resultat operationnel|ebitda|marge operationnelle|croissance organique|"
    r"bnpa|revenus locatifs|net bookings|revenue|net income|operating income|organic growth|sales)"
    r"[^.%]{0,90}?(?:[+-]\s?\d|(?:hausse|baisse|progression|recul|up|down)\s(?:de\s|of\s|by\s)?\d)"
    r"[\d.,]*\s?%")


def chiffres_de_resultats(texte):
    return bool(texte) and bool(_CHIFFRE_CLE.search(normaliser(texte[:700])))


def categorie(c, texte):
    """Type du communique : rubrique AMF, titre, sinon en-tete du texte."""
    titre = normaliser(c["titre"])
    for rubrique, cid in RUBRIQUES.items():
        if rubrique in titre:
            return cid
    sources = [] if generique(c["titre"]) else [titre]
    if texte:
        sources.append(normaliser(entete(texte) + " " + texte[:400]))
    for n in sources:
        for cid, _, mots in CATEGORIES:
            if cid == "resultats" and _trouve(n, FAUX_RESULTATS):
                continue
            if _trouve(n, mots):
                # Communique de comptes qui releve ou abaisse aussi ses
                # objectifs : c'est une publication de resultats.
                if cid == "revision" and re.search(r"\b(resultats?|results|chiffre d'affaires)\b", n) and \
                        not _trouve(n, ["preliminaire", "preliminary", "avertissement", "warning"]):
                    return "resultats"
                # Pas les acquisitions : elles citent les chiffres de la cible.
                if cid not in ("resultats", "revision", "trafic", "operation") and chiffres_de_resultats(texte) and \
                        not _trouve(normaliser(texte[:700]), FAUX_RESULTATS):
                    return "resultats"
                return cid
    if c["sous_type"] in AMF_RAPPORTS or (chiffres_de_resultats(texte)
                                          and not _trouve(normaliser(texte[:700]), FAUX_RESULTATS)):
        return "resultats"
    return "autre"


def periode(c, texte):
    """Publication de resultats : annuels, semestriels, trimestriels ou
    chiffre d'affaires seul."""
    n = normaliser(("" if generique(c["titre"]) else c["titre"]) + " " + entete(texte or "") + " "
                   + (texte or "")[:300])
    ca_seul = _trouve(n, ["chiffre d'affaires", "ventes", "revenue", "sales", "produit net bancaire",
                          "activite du", "information financiere"]) and not _trouve(
        n, ["resultat", "results", "earnings", "benefice", "ebitda", "marge", "profit"])
    if ca_seul:
        return "chiffre_affaires"
    if _trouve(n, ["annuel", "exercice 20", "annee 20", "full-year", "full year", "fy20", "de l'annee"]):
        return "annuels"
    if _trouve(n, ["semestr", "half-year", "half year", "first half", "premier semestre", "1er semestre",
                   "h1 ", "s1 20", "1s20", "1s 20"]):
        return "semestriels"
    if _trouve(n, ["trimestr", "quarter", "9 mois", "neuf mois", "nine months", "q1", "q2", "q3", "q4",
                   "1t20", "2t20", "3t20", "4t20", "t1 20", "t2 20", "t3 20", "t4 20"]):
        return "trimestriels"
    return "autres"


def sens_mots(titre, texte):
    """Sens estime par mots-cles sur le titre et le debut du texte : (sens,
    score). Sens None quand le texte n'est pas nettement oriente."""
    n = normaliser((titre or "") + " " + (texte or "")[:2000])
    p = sum(n.count(m) for m in POSITIF)
    q = sum(n.count(m) for m in NEGATIF)
    if p + q == 0:
        return None, 0.0
    score = (p - q) / (p + q)
    if score >= SEUIL_SENS and p >= 2:
        return "positif", round(score, 2)
    if score <= -SEUIL_SENS and q >= 2:
        return "negatif", round(score, 2)
    return None, round(score, 2)


# ---------------------------------------------------------------------------
# Liste AMF
# ---------------------------------------------------------------------------

def telecharger_liste(isins, debut):
    """Communiques AMF des societes (toutes langues) depuis `debut`."""
    liste = ", ".join(f"'{i}'" for i in isins)
    types = ", ".join(f'"{t}"' for t in AMF_TYPES)
    where = (f"identificationsociete_iso_cd_isi IN ({liste}) AND informationdeposee_inf_dat_emt >= '{debut}'"
             f" AND sous_type_d_information IN ({types})")
    params = {"where": where, "select": (
        "uin_idt_uin,informationdeposee_inf_dat_emt,identificationsociete_iso_cd_isi,"
        "informationdeposee_inf_tit_inf,sous_type_d_information,informationdeposee_inf_lng_inf,"
        "url_de_recuperation,identificationdiffuseur_idi_cod_dif")}
    brut = json.loads(http_get(AMF_EXPORT + "?" + urllib.parse.urlencode(params), timeout=600))
    return [{
        "id": r["uin_idt_uin"],
        "isin": r["identificationsociete_iso_cd_isi"],
        "publie_le": r["informationdeposee_inf_dat_emt"],
        "langue": "en" if (r.get("informationdeposee_inf_lng_inf") or "").lower().startswith("angl") else "fr",
        "diffuseur": r.get("identificationdiffuseur_idi_cod_dif") or "",
        "sous_type": r.get("sous_type_d_information") or "",
        "titre": (r.get("informationdeposee_inf_tit_inf") or "").strip(),
        "url": r.get("url_de_recuperation") or "",
    } for r in brut if r.get("uin_idt_uin") and r.get("informationdeposee_inf_dat_emt")]


def versions_francaises(communiques):
    """Le meme communique est souvent depose en francais et en anglais : garde
    le francais, et l'anglais seulement sans version francaise de la meme
    societe dans l'heure (societes qui ne publient qu'en anglais)."""
    fr = {}
    for c in communiques:
        if c["langue"] == "fr":
            fr.setdefault(c["isin"], []).append(dt.datetime.fromisoformat(c["publie_le"]))
    garde = []
    for c in communiques:
        if c["langue"] == "en":
            t = dt.datetime.fromisoformat(c["publie_le"])
            if any(abs((t - u).total_seconds()) <= 3600 for u in fr.get(c["isin"], [])):
                continue
        garde.append(c)
    return garde


def rapports_redondants(communiques, jours=45, classes=None):
    """Rapports financiers deposes apres le communique de resultats (souvent
    quelques jours plus tard) : pas une information nouvelle. Ids des rapports
    precedes d'un communique de resultats de la meme societe dans les
    `jours` precedents (le meme jour, ils forment un seul evenement).
    Un communique que Claude a juge "autre" (principaux indicateurs,
    calendrier...) ne compte pas : sinon la publication trimestrielle qui le
    suit (TotalEnergies au premier et au troisieme trimestre) serait perdue."""
    if classes is None:
        classes = charger_classes()["classes"]
    publications = {}
    for c in communiques:
        if (c["categorie"] in ("resultats", "revision") and c["sous_type"] not in AMF_RAPPORTS
                and classes.get(c["id"], {}).get("type") != "autre"):
            publications.setdefault(c["isin"], []).append(c["publie_le"][:10])
    res = set()
    for c in communiques:
        if c["sous_type"] not in AMF_RAPPORTS:
            continue
        jour = c["publie_le"][:10]
        limite = (dt.date.fromisoformat(jour) - dt.timedelta(days=jours)).isoformat()
        if any(limite <= d < jour for d in publications.get(c["isin"], [])):
            res.add(c["id"])
    return res


def classer(c, texte):
    """Type, periode des resultats, sens par mots-cles et en-tete."""
    c["texte_lu"] = bool(texte)
    c["entete"] = entete(texte or "")
    c["categorie"] = categorie(c, texte)
    c["periode"] = periode(c, texte) if c["categorie"] == "resultats" else None
    titre = "" if generique(c["titre"]) else c["titre"]
    c["sens_mots"], c["score_mots"] = sens_mots(titre, texte)
    return c


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

CHAMPS = ["id", "isin", "publie_le", "langue", "diffuseur", "sous_type", "titre", "entete", "categorie",
          "periode", "sens_mots", "score_mots", "texte_lu", "url"]


def charger(chemin, defaut=None):
    if not os.path.exists(chemin):
        return defaut
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def url_type(c):
    """Adresse habituelle du PDF d'un communique (la plupart la suivent)."""
    return (f"https://fr.ftp.opendatasoft.com/datadila/INFOFI/{c['diffuseur']}/{c['id'][-8:-4]}/"
            f"{c['id'][-4:-2]}/FC{c['diffuseur']}{c['id']}.pdf")


def charger_communiques():
    """communiques.json, chaque communique en dictionnaire (le fichier les
    range en listes de valeurs, dans l'ordre de "champs")."""
    doc = charger(FICHIER)
    if doc is None:
        return None
    champs = doc.get("champs")
    if champs:
        doc["communiques"] = [dict(zip(champs, ligne)) for ligne in doc["communiques"]]
    for c in doc["communiques"]:
        c["url"] = c.get("url") or url_type(c)
        c["entete"] = c.get("entete") or ""
    return doc


def ecrire_communiques(doc):
    """Un communique par ligne (liste de valeurs) : fichier compact, lisible,
    et differences courtes dans l'historique git. L'adresse du PDF n'est
    ecrite que si elle sort de l'adresse habituelle, l'en-tete du texte que
    pour les titres generiques."""
    with open(FICHIER, "w", encoding="utf-8") as f:
        f.write("{\n")
        for k in ("version", "depuis", "maj_le", "extracteur", "isins"):
            f.write(f" {json.dumps(k)}: {json.dumps(doc[k], ensure_ascii=False)},\n")
        f.write(f' "champs": {json.dumps(CHAMPS)},\n')
        f.write(' "communiques": [\n')
        lignes = []
        for c in doc["communiques"]:
            ligne = dict(c, url=None if c.get("url") == url_type(c) else c.get("url"),
                         entete=c.get("entete") if generique(c["titre"]) or not c["titre"] else None)
            lignes.append("  " + json.dumps([ligne.get(k) for k in CHAMPS], ensure_ascii=False,
                                            separators=(",", ":")))
        f.write(",\n".join(lignes))
        f.write("\n ]\n}\n")


def charger_classes():
    return charger(CLASSES, {"version": 1, "classes": {}})


def ecrire_classes(doc):
    with open(CLASSES, "w", encoding="utf-8") as f:
        f.write('{\n "version": 1,\n "classes": {\n')
        lignes = [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                  for k, v in sorted(doc["classes"].items(), key=lambda kv: (kv[0][-8:], kv[0]))]
        f.write(",\n".join(lignes))
        f.write("\n }\n}\n")


# ---------------------------------------------------------------------------
# Avis de Claude sur les publications de resultats
# ---------------------------------------------------------------------------

# Format d'un avis (voir CONSIGNES.md, section "Publications de resultats").
SENS_RESULTATS = ["positif", "negatif", "mitige"]
DIRECTIONS = ["+", "-", "="]
PERSPECTIVES = ["relevees", "confirmees", "abaissees", "nouvelles"]
ATTENTES = ["superieures", "conformes", "inferieures"]
TYPES_AVIS = ["resultats", "revision", "autre"]


def valider_avis(cid, a):
    """Erreurs de format d'un avis de Claude (liste vide si valide)."""
    e = []
    if a.get("type") not in TYPES_AVIS:
        e.append(f"{cid} : type {a.get('type')!r}, attendu {TYPES_AVIS}")
    if a.get("type") == "autre":
        return e
    # Sens null : publication sans chiffres lisibles (sommaire d'un rapport...).
    if a.get("sens") not in SENS_RESULTATS + [None]:
        e.append(f"{cid} : sens {a.get('sens')!r}, attendu {SENS_RESULTATS} ou null")
    for champ in ("activite", "rentabilite"):
        if a.get(champ) not in DIRECTIONS + [None]:
            e.append(f"{cid} : {champ} {a.get(champ)!r}, attendu {DIRECTIONS} ou null")
    if a.get("perspectives") not in PERSPECTIVES + [None]:
        e.append(f"{cid} : perspectives {a.get('perspectives')!r}, attendu {PERSPECTIVES} ou null")
    if a.get("attentes") not in ATTENTES + [None]:
        e.append(f"{cid} : attentes {a.get('attentes')!r}, attendu {ATTENTES} ou null")
    if a.get("consensus") not in ["superieur", "conforme", "inferieur", None]:
        e.append(f"{cid} : consensus {a.get('consensus')!r}, attendu superieur, conforme, inferieur ou null")
    for champ in ("exceptionnel", "actionnaires"):
        if not isinstance(a.get(champ, False), bool):
            e.append(f"{cid} : {champ} doit etre true ou false")
    return e


def integrer(chemin):
    """Ajoute a resultats_classes.json les avis d'un fichier {"id": avis}
    (ou une liste d'avis portant chacun leur "id")."""
    nouveaux = charger(chemin)
    if isinstance(nouveaux, list):
        nouveaux = {a["id"]: a for a in nouveaux}
    nouveaux = {k.removeprefix("amf-"): {c: v for c, v in a.items() if c != "id"} for k, a in nouveaux.items()}
    from communiques_usa import valider_chiffres, valider_objectifs
    erreurs = [x for k, a in nouveaux.items()
               for x in valider_avis(k, a) + valider_chiffres(k, a) + valider_objectifs(k, a)]
    if erreurs:
        sys.exit("Avis invalides :\n - " + "\n - ".join(erreurs))
    doc = charger_classes()
    jour = dt.date.today().isoformat()
    for k, a in nouveaux.items():
        doc["classes"][k] = dict(a, le=a.get("le") or jour)
    ecrire_classes(doc)
    print(f"resultats_classes.json : {len(nouveaux)} avis ajoute(s), {len(doc['classes'])} au total.")


def phrases_cles(texte, mots, limite):
    """Phrases du texte qui contiennent l'un des mots, jusqu'a `limite`
    caracteres."""
    res, total = [], 0
    for p in re.split(r"(?<=[.;!?])\s+|\n", texte or ""):
        n = normaliser(p)
        if len(p) > 25 and any(m in n for m in mots) and p not in res:
            res.append(p.strip())
            total += len(p)
            if total >= limite:
                break
    return res


PERSPECTIVES_MOTS = ["objectif", "perspective", "prevision", "guidance", "outlook", "attend", "anticipe",
                     "confirme", "releve", "rehausse", "abaisse", "revise", "ambition", "vise ", "expects",
                     "target", "reaffirm", "confirm", "raise", "lower"]


def extrait_resultats(texte, n=900):
    """Debut du texte (chiffres cles) et phrases sur les perspectives, pour
    l'avis de Claude."""
    debut = " ".join((texte or "").split("\n"))[:n]
    persp = phrases_cles((texte or "")[n:], PERSPECTIVES_MOTS, 400)
    return debut + ((" [...] " + " ".join(persp)[:450]) if persp else "")


_DOCUMENTS = ("mise a disposition", "mise en ligne", "availability of", "comptes consolides", "etats financiers",
              "informations financieres consolidees", "financial statements", "financial report", "sommaire")


def document_joint(c):
    """Avis de mise a disposition d'un document ou etats financiers bruts :
    pas le communique de resultats lui-meme."""
    n = normaliser((lire_texte(c, None) or "")[:250])
    return any(x in n for x in _DOCUMENTS)


def a_classer(communiques, referentiel):
    """Publications de resultats et revisions d'objectifs sans avis de
    Claude : un communique par jour et par societe (le communique de presse
    plutot que le rapport financier du meme jour)."""
    import consensus as consensus_mod  # releve des analystes (facultatif)
    classes = charger_classes()["classes"]
    estimations = consensus_mod.charger().get("entreprises", {})
    noms = {e["isin"]: (e["ticker"], e["nom"]) for e in referentiel}
    redondants = rapports_redondants(communiques, classes=classes)
    par_jour = {}
    for c in communiques:
        if c["categorie"] not in ("resultats", "revision") or c["isin"] not in noms or c["id"] in redondants:
            continue
        par_jour.setdefault((c["isin"], c["publie_le"][:10]), []).append(c)
    liste = []
    for (isin, jour), cs in sorted(par_jour.items(), key=lambda kv: kv[0][1]):
        # Un avis de resultats couvre la journee ; un avis "autre" (calendrier,
        # document mis en ligne...) ne vaut que pour son communique.
        if any(classes.get(c["id"], {}).get("type") in ("resultats", "revision") for c in cs):
            continue
        cs = [c for c in cs if c["id"] not in classes]
        if not cs:
            continue
        # Communique de presse (information privilegiee ou trimestrielle)
        # avant le rapport financier semestriel ou annuel et avant l'avis de
        # mise a disposition ou les etats financiers publies a la meme heure,
        # francais avant anglais, texte lu avant titre seul.
        c = min(cs, key=lambda c: (c["sous_type"] in AMF_RAPPORTS - {"Information financière trimestrielle"},
                                   document_joint(c), c["langue"] != "fr", not c["texte_lu"], c["publie_le"]))
        texte = lire_texte(c, None) or ""
        ticker, nom = noms[isin]
        liste.append({"id": c["id"], "ticker": ticker, "nom": nom, "publie_le": c["publie_le"],
                      "categorie": c["categorie"], "periode": c["periode"],
                      "titre": c["entete"] if generique(c["titre"]) and c["entete"] else c["titre"],
                      "url": c["url"], "extrait": extrait_resultats(texte),
                      # Consensus des analystes la veille : a comparer aux
                      # chiffres publies (champ "consensus" de l'avis).
                      "consensus": consensus_mod.avant(estimations.get(ticker), jour)})
    return liste


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--depuis", default=DEPUIS, help="debut de l'historique")
    p.add_argument("--complet", action="store_true", help="retelecharge toute la liste depuis --depuis")
    p.add_argument("--sans-texte", action="store_true", help="ne lit pas les PDF (type d'apres le titre)")
    p.add_argument("--reclasser", action="store_true", help="recalcule type et sens des communiques en cache")
    p.add_argument("--a-classer", action="store_true",
                   help="ecrit sortie/resultats_a_classer.json (resultats sans avis de Claude)")
    p.add_argument("--integrer", metavar="FICHIER", help="ajoute des avis de Claude a resultats_classes.json")
    p.add_argument("--processus", type=int, default=8, help="telechargements en parallele")
    args = p.parse_args()

    if args.integrer:
        integrer(args.integrer)
        return

    referentiel = charger(os.path.join(ICI, "referentiel.json"))["entreprises"]
    isins = sorted(e["isin"] for e in referentiel)
    doc = charger_communiques()
    if doc is None or doc.get("depuis") != args.depuis:
        doc = {"version": 1, "depuis": args.depuis, "communiques": []}
        args.complet = True
    connus = {c["id"]: c for c in doc["communiques"]}

    if args.a_classer:
        liste = a_classer(list(connus.values()), referentiel)
        os.makedirs(SORTIE, exist_ok=True)
        with open(os.path.join(SORTIE, "resultats_a_classer.json"), "w", encoding="utf-8") as f:
            json.dump({"genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "communiques": liste}, f, ensure_ascii=False, indent=1)
        print(f"sortie/resultats_a_classer.json : {len(liste)} publication(s) sans avis de Claude.")
        return

    # Liste AMF : tout l'historique, ou les derniers jours seulement (le
    # passe ne change pas) ; une societe ajoutee au referentiel est reprise
    # depuis le debut.
    nouvelles_societes = sorted(set(isins) - set(doc.get("isins") or isins))
    debut = args.depuis
    if not args.complet and connus:
        dernier = max(c["publie_le"] for c in connus.values())[:10]
        debut = (dt.date.fromisoformat(dernier) - dt.timedelta(days=RECOUVREMENT)).isoformat()
    brut = telecharger_liste(isins, debut)
    if nouvelles_societes and not args.complet:
        brut += telecharger_liste(nouvelles_societes, args.depuis)
    # Le choix de la langue depend des deux versions : il se fait sur la
    # liste brute des jours relus.
    garde = [c for c in versions_francaises(brut) if not exclu(c)]
    nouveaux = [c for c in garde if c["id"] not in connus]
    print(f"Communiqués AMF : {len(brut)} depuis le {debut}, {len(nouveaux)} nouveau(x).", file=sys.stderr)

    outil = None if args.sans_texte else extracteur()
    os.makedirs(TEXTES, exist_ok=True)
    a_lire = nouveaux + (list(connus.values()) if args.reclasser else [])

    def traiter(c):
        return classer(c, lire_texte(c, outil))

    faits = 0
    with cf.ThreadPoolExecutor(max(1, args.processus)) as ex:
        for c in ex.map(traiter, a_lire):
            connus[c["id"]] = c
            faits += 1
            if faits % 500 == 0:
                print(f"  {faits}/{len(a_lire)} communiqués lus…", file=sys.stderr)
    # Une societe retiree du referentiel sort de l'etude.
    communiques = sorted((c for c in connus.values() if c["isin"] in set(isins)),
                         key=lambda c: (c["publie_le"], c["id"]))
    doc.update(version=1, depuis=args.depuis, isins=isins,
               maj_le=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               extracteur=outil or "aucun", communiques=communiques)
    ecrire_communiques(doc)
    lus = sum(1 for c in communiques if c["texte_lu"])
    print(f"communiques.json : {len(communiques)} communiqué(s), {lus} lu(s) ({outil or 'titre seul'}), "
          f"{len(nouveaux)} nouveau(x).")


if __name__ == "__main__":
    main()
