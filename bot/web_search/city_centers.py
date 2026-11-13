"""IDEALISTA_STATIC_CENTERS (Q30): a small static list of city centres for the Idealista city circle (Q17).

The fallback of ``ApifyIdealistaPortalSource._city_circle`` when the cached geocoder (Redis + Nominatim) is absent or
gives no centre: without it "Valencia" goes to the actor by name and runs as València d'Àneu (Lleida). Province
capitals and the largest cities only. Centres are the town centre from public knowledge, to about 0.01 deg; boxes
(south, north, west, east) are rough municipal bounds, used only for the circle's radius (half the diagonal, clamped
to 3-30 km by the caller). A geocoder answer always wins over this list.
"""

from __future__ import annotations

from bot.web_search.geocoding import Bbox, _fold

# name -> ((lat, lon), (south, north, west, east))
_CENTERS: dict[str, tuple[tuple[float, float], Bbox]] = {
    # Madrid and the centre
    "Madrid": ((40.4168, -3.7038), (40.312, 40.643, -3.889, -3.518)),
    "Alcalá de Henares": ((40.4819, -3.3635), (40.440, 40.560, -3.450, -3.290)),
    "Toledo": ((39.8628, -4.0273), (39.810, 39.910, -4.150, -3.930)),
    "Ciudad Real": ((38.9848, -3.9274), (38.920, 39.040, -4.020, -3.860)),
    "Albacete": ((38.9943, -1.8585), (38.800, 39.200, -2.150, -1.600)),
    "Cuenca": ((40.0704, -2.1374), (39.950, 40.350, -2.300, -1.900)),
    "Guadalajara": ((40.6328, -3.1669), (40.580, 40.700, -3.240, -3.080)),
    # Catalonia
    "Barcelona": ((41.3874, 2.1686), (41.320, 41.470, 2.052, 2.229)),
    "L'Hospitalet de Llobregat": ((41.3597, 2.0997), (41.340, 41.380, 2.080, 2.140)),
    "Badalona": ((41.4500, 2.2474), (41.430, 41.500, 2.200, 2.290)),
    "Girona": ((41.9794, 2.8214), (41.940, 42.020, 2.770, 2.870)),
    "Lleida": ((41.6176, 0.6200), (41.550, 41.700, 0.520, 0.700)),
    "Tarragona": ((41.1189, 1.2445), (41.080, 41.180, 1.160, 1.340)),
    # Valencian Community
    "Valencia": ((39.4699, -0.3763), (39.278, 39.567, -0.433, -0.272)),
    "Alicante": ((38.3452, -0.4810), (38.270, 38.420, -0.590, -0.400)),
    "Castellón de la Plana": ((39.9864, -0.0513), (39.930, 40.060, -0.140, 0.050)),
    "Elche": ((38.2699, -0.7126), (38.150, 38.350, -0.820, -0.550)),
    "Torrevieja": ((37.9787, -0.6822), (37.930, 38.030, -0.750, -0.640)),
    "Benidorm": ((38.5411, -0.1225), (38.520, 38.580, -0.170, -0.080)),
    # Murcia
    "Murcia": ((37.9922, -1.1307), (37.800, 38.080, -1.310, -0.960)),
    "Cartagena": ((37.6257, -0.9966), (37.540, 37.720, -1.150, -0.800)),
    # Andalusia
    "Sevilla": ((37.3891, -5.9845), (37.300, 37.450, -6.033, -5.880)),
    "Málaga": ((36.7213, -4.4214), (36.650, 36.860, -4.570, -4.300)),
    "Marbella": ((36.5101, -4.8825), (36.470, 36.560, -5.010, -4.770)),
    "Estepona": ((36.4276, -5.1463), (36.380, 36.520, -5.280, -5.050)),
    "Fuengirola": ((36.5390, -4.6247), (36.520, 36.560, -4.650, -4.600)),
    "Torremolinos": ((36.6218, -4.4999), (36.590, 36.650, -4.540, -4.470)),
    "Córdoba": ((37.8882, -4.7794), (37.680, 38.100, -5.050, -4.580)),
    "Granada": ((37.1773, -3.5986), (37.130, 37.230, -3.680, -3.550)),
    "Almería": ((36.8340, -2.4637), (36.800, 36.960, -2.560, -2.320)),
    "Cádiz": ((36.5271, -6.2886), (36.440, 36.550, -6.320, -6.210)),
    "Jerez de la Frontera": ((36.6850, -6.1261), (36.500, 36.850, -6.250, -5.700)),
    "Algeciras": ((36.1408, -5.4562), (36.060, 36.200, -5.550, -5.420)),
    "Huelva": ((37.2614, -6.9447), (37.200, 37.320, -7.000, -6.880)),
    "Jaén": ((37.7796, -3.7849), (37.600, 37.900, -3.950, -3.650)),
    # Extremadura
    "Badajoz": ((38.8794, -6.9707), (38.700, 39.100, -7.150, -6.750)),
    "Cáceres": ((39.4753, -6.3724), (39.250, 39.650, -6.700, -6.150)),
    "Mérida": ((38.9161, -6.3437), (38.850, 38.990, -6.450, -6.250)),
    # Castile and León
    "Valladolid": ((41.6523, -4.7245), (41.560, 41.720, -4.830, -4.650)),
    "Burgos": ((42.3439, -3.6969), (42.300, 42.390, -3.800, -3.600)),
    "León": ((42.5987, -5.5671), (42.560, 42.640, -5.620, -5.520)),
    "Salamanca": ((40.9701, -5.6635), (40.930, 41.000, -5.720, -5.600)),
    "Zamora": ((41.5034, -5.7467), (41.440, 41.560, -5.840, -5.650)),
    "Palencia": ((42.0095, -4.5288), (41.950, 42.080, -4.620, -4.450)),
    "Ávila": ((40.6565, -4.6818), (40.580, 40.720, -4.800, -4.600)),
    "Segovia": ((40.9429, -4.1088), (40.890, 41.000, -4.180, -4.040)),
    "Soria": ((41.7636, -2.4649), (41.700, 41.850, -2.600, -2.380)),
    # Aragon, La Rioja, Navarre
    "Zaragoza": ((41.6488, -0.8891), (41.500, 41.850, -1.150, -0.700)),
    "Huesca": ((42.1401, -0.4089), (42.080, 42.200, -0.500, -0.330)),
    "Teruel": ((40.3456, -1.1065), (40.250, 40.450, -1.250, -0.950)),
    "Logroño": ((42.4627, -2.4450), (42.420, 42.500, -2.530, -2.380)),
    "Pamplona": ((42.8125, -1.6458), (42.780, 42.850, -1.700, -1.600)),
    # Basque Country, Cantabria, Asturias
    "Bilbao": ((43.2630, -2.9350), (43.213, 43.290, -2.986, -2.880)),
    "Vitoria-Gasteiz": ((42.8467, -2.6716), (42.750, 42.950, -2.850, -2.550)),
    "San Sebastián": ((43.3183, -1.9812), (43.270, 43.340, -2.070, -1.920)),
    "Santander": ((43.4623, -3.8099), (43.430, 43.500, -3.900, -3.760)),
    "Oviedo": ((43.3614, -5.8494), (43.290, 43.420, -5.980, -5.780)),
    "Gijón": ((43.5322, -5.6611), (43.460, 43.580, -5.770, -5.570)),
    # Galicia
    "A Coruña": ((43.3623, -8.4115), (43.320, 43.390, -8.460, -8.370)),
    "Lugo": ((43.0097, -7.5568), (42.900, 43.120, -7.700, -7.430)),
    "Ourense": ((42.3358, -7.8639), (42.290, 42.390, -7.940, -7.800)),
    "Pontevedra": ((42.4310, -8.6444), (42.360, 42.480, -8.720, -8.560)),
    "Vigo": ((42.2406, -8.7207), (42.150, 42.270, -8.800, -8.630)),
    "Santiago de Compostela": ((42.8782, -8.5448), (42.800, 42.950, -8.650, -8.420)),
    # Islands, Ceuta, Melilla
    "Palma": ((39.5696, 2.6502), (39.490, 39.660, 2.540, 2.800)),
    "Ibiza": ((38.9067, 1.4206), (38.890, 38.940, 1.390, 1.460)),
    "Las Palmas de Gran Canaria": ((28.1235, -15.4363), (28.030, 28.180, -15.520, -15.400)),
    "Santa Cruz de Tenerife": ((28.4636, -16.2518), (28.420, 28.590, -16.330, -16.120)),
    "Ceuta": ((35.8894, -5.3213), (35.870, 35.920, -5.390, -5.280)),
    "Melilla": ((35.2923, -2.9381), (35.265, 35.320, -2.970, -2.920)),
}

