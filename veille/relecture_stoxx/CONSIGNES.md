# Relecture des perspectives du STOXX 600

`extraits_communiques.json` (Royaume-Uni, pays nordiques : phrases du communiqué
de résultats qui parlent des perspectives, après le titre) et
`extraits_presse.json` (autres pays : titres de presse du jour de la
publication) donnent pour chaque publication (`<ticker>|<date>`) un extrait en
anglais. Une seule valeur par publication, d'après l'extrait seul :

- `relevees` : l'entreprise relève ses objectifs ou prévisions déjà annoncés
  (« raises », « lifts », « upgrades », « now expects … above the previous
  range », milieu de fourchette relevé, « outlook raised »).
- `abaissees` : elle les abaisse (« cuts », « lowers », « now expects … below »,
  avertissement sur résultats, retrait des prévisions, milieu de fourchette
  abaissé).
- `confirmees` : elle les maintient (« confirms », « reiterates », « maintains »,
  « on track », « in line with guidance », fourchette resserrée autour du même
  milieu, « expectations unchanged »).
- `nouvelles` : premiers objectifs d'une nouvelle période, sans objectifs
  précédents à comparer (résultats annuels qui donnent les prévisions de
  l'exercice suivant, « initiates 2027 guidance », premiers objectifs de moyen
  terme).
- `null` : aucune prévision explicite, généralités (« we expect continued
  demand »), ou seulement un poste secondaire (impôt, investissements, charges).

Règles :
- Plusieurs éléments de sens différents : retenir celui qui porte sur le
  résultat opérationnel, le bénéfice par action ou le chiffre d'affaires de
  l'exercice.
- Un titre de presse qui dit seulement que les résultats battent ou manquent
  les attentes n'est pas une perspective : `null`.
- Presse : « raises/lifts/upgrades outlook » est `relevees`, « cuts/lowers/
  slashes/warns » est `abaissees`, « confirms/reiterates/keeps/sticks to »
  est `confirmees`, « sees/expects/targets … for 2027 » sans comparaison est
  `nouvelles` ; une prévision d'analyste n'est pas une perspective de
  l'entreprise.
- Presse : plusieurs entreprises mêmes noms (Siemens, Siemens Energy, Siemens
  Healthineers) : ne retenir que les titres de l'entreprise relue.
- Juger seulement d'après l'extrait, sans chercher ailleurs.

Les verdicts vont dans `perspectives_claude.json` (`python relecture_stoxx.py
--integrer FICHIER`, fichier `{id: valeur ou null}`), prioritaires dans
`etude_stoxx.py`.
