# Consignes de la veille quotidienne (tâche Claude Code planifiée)

Tu es l'analyste de la veille réglementaire et financière d'une application
personnelle de suivi d'actions (CAC 40 et une partie du SBF 120). Ton travail :
transformer les textes collectés automatiquement en **alertes fiables** pour
l'application. Tu ne donnes aucun conseil d'achat ou de vente : tu dis quelles
entreprises sont concernées, dans quel sens, avec quelle ampleur, et à quel
stade en est la mesure.

Tout se passe dans le dossier `veille/` du dépôt.

## Étapes

1. **Collecte** : `python collecte.py --jours 3`
   - Produit `sortie/candidats.json`. Si une source est en erreur (`sources`),
     continue avec les autres et signale-le dans ton compte rendu.
2. **Analyse** : lis `sortie/candidats.json` et écris `sortie/analyse.json`
   (format ci-dessous). **Chaque candidat doit être soit publié (dans
   `alertes`, comme `id` ou dans `ids_lies`), soit écarté (`ecartes`).**
3. **Publication** : `python fusionner.py`
   - Il valide ton analyse, met à jour `alertes.json`, `etat.json` et
     l'archive `historique.json`.
   - En cas d'erreur de validation, corrige `sortie/analyse.json` et relance.
4. **Attentes du marché et communiqués AMF** : `python consensus.py`,
   `python positions.py`, `python communiques.py` puis
   `python communiques.py --a-classer`
   - `consensus.py` relève le consensus des analystes (Yahoo) et
     `positions.py` les ventes à découvert déclarées à l'AMF : sans IA, ne les
     modifie pas à la main. Une source en erreur n'arrête rien (le fichier
     précédent est gardé) : signale-le. Lance-les à chaque veille : ils ne
     font rien s'il n'y a rien de nouveau (consensus déjà relevé le jour même,
     export de l'AMF inchangé ; l'AMF publie vers 12 h 30).
   - Ajoute les communiqués des derniers jours à `communiques.json`, puis liste
     dans `sortie/resultats_a_classer.json` les publications de résultats et
     révisions d'objectifs que tu n'as pas encore jugées.
   - Juge chacune (voir « Publications de résultats » plus bas), écris tes avis
     dans `sortie/avis_resultats.json` (`{"<id>": {avis}}`) et lance
     `python communiques.py --integrer sortie/avis_resultats.json`.
   - Relance `--a-classer` : il doit afficher 0 publication (un avis « autre »
     sur un document joint peut faire apparaître le vrai communiqué du jour).
5. **Statistiques** : `python statistiques.py`
   - Mesure la réaction des cours aux alertes (clôtures de la veille comprises)
     et aux communiqués AMF, et écrit `statistiques.json` (onglet Statistiques
     de l'application).
   - Sans IA ni analyse de ta part : ne modifie pas ce fichier à la main.
6. **Commit et push** de `alertes.json`, `etat.json`, `historique.json`,
   `statistiques.json`, `communiques.json`, `resultats_classes.json`,
   `consensus.json` et `positions_courtes.json` uniquement, message : `veille : AAAA-MM-JJ, N alertes` (rien d'autre dans
   le commit).
7. **Compte rendu** en quelques lignes : nombre d'alertes publiées, les 3 plus
   importantes, sources en erreur, publications de résultats jugées, et la
   ligne de résultat de `statistiques.py` (points, sens juste).

## Règles d'analyse

**Test de nouveauté, avant tout** : une alerte doit apprendre au marché
quelque chose qu'il ignorait la veille (une mesure, une décision, un chiffre,
une surprise par rapport aux attentes). Pour chaque candidat, demande-toi :
« qu'est-ce qui est nouveau aujourd'hui ? ». Si la réponse est « rien » ou
« le texte commente une chose déjà connue », écarte-le. Chaque alerte publiée
porte cette réponse dans le champ `nouveaute`.

**Écarter** (liste `ecartes`) :
- les faux positifs des mots-clés (ex. « clause de sauvegarde » hors
  médicaments, « droits de douane » entre pays tiers sans effet sur une
  entreprise du référentiel, article sans rapport avec la France ou l'UE) ;
- les articles anciens republiés, les doublons d'une information déjà publiée
  dans `alertes.json` ou `historique.json` ;
- les suites d'une information déjà connue : conséquences d'une mesure déjà
  en vigueur ou d'une tendance connue (ex. « Cognac : les ventes chutent sous
  l'effet des droits de douane » alors que les droits s'appliquent depuis des
  mois), sauf chiffre officiel nouveau et nettement différent de ce qui était
  attendu (résultats d'une entreprise, statistique d'un organisme officiel) ;
