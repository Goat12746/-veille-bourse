# Relecture des perspectives américaines

Chaque lot `relecture/lot_NN.json` contient 100 publications de résultats
d'entreprises américaines : `id` (numéro du dépôt SEC), `entreprise`,
`publication` et `extrait` (phrase titre et phrases du communiqué qui parlent
des perspectives, en anglais). Pour chacune, une seule valeur :

- `relevees` : l'entreprise relève ses objectifs ou ses prévisions déjà
  annoncés (« raises », « increases », « raises the low end », « now expects
  … above the prior range », milieu de fourchette relevé).
- `abaissees` : elle les abaisse (« lowers », « reduces », « cuts »,
  « now expects … below », milieu de fourchette abaissé, retrait des
  prévisions).
- `confirmees` : elle maintient ou réaffirme ses objectifs (« reaffirms »,
  « maintains », « confirms », « reiterates », fourchette resserrée autour
  du même milieu).
- `nouvelles` : premiers objectifs d'une nouvelle période (premier
  trimestre suivant, nouvel exercice) sans objectifs précédents à comparer
  (« for the fourth quarter, expects revenue of… », « initiates fiscal 2027
  guidance »).
- `null` : aucune prévision chiffrée ou explicite dans l'extrait, ou
  seulement des généralités (« we expect durable demand »), ou des mentions
  juridiques.

Règles :
- Si plusieurs éléments vont dans des sens différents, retiens celui qui
  porte sur le bénéfice par action ou le chiffre d'affaires de l'exercice ;
  à défaut, le plus important pour le titre.
- Une prévision d'un seul poste secondaire (taux d'impôt, investissements,
  charges) ne suffit pas : `null`, sauf si c'est la seule prévision et
  qu'elle est explicitement relevée ou abaissée.
- Juge seulement d'après l'extrait, sans chercher ailleurs.

Réponse : un seul fichier JSON `sortie/relecture_NN.json` (NN = numéro du
lot), objet `{"<id>": "<valeur ou null>"}` avec les 100 `id` du lot, sans
autre texte, par exemple
`{"0000002488-25-000163": "nouvelles", "0001104659-26-001234": null}`.
Puis `python relecture_usa.py --integrer sortie/relecture_NN.json`.
