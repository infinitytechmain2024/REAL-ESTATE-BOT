"""Municipalities of the provinces the campaign cities stand for, by ``source_place.AREAS`` area (Q13).

``bot/campaign/architect.py`` ``GAZETTEER`` knows only the seven cities a campaign can target. A portal may file a
listing under a district of the capital («Ciudad Universitaria, Madrid») while its own text says «finca … en
Móstoles»: to see that, ``source_place`` needs the towns around those cities. This list is deliberately short (the
larger and the coastal municipalities of each province), written as the portals write them; a town it does not know
is never a conflict, and so is a name that is also a common word («Ronda», «Silla», «Camas», «Pizarra»). The province capitals are not here: «Madrid», «Valencia» or «Málaga» in a text may mean the
province, so they never move a listing (``source_place.AREAS`` covers them). A name that contains another area's
name («Sevilla la Nueva» in Madrid) is left out. Pure data, no I/O.
"""

from __future__ import annotations

MUNICIPALITIES: dict[str, tuple[str, ...]] = {
    "Madrid": (
        "Móstoles", "Alcalá de Henares", "Fuenlabrada", "Leganés", "Getafe", "Alcorcón", "Torrejón de Ardoz", "Parla",
        "Alcobendas", "Las Rozas de Madrid", "Las Rozas", "San Sebastián de los Reyes", "Pozuelo de Alarcón",
        "Rivas-Vaciamadrid", "Coslada", "Valdemoro", "Majadahonda", "Collado Villalba", "Aranjuez", "Arganda del Rey",
        "Boadilla del Monte", "Pinto", "Colmenar Viejo", "Tres Cantos", "San Fernando de Henares", "Galapagar",
        "Arroyomolinos", "Villaviciosa de Odón", "Navalcarnero", "Ciempozuelos", "Torrelodones",
        "Paracuellos de Jarama", "Mejorada del Campo", "Algete", "Villanueva de la Cañada", "Villanueva del Pardillo",
        "Brunete", "Humanes de Madrid", "Chinchón", "Guadarrama", "San Lorenzo de El Escorial", "El Escorial",
        "Moralzarzal", "Navacerrada", "Cercedilla", "Collado Mediano", "Daganzo", "Griñón", "El Molar",
        "Morata de Tajuña", "Villarejo de Salvanés",
    ),
    "Cataluña": (
        "L'Hospitalet de Llobregat", "Hospitalet de Llobregat", "Badalona", "Terrassa", "Sabadell", "Mataró",
        "Santa Coloma de Gramenet", "Cornellà de Llobregat", "Sant Boi de Llobregat", "Sant Cugat del Vallès",
        "Sant Cugat", "Rubí", "Manresa", "Vilanova i la Geltrú", "Viladecans", "Castelldefels", "Granollers",
        "Cerdanyola del Vallès", "Mollet del Vallès", "El Prat de Llobregat", "Gavà", "Esplugues de Llobregat",
        "Sitges", "Igualada", "Sant Adrià de Besòs", "Ripollet", "Montcada i Reixac", "Vilafranca del Penedès",
        "Sant Feliu de Llobregat", "Premià de Mar", "El Masnou", "Alella", "Calella", "Pineda de Mar",
        "Malgrat de Mar", "Arenys de Mar", "Canet de Mar", "Caldes de Montbui", "Martorell", "Olesa de Montserrat",
        "Sant Andreu de la Barca", "Molins de Rei", "Valldoreix", "Bellaterra", "Teià", "Vallirana", "Begues",
        "Corbera de Llobregat", "Cubelles",
    ),
    "Comunidad Valenciana": (
        # Valencia province
        "Torrent", "Gandía", "Paterna", "Sagunto", "Sagunt", "Alzira", "Mislata", "Burjassot", "Ontinyent", "Xàtiva",
        "Aldaia", "Manises", "Alaquàs", "Xirivella", "Sueca", "Cullera", "Quart de Poblet", "Catarroja", "Paiporta",
        "Picassent", "Bétera", "Puçol", "Puzol", "Massamagrell", "Alboraya", "Alboraia", "Riba-roja de Túria",
        "L'Eliana", "La Pobla de Vallbona", "Godella", "Rocafort", "Requena", "Chiva", "Buñol", "Llíria",
        "Algemesí", "Carcaixent", "Alfafar", "Náquera", "Canet d'en Berenguer",
        # Alicante province
        "Elche", "Elx", "Torrevieja", "Orihuela", "Benidorm", "Alcoy", "Alcoi", "San Vicente del Raspeig", "Elda",
        "Dénia", "Jávea", "Xàbia", "Santa Pola", "Villena", "Petrer", "Calpe", "Calp", "Altea", "Villajoyosa",
        "La Vila Joiosa", "Crevillent", "Novelda", "El Campello", "Mutxamel", "San Juan de Alicante",
        "Sant Joan d'Alacant", "Pilar de la Horadada", "Rojales", "Guardamar del Segura", "Finestrat",
        "L'Alfàs del Pi", "Alfaz del Pi", "Teulada", "Moraira", "Benissa", "Benitachell", "Polop",
        "La Nucía", "Busot", "Monforte del Cid",
    ),
    "Andalucía": (
        # Málaga province (Marbella is a campaign city, but not a province: a text naming it means the town)
        "Marbella", "Mijas", "Fuengirola", "Torremolinos", "Benalmádena", "Estepona", "Vélez-Málaga",
        "Rincón de la Victoria", "Antequera", "Alhaurín de la Torre", "Alhaurín el Grande", "Nerja", "Coín",
        "Cártama", "Manilva", "Casares", "Benahavís", "Torrox", "Álora", "Ojén", "Istán", "Frigiliana", "Alcaucín",
        "Tolox", "Archidona",
        # Sevilla province
        "Dos Hermanas", "Alcalá de Guadaíra", "Utrera", "Mairena del Aljarafe", "Écija", "La Rinconada",
        "Los Palacios y Villafranca", "Coria del Río", "Carmona", "Tomares", "Bormujos",
        "San Juan de Aznalfarache", "Lebrija", "Morón de la Frontera", "Sanlúcar la Mayor", "Espartinas",
        "Castilleja de la Cuesta", "Mairena del Alcor", "Brenes", "Lora del Río", "Osuna", "Gelves",
        "Valencina de la Concepción", "Salteras", "Umbrete", "La Algaba", "Arahal", "Marchena",
    ),
}