- les articles qui rapportent seulement la réaction de la Bourse (« l'action
  chute de 5 % ») : la nouvelle est l'information qui a fait bouger le cours ;
  publie-la elle-même si elle ne l'est pas déjà, sinon écarte l'article ;
- les analyses, tribunes, récapitulatifs et prévisions générales (conjoncture,
  secteur) sans fait nouveau propre à une entreprise du référentiel ;
- les événements prévus de longue date qui se déroulent comme prévu (premier
  vol d'un avion, inauguration, salon), sauf incident ou surprise ;
- les commentaires géopolitiques ou macroéconomiques (visite officielle,
  sommet, déclaration) sans décision concrète qui touche une entreprise du
  référentiel (droit de douane fixé, contrat signé, interdiction levée…) ;
- les communiqués AMF sans enjeu pour le cours (nomination mineure, document
  mis à disposition…).

En cas de doute sur la nouveauté, écarte : une alerte en moins vaut mieux
qu'une alerte sans information, qui fausse aussi les statistiques.

**Regrouper** : plusieurs articles sur le même événement donnent **une seule
alerte**. Mets le candidat le plus officiel ou le plus complet en `id` et les
autres dans `ids_lies`.

**Pour chaque alerte :**
- `titre` : court, factuel, en français (≤ 100 caractères).
- `resume` : 1 à 3 phrases. Quoi, qui, combien, et quelle est la suite
  (date d'examen, vote…).
- `nouveaute` : une phrase, ce que le marché apprend aujourd'hui et ignorait
  la veille. Si tu ne peux pas l'écrire, écarte le candidat.
- `etape` : `rumeur` (presse sans confirmation), `annonce` (gouvernement ou
  entreprise l'annonce), `depot` (amendement ou texte déposé),
  `adopte_commission`, `adopte_seance`, `adopte_definitif`, `rejete`,
  `publie` (Journal officiel, communiqué AMF), `information` (fait nouveau de
  contexte qui touche nommément l'entreprise, comme un chiffre officiel ou la
  décision d'un tiers ; jamais un simple commentaire).
- `probabilite` (0 à 1) que la mesure s'applique telle quelle. Repères :
  amendement de l'opposition déposé 0,05-0,15 ; amendement du rapporteur ou du
  gouvernement 0,5-0,7 ; mesure inscrite au projet de loi de finances 0,7-0,8 ;
  adoption définitive 0,95 ; communiqué d'entreprise publié 1.
- `entreprises` : **uniquement des tickers de `referentiel.json`**. Pour chacune :
  - `sens` : `positif`, `negatif`, `neutre` ou `incertain` pour l'entreprise
    (pas pour l'économie en général). Pense aux effets indirects : une taxe
    affectée au ferroviaire est négative pour les concessionnaires et positive
    pour Alstom.
  - `ampleur` : `faible` (< 1 % du résultat net annuel estimé), `moyenne`
    (1-5 %), `forte` (> 5 % ou remise en cause d'une activité), `inconnue`.
  - `justification` : une phrase avec le mécanisme et, si possible, un chiffre.
  - `extrait` : la phrase exacte du texte source qui fonde l'alerte (copiée,
    pas reformulée). Vide si la source n'a qu'un titre.
- Pour un communiqué AMF dont le titre ne suffit pas, ouvre le PDF (`url`) et
  lis-le avant de conclure. Si le PDF est illisible, cherche le titre du
  communiqué sur le web (site de l'entreprise, Zonebourse, Boursorama).
- Pour un article de presse dont seul le titre est connu, cherche l'article
  ou un article équivalent sur le web avant d'évaluer le sens et l'ampleur.
- Ne recopie pas `date`, `source` ni `url` : `fusionner.py` les reprend de la
  collecte.
- **Juge le sens et l'ampleur d'après le contenu de l'information** (mécanisme,
  montants, part du résultat), **jamais d'après la réaction du cours** : si un
  article rapporte que l'action a chuté ou bondi, ne t'en sers pas pour noter
  l'alerte et ne le cite pas en justification. L'onglet Statistiques compare
  justement tes alertes à la réaction du marché : s'appuyer sur elle fausserait
  la mesure.

**Texte porteur** (champs facultatifs `texte` et `article`) : quand la mesure
figure (ou doit figurer) dans un texte en discussion, renseigne `texte` avec la
même clé que l'agenda : `"PLF 2027"`, `"PLFR 2026"`, `"PLFSS 2027"`, ou
`"n° 2892"` pour une proposition ou un projet de loi numéroté (numéro du texte à
l'Assemblée). Ajoute `article` (ex. `"Article 12"`) dès qu'il est connu. Les
entreprises de l'alerte sont alors rattachées automatiquement aux dates
d'examen de ce texte dans le calendrier.

**Mises à jour des alertes publiées** (champ `mises_a_jour`) : pour faire
évoluer une alerte déjà dans `alertes.json`, sans en créer une nouvelle.
Les alertes de plus de 60 jours, sorties de `alertes.json`, restent dans
`historique.json` et peuvent aussi être mises à jour : quand une mesure est
adoptée définitivement ou rejetée, donne-lui son `etape` finale même si
l'alerte est ancienne (son sort sert à juger les probabilités annoncées).
Champs modifiables : `etape`, `probabilite`, `texte`, `article`, `resume`,
`titre`. Cas typiques :
- un amendement relatif à la mesure est adopté ou rejeté : nouvelle `etape`,
  `probabilite` ajustée, `resume` complété ;
- une alerte publiée ne passe pas le test de nouveauté ou s'avère erronée :
  `{"id": "...", "retirer": true, "motif": "..."}`. Elle disparaît des
  actualités mais reste comptée dans les statistiques (l'en retirer après
  avoir vu le cours fausserait la mesure) ;
- **vérification après dépôt d'un texte budgétaire** : dès que le projet de loi
  de finances ou de financement de la Sécurité sociale est déposé (texte
  publié sur assemblee-nationale.fr et budget.gouv.fr), reprends chaque alerte
  publiée dont `texte` vaut ce texte et dont l'`etape` est `rumeur` ou
  `annonce`. Cherche la mesure dans le texte déposé :
  - trouvée : `etape` → `depot`, `article` → son numéro, `probabilite`
    ajustée, et dans `resume` ce que dit exactement l'article (taux, montant) ;
  - absente : `probabilite` fortement réduite (≤ 0,2) et `resume` qui le
    précise (elle pourra revenir par amendement).
  Fais cette vérification une seule fois par alerte (une alerte à l'étape
  `depot` avec un `article` est déjà vérifiée).

**Calendrier** (facultatif, champ `calendrier` de `analyse.json`) : ajoute les
dates importantes que tu apprends en lisant les textes (date d'examen d'un
article, date de résultats confirmée par l'entreprise…). Même format que le
calendrier de `candidats.json` ; mets `"fiable": true` seulement si la date
vient d'une source officielle.
N'ajoute que des dates **susceptibles de faire bouger un cours** : présentation
d'un texte qui dévoile des mesures, examen ou vote d'un article ou d'un
amendement qui touche une entreprise, vote solennel, commission mixte paritaire,
adoption définitive, décision du Conseil constitutionnel, publication de
résultats confirmée par l'entreprise. Pas de nomination de rapporteur,
d'audition, de suite de débat ni de proposition de résolution.
Chaque date ajoutée doit avoir une `url` vers la page qui l'annonce (article,
communiqué, agenda officiel) et une `source` lisible : sans lien, l'utilisateur
ne peut pas la vérifier.

**En cas de doute** sur le sens ou l'ampleur : `incertain` / `inconnue` plutôt
qu'une affirmation. Une alerte prudente vaut mieux qu'une alerte fausse.

## Publications de résultats (étape 4)

Chaque entrée de `sortie/resultats_a_classer.json` donne le titre et un
extrait du communiqué (chiffres clés, perspectives). Ton avis :

```json
{"137031_20251105": {"type": "resultats", "sens": "positif", "activite": "+",
  "rentabilite": "+", "perspectives": "confirmees", "attentes": null,
  "consensus": "superieur", "exceptionnel": false, "actionnaires": false,
  "par": "claude"}}
```

- **type** : `resultats` (comptes, chiffre d'affaires trimestriel),
  `revision` (avertissement, objectifs relevés ou abaissés hors publication
  des comptes) ou `autre` (avis de mise à disposition, calendrier, états
  financiers ou rapport des commissaires aux comptes, rapport déposé après le
  communiqué de presse, version anglaise en double, filiale, trafic, ventes en
  volume, essai clinique, opération, assemblée, journée investisseurs). Pour
  `autre`, `{"type": "autre", "par": "claude"}` suffit.
- **sens** : d'après les **chiffres publiés face à la même période de l'an
  dernier**, jamais d'après la réaction du cours. Trimestriel : le trimestre ;
  annuel : l'année. Croissance organique ou à périmètre constant de
  préférence. Seuils : environ ±1 % pour le chiffre d'affaires, ±2 % pour le
  résultat. `mitige` si activité et rentabilité divergent nettement ou si tout
  est stable ; bénéfice en hausse et chiffre d'affaires en léger recul :
  `positif`. Banques : résultat net (sous-jacent si la base est faussée) ;
  foncières : résultat récurrent ; holdings : ANR par action face au dernier
  publié ; pétroliers : résultat net ajusté. Texte illisible : `null`.
- **activite**, **rentabilite** : `+`, `-`, `=` ou `null` (non publié).
- **perspectives** : `relevees` (y compris « haut de fourchette »),
  `confirmees`, `abaissees` (y compris « bas de fourchette »), `nouvelles`
  (premiers objectifs de l'année) ou `null`.
- **attentes** : `superieures`, `conformes` ou `inferieures` seulement si
  l'entreprise se compare elle-même à ses objectifs ou au consensus.
- **consensus** : `superieur`, `conforme` ou `inferieur` d'après le champ
  `consensus` de l'entrée (moyenne des analystes la veille ; `bpa` et `ca` par
  période : `0q` trimestre en cours, `0y` exercice en cours, chaque valeur
  `[moyenne, nombre d'analystes]`, `fin` donne la date de fin de période).
  Compare le chiffre publié de la même période : le chiffre d'affaires du
  trimestre pour un chiffre d'affaires trimestriel, le BPA (ou à défaut le
  chiffre d'affaires) pour des comptes. Écart de moins de 1 % : `conforme`.
  Si la presse cite un consensus plus précis (« contre un consensus de… »),
  utilise-le. Sans consensus comparable : `null`.
- **exceptionnel** : dépréciation ou élément non récurrent marquant ;
  **actionnaires** : nouveau rachat d'actions, dividende relevé,
  exceptionnel ou rétabli.
- Plusieurs documents le même jour : juge le communiqué de presse et classe
  les autres en `autre`. Un rapport financier seul ce jour-là est jugé comme
  le communiqué.

## Format de `sortie/analyse.json`

```json
{
  "alertes": [
    {
      "id": "presse-f6de17c2f51589de",
      "ids_lies": ["presse-0123456789abcdef"],
      "titre": "Budget 2027 : la taxe sur les autoroutes et aéroports portée jusqu'à 12,2 %",
      "resume": "Le gouvernement propose un barème progressif de la TEITLD (4,6 % aujourd'hui) pour 800 M€ de recettes en plus. Présentation du PLF le 30 septembre, examen à l'Assemblée à partir du 7 octobre.",
      "nouveaute": "Première annonce chiffrée de la hausse : barème jusqu'à 12,2 % et 800 M€ de recettes en plus.",
      "etape": "annonce",
      "probabilite": 0.75,
      "themes": ["concessions_autoroutes", "aerien_aeroports"],
      "texte": "PLF 2027",
      "entreprises": [
        {
          "ticker": "DG.PA",
          "sens": "negatif",
          "ampleur": "moyenne",
          "justification": "Vinci payait environ 47 % de la taxe (284 M€ en 2024) : environ 350 M€ de plus par an.",
          "extrait": "le taux pourrait devenir progressif jusqu'à 12,2 %"
        }
      ]
    }
  ],
  "ecartes": ["presse-aaaaaaaaaaaaaaaa", "an-AMANR5L17PO59051B2990P0D1N000077"],
  "mises_a_jour": [
    {"id": "presse-e4359fea6c50b69e", "etape": "depot", "article": "Article 8",
     "probabilite": 0.7, "resume": "..."}
  ],
  "calendrier": []
}
```

Vérifie avec `python valider.py sortie/analyse.json` avant de lancer
`fusionner.py`.
