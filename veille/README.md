# Veille réglementaire et financière

Alimente l'onglet **Veille** de l'application : alertes sur les textes et
annonces susceptibles de faire bouger le cours d'une entreprise du CAC 40 ou du
SBF 120, et calendrier (budget, examen des textes, résultats, dividendes).

## Fonctionnement

```
collecte.py  (sans IA)      sources publiques -> sortie/candidats.json
      |                     AMF, amendements et agenda de l'Assemblée,
      |                     Google Actualités, dates Yahoo
      v
Claude Code (abonnement)    lit les candidats, écarte les faux positifs,
      |                     regroupe, évalue sens / ampleur / probabilité
      |                     -> sortie/analyse.json   (voir CONSIGNES.md)
      v
fusionner.py                valide et publie alertes.json (+ etat.json),
      |                     archive toutes les alertes dans historique.json
      v
consensus.py, positions.py  consensus des analystes (Yahoo), ventes à
communiques.py (sans IA)    découvert (AMF), communiqués AMF ; Claude juge
      |                     les nouvelles publications de résultats
      v
statistiques.py (sans IA)   réaction des cours aux alertes, référence AMF
      |                     -> statistiques.json
      v
objectifs.py, scores.py     objectifs de cours des analystes (Yahoo,
      |                     Zonebourse) comparés au cours à 12 mois
      |                     -> scores.json
      v
Application (onglets Actualités et Statistiques) lit alertes.json,
statistiques.json et scores.json à l'adresse configurée
```

