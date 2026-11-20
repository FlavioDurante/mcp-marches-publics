"""French administrative geography: regions, departments, accent-insensitive lookups.

BOAMP stores the department of the buyer (``code_departement``); this module lets the MCP
tools accept a region ("Occitanie") or department names ("Haute-Garonne") and turn them into
department codes.
"""

from __future__ import annotations

import re
import unicodedata

#: Department code → name (96 metropolitan departments incl. Corsica 2A/2B, plus 5 overseas).
DEPARTMENTS: dict[str, str] = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron", "13": "Bouches-du-Rhône",
    "14": "Calvados", "15": "Cantal", "16": "Charente", "17": "Charente-Maritime",
    "18": "Cher", "19": "Corrèze", "2A": "Corse-du-Sud", "2B": "Haute-Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir", "29": "Finistère",
    "30": "Gard", "31": "Haute-Garonne", "32": "Gers", "33": "Gironde", "34": "Hérault",
    "35": "Ille-et-Vilaine", "36": "Indre", "37": "Indre-et-Loire", "38": "Isère",
    "39": "Jura", "40": "Landes", "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire",
    "44": "Loire-Atlantique", "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne",
    "48": "Lozère", "49": "Maine-et-Loire", "50": "Manche", "51": "Marne",
    "52": "Haute-Marne", "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse",
    "56": "Morbihan", "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe", "73": "Savoie",
    "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime", "77": "Seine-et-Marne",
    "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme", "81": "Tarn", "82": "Tarn-et-Garonne",
    "83": "Var", "84": "Vaucluse", "85": "Vendée", "86": "Vienne", "87": "Haute-Vienne",
    "88": "Vosges", "89": "Yonne", "90": "Territoire de Belfort", "91": "Essonne",
    "92": "Hauts-de-Seine", "93": "Seine-Saint-Denis", "94": "Val-de-Marne",
    "95": "Val-d'Oise", "971": "Guadeloupe", "972": "Martinique", "973": "Guyane",
    "974": "La Réunion", "976": "Mayotte",
}  # fmt: skip

#: Region name → department codes (13 metropolitan regions, then overseas regions).
REGIONS: dict[str, list[str]] = {
    "Auvergne-Rhône-Alpes": [
        "01",
        "03",
        "07",
        "15",
        "26",
        "38",
        "42",
        "43",
        "63",
        "69",
        "73",
        "74",
    ],
    "Bourgogne-Franche-Comté": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "Bretagne": ["22", "29", "35", "56"],
    "Centre-Val de Loire": ["18", "28", "36", "37", "41", "45"],
    "Corse": ["2A", "2B"],
    "Grand Est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "Hauts-de-France": ["02", "59", "60", "62", "80"],
    "Île-de-France": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "Normandie": ["14", "27", "50", "61", "76"],
    "Nouvelle-Aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "Occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "Pays de la Loire": ["44", "49", "53", "72", "85"],
    "Provence-Alpes-Côte d'Azur": ["04", "05", "06", "13", "83", "84"],
    "Guadeloupe": ["971"],
    "Martinique": ["972"],
    "Guyane": ["973"],
    "La Réunion": ["974"],
    "Mayotte": ["976"],
}

#: Regex accepted for a department code (metropolitan, Corsica 2A/2B, overseas 971-976).
DEPARTMENT_PATTERN = r"^(0[1-9]|1[0-9]|2[1-9]|[3-8][0-9]|9[0-5]|2[AB]|97[1-6])$"

_ALIASES = {
    "paca": "Provence-Alpes-Côte d'Azur",
    "provence alpes cote d azur": "Provence-Alpes-Côte d'Azur",
    "idf": "Île-de-France",
    "ile de france": "Île-de-France",
    "reunion": "La Réunion",
}


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse punctuation into single spaces."""
    base = unicodedata.normalize("NFKD", text)
    base = "".join(c for c in base if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()


_REGION_INDEX = {normalize(name): name for name in REGIONS}
_REGION_INDEX.update({alias: name for alias, name in _ALIASES.items() if name in REGIONS})
_DEPT_INDEX = {normalize(name): code for code, name in DEPARTMENTS.items()}


def region_departments(region: str) -> list[str]:
    """Return the department codes of a region (accent and case insensitive).

    Args:
        region: Region name such as ``"Occitanie"``, ``"ile-de-france"`` or ``"PACA"``.

    Returns:
        Department codes of the region.

    Raises:
        ValueError: If the region is unknown (the message lists the valid names).
    """
    name = _REGION_INDEX.get(normalize(region))
    if name is None:
        raise ValueError(f"Unknown region {region!r}. Valid regions: {', '.join(REGIONS)}.")
    return list(REGIONS[name])


def department_code(value: str) -> str:
    """Resolve a department code or name to its canonical code.

    Args:
        value: ``"31"``, ``"2a"``, ``"Haute-Garonne"``, ``"haute garonne"``...

    Returns:
        The canonical code (e.g. ``"31"``, ``"2A"``, ``"974"``).

    Raises:
        ValueError: If the value is neither a known code nor a known name.
    """
    raw = value.strip()
    code = raw.upper()
    if code in DEPARTMENTS:
        return code
    if raw.isdigit() and len(raw) == 1 and f"0{raw}" in DEPARTMENTS:
        return f"0{raw}"
    found = _DEPT_INDEX.get(normalize(raw))
    if found is None:
        raise ValueError(
            f"Unknown department {value!r}. Use a code like '31', '2A', '974' or an exact name "
            "like 'Haute-Garonne'."
        )
    return found


def resolve_departments(
    region: str | None = None, departments: list[str] | None = None
) -> list[str] | None:
    """Merge a region and/or department codes/names into a sorted list of unique codes.

    Args:
        region: Optional region name.
        departments: Optional department codes or names.

    Returns:
        Sorted unique codes, or ``None`` when neither argument was given.

    Raises:
        ValueError: On an unknown region or department.
    """
    if region is None and not departments:
        return None
    codes: set[str] = set()
    if region:
        codes.update(region_departments(region))
    for item in departments or []:
        codes.add(department_code(item))
    return sorted(codes)
