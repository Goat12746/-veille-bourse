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
fusionner.py                valide et publie alertes.json (+ etat.json)
      |
      v
Application (onglet Veille) lit alertes.json à l'adresse configurée
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
