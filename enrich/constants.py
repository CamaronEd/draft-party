"""ESPN numeric ID tables and shared position metadata.

ESPN's fantasy API returns integers for positions and pro teams. These tables
are stable across seasons and are the standard published mappings.
"""

# Verified empirically against a live IDP league rather than taken on faith:
# 9-13 are the individual-defensive-player slots, 15 is ESPN's "Team QB".
ESPN_POSITIONS = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    9: "DT",
    10: "DE",
    11: "LB",
    12: "CB",
    13: "S",
    15: "TQB",
    16: "DST",
}

# Sleeper and ESPN disagree on defensive labels (ESPN "DE" vs Sleeper "DL"),
# so the name bridge falls back to matching within these groups.
POSITION_GROUPS = {
    "QB": "OFF", "RB": "OFF", "WR": "OFF", "TE": "OFF", "K": "OFF", "FB": "OFF",
    "DT": "DEF", "DE": "DEF", "DL": "DEF", "NT": "DEF", "EDGE": "DEF",
    "LB": "DEF", "OLB": "DEF", "ILB": "DEF", "MLB": "DEF",
    "CB": "DEF", "S": "DEF", "SS": "DEF", "FS": "DEF", "DB": "DEF",
    "DST": "TEAM", "TQB": "TEAM",
}

ESPN_PRO_TEAMS = {
    0: "FA",
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WSH",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}

# Full team names, used for DST hype cards and highlight search queries.
TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WSH": "Washington Commanders",
}

# Primary accent color per position, used by the display for tier styling.
POSITION_COLORS = {
    "QB": "#ff2d6f",
    "RB": "#00e08a",
    "WR": "#2d9dff",
    "TE": "#ff9a2d",
    "K": "#b47cff",
    "DST": "#8a94a6",
    "TQB": "#ff2d6f",
    # IDP share a single defensive red so they read as one family on screen.
    "DT": "#ff6b4a", "DE": "#ff6b4a", "DL": "#ff6b4a", "EDGE": "#ff6b4a",
    "LB": "#ffd23f", "OLB": "#ffd23f", "ILB": "#ffd23f",
    "CB": "#4ad9ff", "S": "#4ad9ff", "SS": "#4ad9ff", "FS": "#4ad9ff", "DB": "#4ad9ff",
}
