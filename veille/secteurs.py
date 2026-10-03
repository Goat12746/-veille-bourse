"""Grands secteurs (11, sur le modele de la classification GICS) : ceux des
entreprises americaines (univers_objectifs.json) et regroupement des secteurs
detailles des entreprises francaises (referentiel.json). Sert a comparer une
entreprise a son secteur, dans son seul marche (etude_amf.py).
"""

SECTEURS = ("Industrie", "Technologie", "Finance", "Santé", "Consommation discrétionnaire",
            "Consommation de base", "Services publics", "Immobilier", "Matériaux", "Énergie", "Communication")

# Secteur detaille du referentiel francais -> grand secteur.
FRANCE = {
    # Matériaux
    "Acier": "Matériaux", "Acier inoxydable": "Matériaux", "Chimie": "Matériaux",
    "Chimie de spécialités": "Matériaux", "Ciment": "Matériaux", "Emballages en verre": "Matériaux",
    "Gaz industriels": "Matériaux", "Mines et métallurgie": "Matériaux", "Minéraux industriels": "Matériaux",
    "Arômes et parfums": "Matériaux",
    # Consommation de base
    "Agroalimentaire": "Consommation de base", "Grande distribution": "Consommation de base",
    "Spiritueux": "Consommation de base", "Cosmétiques": "Consommation de base",
    "Biens de consommation": "Consommation de base", "Parfums": "Consommation de base",
    # Consommation discrétionnaire
    "Automobile": "Consommation discrétionnaire", "Équipement automobile": "Consommation discrétionnaire",
    "Pneumatiques": "Consommation discrétionnaire", "Hôtellerie": "Consommation discrétionnaire",
    "Luxe": "Consommation discrétionnaire", "Luxe, vins et spiritueux": "Consommation discrétionnaire",
    "Bateaux de plaisance": "Consommation discrétionnaire", "Camping-cars": "Consommation discrétionnaire",
    "Petit électroménager": "Consommation discrétionnaire", "Jeux d'argent": "Consommation discrétionnaire",
    "Restauration collective": "Consommation discrétionnaire",
    # Énergie
    "Pétrole": "Énergie", "Pétrole, gaz, électricité": "Énergie", "Raffinage, carburants": "Énergie",
    "Ingénierie énergétique": "Énergie", "Tubes pour l'énergie": "Énergie", "Géosciences": "Énergie",
    "Technologie de transport du GNL": "Énergie", "Distribution d'énergie": "Énergie",
    # Services publics
    "Énergie": "Services publics", "Eau, déchets, énergie": "Services publics",
    # Finance
    "Banque": "Finance", "Assurance": "Finance", "Assurance-crédit": "Finance", "Réassurance": "Finance",
    "Bourses": "Finance", "Gestion d'actifs": "Finance", "Investissement": "Finance", "Paiements": "Finance",
    "Titres-restaurant, paiements": "Finance", "Titres-restaurant, avantages salariés": "Finance",
    # Santé
    "Pharmacie": "Santé", "Vaccins": "Santé", "Diagnostic": "Santé", "Laboratoires d'analyses": "Santé",
    "Équipements biopharmaceutiques": "Santé", "Maisons de retraite, cliniques": "Santé",
    "Santé animale": "Santé", "Optique": "Santé",
    # Technologie
    "Logiciels": "Technologie", "Semi-conducteurs": "Technologie", "Services informatiques": "Technologie",
    "Étiquettes électroniques": "Technologie", "Ingénierie et conseil technologique": "Technologie",
    # Communication
    "Télécoms": "Communication", "Télévision": "Communication", "Médias": "Communication",
    "Publicité": "Communication", "Publicité extérieure": "Communication", "Jeux vidéo": "Communication",
    "Satellites": "Communication",
    # Immobilier
    "Immobilier": "Immobilier", "Immobilier commercial": "Immobilier", "Immobilier de bureaux": "Immobilier",
    "Immobilier de bureaux et hôtels": "Immobilier", "Immobilier logistique": "Immobilier",
    "Promotion immobilière": "Immobilier",
    # Industrie
    "Aéronautique, défense": "Industrie", "Défense, électronique": "Industrie",
    "Matériel ferroviaire": "Industrie", "Concessions, BTP": "Industrie", "Concessions, BTP, énergie": "Industrie",
    "BTP, télécoms, médias": "Industrie", "Aéroports": "Industrie", "Tunnel sous la Manche": "Industrie",
    "Transport aérien": "Industrie", "Logistique": "Industrie", "Location longue durée de véhicules": "Industrie",
    "Location-entretien de linge": "Industrie", "Recyclage, services aux entreprises": "Industrie",
    "Services techniques": "Industrie", "Certification": "Industrie", "Centres de contacts": "Industrie",
    "Distribution électrique": "Industrie", "Câbles": "Industrie", "Équipement électrique": "Industrie",
    "Équipements électriques, matériaux": "Industrie", "Holding (logistique, médias)": "Industrie",
    "Études de marché": "Industrie", "Matériaux de construction": "Industrie",
}


def grand_secteur(secteur):
    """Grand secteur d'un secteur (detaille en France, deja grand aux
    Etats-Unis). None si inconnu."""
    if secteur in SECTEURS:
        return secteur
    return FRANCE.get(secteur)