# Other names of the same municipality (co-official, Spanish/English, short forms). A short form that is also
# another town («Santa Cruz», «Santiago») is not listed.
_ALIASES: dict[str, str] = {
    "València": "Valencia", "Seville": "Sevilla", "Malaga": "Málaga", "Alacant": "Alicante",
    "Castelló de la Plana": "Castellón de la Plana", "Castellón": "Castellón de la Plana",
    "Castelló": "Castellón de la Plana", "Elx": "Elche", "Saragossa": "Zaragoza", "Bilbo": "Bilbao",
    "Vitoria": "Vitoria-Gasteiz", "Gasteiz": "Vitoria-Gasteiz", "Donostia": "San Sebastián",
    "Donostia-San Sebastián": "San Sebastián", "Iruña": "Pamplona", "Iruña/Pamplona": "Pamplona",
    "La Coruña": "A Coruña", "Coruña": "A Coruña", "Orense": "Ourense", "Lérida": "Lleida", "Gerona": "Girona",
    "Xixón": "Gijón", "Palma de Mallorca": "Palma", "Eivissa": "Ibiza", "Las Palmas": "Las Palmas de Gran Canaria",
    "L'Hospitalet": "L'Hospitalet de Llobregat", "Hospitalet de Llobregat": "L'Hospitalet de Llobregat",
    "Jerez": "Jerez de la Frontera",
}

_BY_NAME: dict[str, tuple[tuple[float, float], Bbox]] = {
    **{_fold(name): value for name, value in _CENTERS.items()},
    **{_fold(alias): _CENTERS[name] for alias, name in _ALIASES.items()},
}


def static_city_area(city: str) -> tuple[tuple[float, float], Bbox] | None:
    """The listed city's centre and rough municipal box, or None for a name not in the list."""
    return _BY_NAME.get(_fold(city)) if isinstance(city, str) and city.strip() else None