| Fichier | Rôle |
|---|---|
| `referentiel.json` | Entreprises suivies (ticker Yahoo, ISIN, indice). **À compléter** pour tout le SBF 120. |
| `themes.json` | Thèmes : mots-clés → entreprises concernées, requêtes presse, mots de polarité. |
| `collecte.py` | Collecte et filtre par mots-clés. Bibliothèque standard Python uniquement. |
| `CONSIGNES.md` | Instructions de la tâche Claude Code (règles d'analyse, format). |
| `valider.py` | Contrôle du format de `sortie/analyse.json` et de `alertes.json`. |
| `fusionner.py` | Publication de `alertes.json`, mémoire des candidats traités (`etat.json`). |
| `alertes.json` | Fichier lu par l'application (aussi intégré à l'APK comme copie de secours). |
| `historique.json` | Archive de toutes les alertes publiées, sans limite de durée (étape et probabilité initiales comprises). |
| `statistiques.py` | Étude d'événements : réaction des cours aux alertes (écart au CAC 40 corrigé du bêta, en σ), points, tests ; référence sur l'historique des communiqués AMF depuis 2019. Sans IA. |
| `statistiques.json` | Résultats lus par l'onglet Statistiques (aussi intégré à l'APK). |
| `communiques.py` | Communiqués AMF du CAC 40 et du SBF 120 depuis 2019 (`communiques.json`) ; avis de Claude sur les publications de résultats (`resultats_classes.json`). |
| `etude_amf.py` | Étude de la réaction des cours aux communiqués : scénarios des résultats, facteurs, prévision (appelée par `statistiques.py`). |
| `consensus.py` | Relevé quotidien du consensus des analystes (Yahoo) : BPA et chiffre d'affaires attendus, révisions, surprises (`consensus.json`, historique gratuit limité à 90 jours et 4 trimestres, puis construit jour après jour). |
| `avant_ouverture.py` | Résultats publiés depuis la dernière clôture : part de hausse historique selon le consensus et les perspectives, pour les alertes de résultats de la veille du matin (avant 9 h). |
| `objectifs.py` | Objectifs de cours des analystes (`objectifs.json`) : relevé quotidien Yahoo (moyen, médian, haut, bas, note, recommandations) ; historique de l'objectif moyen depuis 2021 lu une fois dans le graphique de la page Consensus de Zonebourse (S&P Global Market Intelligence), à relancer à la main pour une entreprise ajoutée au référentiel. |
| `scores.py` | Scores des objectifs à 12 mois : écart du cours à l'objectif à l'échéance, objectif atteint, bon sens, par tranche de potentiel, année, secteur et entreprise (`scores.json`). Sans IA. |
| `univers.py` | Actions des autres indices de l'application pour les scores des objectifs (STOXX 600 via les positions de l'ETF iShares, S&P 500 et Nasdaq-100 via Wikipedia, étiquettes DAX et Euro Stoxx 50) : `univers_objectifs.json`. À la main. |
| `objectifs.py --univers monde --zonebourse --reprise` puis `scores.py --univers monde` | Scores des objectifs des autres indices, lus une seule fois depuis le PC (environ 10 heures de lecture Zonebourse, reprise possible) : `objectifs_monde.json`, `scores_monde.json` (synthèse par zone) et `scores_monde/<ticker>.json` (détail chargé par l'application à l'ouverture d'une entreprise). Les routines n'y touchent pas : calcul figé jusqu'à la prochaine relance à la main. |
| `secteurs.py` | Onze grands secteurs (modèle GICS) : ceux des entreprises américaines et regroupement des secteurs détaillés français, pour la probabilité du jour de publication (position face au consensus comparée aux publications du même secteur et du même marché). |
| `edgar.py` | Publications de résultats des entreprises américaines (S&P 500, Nasdaq-100) depuis 2019 d'après la SEC : dépôts 8-K item 2.02 à l'heure de New York, période publiée d'après les comptes déposés (`resultats_usa.json`) ; les résultats ne sont jugés que face au consensus. Sans IA. |
| `etude_usa.py` | Réaction des cours aux publications américaines, face au S&P 500 : même étude que `etude_amf.py` (`communiques_usa.json`, avec `consensus.py --zone usa` → `consensus_usa.json`). |
| `positions.py` | Ventes à découvert déclarées à l'AMF depuis 2012 (data.gouv.fr) : part du capital par entreprise et par jour (`positions_courtes.json`). |

## Statistiques

`python statistiques.py` (environ 30 s la première fois, cours et communiqués
mis en cache dans `.cache/`). Pour chaque entreprise d'une alerte :

- **jour de la réaction** : première séance qui clôture (17 h 35) après la
  publication de l'information ; sans heure connue, le jour même et le
  lendemain. `collecte.py` conserve donc l'heure de publication (`publie_le`)
  des communiqués AMF et des articles ;
- **écart au CAC 40** : rendement de l'action moins celui attendu d'après le
  CAC 40 et son bêta (modèle de marché sur les 250 séances précédentes),
  exprimé en σ (écart habituel d'une séance) : bruit sous 1 σ, net de 1 à 2 σ,
  fort au-delà ;
- **points** : sens +1 / 0 / −1, ampleur +1 / 0 / −1 (détail dans l'onglet) ;
- **tests** : t de Student sur les écarts standardisés, test du signe, test de
  rang, corrélation de Spearman ; score de Brier des probabilités une fois les
  mesures tranchées.

Ne sont pas notées : les entreprises visées par une autre alerte aux mêmes
séances, les sens neutres ou incertains, et les alertes générales (10
entreprises ou plus, comparées au CAC 40). Une alerte publiée après la
réaction du marché est signalée : seules celles publiées avant (tâche du
matin) testent une vraie prévision.

Les clôtures sont prises en compte après 17 h 45 : lancée à 7 h, la tâche
quotidienne mesure les réactions de la veille. Pour un calcul dès le soir,
planifier aussi `python statistiques.py --sans-historique` vers 18 h (sans IA,
il ne consomme pas de quota) puis publier `statistiques.json`.

## Lancer à la main

```bash
cd veille
python collecte.py --jours 3          # ~30 s, télécharge ~300 Mo d'amendements (mis en cache)
# puis, dans Claude Code : « Suis les consignes de veille/CONSIGNES.md »
# ou sans IA :
python fusionner.py --sans-ia         # alertes brutes, sens indicatif par mots-clés
```

Options de `collecte.py` : `--sans-amendements` (test rapide),
`--sans-calendrier` (pas d'appels Yahoo).

## Automatiser avec l'abonnement Claude

**1. Publier le dossier sur GitHub.** L'application doit pouvoir lire
`alertes.json` sans authentification : le plus simple est un **dépôt public
dédié** contenant uniquement ce dossier `veille/` (il ne contient aucune donnée
personnelle, vos portefeuilles restent dans l'application). Adresse à mettre
dans l'application (onglet Veille → ⚙) :

```
https://raw.githubusercontent.com/<compte>/<depot>/main/veille/alertes.json
```

**2a. Tâche planifiée dans le cloud (PC éteint).** Dans Claude Code, `/schedule`,
puis créer une tâche quotidienne (ex. 7 h, du lundi au vendredi) sur ce dépôt
avec l'instruction : « Suis les consignes de veille/CONSIGNES.md ».
Vérifier que l'environnement cloud a accès au réseau pour
`data.assemblee-nationale.fr`, `info-financiere.gouv.fr`,
`fr.ftp.opendatasoft.com`, `news.google.com`, `*.yahoo.com` et le push GitHub.

**2b. Alternative sur le PC (Planificateur de tâches Windows).** Tâche
quotidienne qui exécute, dans le dossier du dépôt :

```
claude -p "Suis les consignes de veille/CONSIGNES.md" --permission-mode acceptEdits
```

Le PC doit être allumé à l'heure prévue.

## Limites connues (premier jet)

- **Référentiel** : 40 valeurs du CAC 40 (composition Wikipédia) et 18 du SBF 120.
  Airbus, ArcelorMittal et Stellantis ne publient pas via l'AMF.
- **Sénat et Journal officiel** : pas encore collectés (la base Ameli du Sénat
  est un dump PostgreSQL ; Légifrance demande une clé PISTE gratuite).
- **Amendements de la seconde partie du budget** : lien générique (format
  d'adresse non vérifié).
- **Presse** : Google Actualités ne donne que les titres, avec du bruit ; c'est
  l'analyse de Claude qui trie.
- **Dates de résultats Yahoo** : indicatives (`fiable: false`), souvent
  inexactes pour l'Europe.
- **Quota d'abonnement** : pendant l'examen du budget, des centaines
  d'amendements par jour peuvent passer le filtre ; resserrer `themes.json` si
  l'analyse devient trop longue.
- Aucune alerte n'est un conseil d'investissement.
