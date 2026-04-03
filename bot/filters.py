"""Market filtering logic for the arbitrage bot.

Reuses category inference and crypto detection from the analysis scripts.
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone


def is_crypto_short_term(question: str) -> bool:
    """Detect 5min/15min crypto markets by question text.

    These markets have up to 1.56% taker fees, killing the 1% margin.
    Matches patterns like: 'Bitcoin Up or Down - February 28, 1:45AM-1:50AM ET'

    Must NOT match long-term markets with a single deadline time, e.g.
    'Will Bitcoin hit $100K by December 31, 2:00 PM ET?'
    """
    q = question if isinstance(question, str) else ""
    # Pattern 1: "Bitcoin Up or Down" — always short-term fee markets
    if re.search(
        r"(?i)(?:bitcoin|ethereum|solana|xrp|dogecoin|matic|btc|eth|sol|doge)\s+up\s+or\s+down",
        q,
    ):
        return True
    # Pattern 2: Time RANGE like "1:45AM-1:50AM" — distinctive 5min/15min pattern
    if re.search(
        r"(?i)(?:bitcoin|ethereum|solana|xrp|btc|eth|sol|doge)"
        r".*\d+:\d+\s*(?:AM|PM)\s*[-–]\s*\d+:\d+\s*(?:AM|PM)",
        q,
    ):
        return True
    # Pattern 3: Explicit short interval mentions
    if re.search(
        r"(?i)(?:bitcoin|ethereum|solana|xrp|btc|eth|sol|doge)"
        r".*\b(?:5|15)\s*[-\s]?(?:min|minute)",
        q,
    ):
        return True
    return False


def _word_match(q: str, words: list[str]) -> bool:
    """Match keywords using word boundaries for short tokens to avoid
    false positives like 'eth ' matching 'Beth ' or 'sol ' matching 'Solomon'.
    """
    for w in words:
        if len(w.strip()) <= 4:
            # Short tokens need word-boundary matching
            if re.search(r'\b' + re.escape(w.strip()) + r'\b', q):
                return True
        else:
            if w in q:
                return True
    return False


# Compiled regex patterns for sports betting market formats
_SPORTS_PATTERNS = [
    # Spread format: "Team Name (-2.5)" or "Team (+1.5)"
    re.compile(r'\([+-]\d+\.5\)', re.IGNORECASE),
    # "Team A vs Team B" or "Team A v Team B" (common match format)
    re.compile(r'\b\w+\s+(?:vs\.?|v\.?)\s+\w+', re.IGNORECASE),
    # "to win" as in "Team to win match/game/series"
    re.compile(r'\bto\s+win\b.*\b(?:match|game|series|set|round|fight|bout)\b', re.IGNORECASE),
    # "Will X win on YYYY-MM-DD?" — Polymarket sports match format
    re.compile(r'will\s+.+\s+win\s+on\s+\d{4}-\d{2}-\d{2}', re.IGNORECASE),
    # "FC", "AFC", "CFC", "SC", "CF", "AC" club suffixes (e.g. "Genoa CFC", "FC Porto")
    re.compile(r'\b(?:FC|AFC|CFC|CF|AC|SC|SV|BVB|PSG|RB)\b'),
    # "United", "City", "Rovers", "Wanderers" — common club name parts
    re.compile(r'\b(?:United|Rovers|Wanderers|Athletic|Sporting|Dynamo|Real|Inter)\b(?!.*(?:states|nations|kingdom|airlines))', re.IGNORECASE),
    # "O/U X.5" — over/under player props (e.g. "LeBron James: Rebounds O/U 5.5")
    re.compile(r'\bO/U\s+\d+\.5\b', re.IGNORECASE),
    # Player prop patterns: "Name: Stat O/U" or "Name: Stat Over/Under"
    re.compile(r':\s*(?:points|rebounds|assists|strikeouts|hits|yards|tackles|sacks|goals|saves)\b', re.IGNORECASE),
    # Common South American / European club prefixes
    re.compile(r'\b(?:CA|CD|CF|CR|CS|FK|NK|SK|AS|SS|US)\s+[A-Z]', re.IGNORECASE),
]


def _is_sports_pattern(q: str) -> bool:
    """Check regex patterns that indicate sports betting markets."""
    return any(p.search(q) for p in _SPORTS_PATTERNS)


def infer_category(question: str) -> str:
    """Infer market category from question text."""
    q = question.lower() if isinstance(question, str) else ""

    if _word_match(q, [
        # Sports / leagues
        "tennis", "football", "soccer", "nba", "nfl", "nhl",
        "mlb", "ufc", "mma", "boxing", "cricket", "f1",
        "formula", "grand prix", "atp", "wta", "ncaa",
        "premier league", "champions league", "la liga",
        "bundesliga", "serie a", "ligue 1", "copa",
        "super bowl", "world cup", "olympics",
        "europa league", "mls", "liga mx", "eredivisie",
        "primeira liga", "süper lig", "j-league", "k-league",
        "afl", "nrl", "ipl", "pga", "lpga", "wwe",
        "six nations", "rugby", "cycling", "tour de france",
        "nascar", "indycar", "motogp",
        # Betting market formats
        "spread:", "moneyline", "over/under", "handicap",
        "total goals", "total points", "total runs",
        "total assists", "total rebounds", "total touchdowns",
        "total strikeouts", "total aces", "total kills",
        "total corners", "total cards", "total sets",
        "winner:", "match winner", "game winner",
        "clean sheet", "both teams to score",
        "first to score", "last to score",
        "half-time", "halftime", "full-time", "fulltime",
        "1st half", "2nd half", "1st quarter", "1st set",
        "home team", "away team", "home win", "away win",
        # Competition stages
        "semifinal", "semi-final", "quarterfinal", "quarter-final",
        "round of 16", "group stage", "playoff", "play-off",
        "wild card", "divisional round", "conference finals",
        # Game actions
        "touchdown", "home run", "strikeout", "three-pointer",
        "hat trick", "hat-trick", "goal scorer",
        "assists leader", "mvp award",
    ]):
        return "Sports"

    # Regex patterns for sports betting formats (use original case for club suffixes)
    if _is_sports_pattern(question if isinstance(question, str) else ""):
        return "Sports"

    if _word_match(q, [
        "dota", "counter-strike", "cs2", "csgo", "league of legends",
        "lol", "valorant", "esport",
    ]):
        return "Esports"

    if _word_match(q, [
        "bitcoin", "btc", "ethereum", "eth", "crypto",
        "xrp", "solana", "sol", "dogecoin", "doge",
    ]):
        return "Crypto"

    if _word_match(q, [
        "trump", "biden", "election", "congress", "senate",
        "president", "democrat", "republican", "political",
        "governor", "mayor", "vote", "poll", "legislation",
        "executive order",
    ]):
        return "Politics"

    if _word_match(q, [
        "stock", "s&p", "nasdaq", "dow jones", "fed", "fomc",
        "interest rate", "inflation", "gdp", "unemployment",
        "earnings", "revenue", "ipo", "market cap",
        "cpi", "ppi", "payroll", "jobs report", "nonfarm",
        "treasury", "bond", "yield", "forex",
        "tariff", "trade deficit", "trade surplus",
        "oil price", "gold price", "commodity",
        "housing starts", "retail sales", "consumer confidence",
        "pmi", "manufacturing index",
    ]):
        return "Economics/Finance"

    if _word_match(q, [
        "oscar", "grammy", "emmy", "movie", "album",
        "celebrity", "taylor swift", "kanye", "elon musk",
        "twitter", "tiktok", "youtube",
    ]):
        return "Entertainment"

    if _word_match(q, [
        "weather", "temperature", "hurricane", "earthquake",
        "climate", "nasa", "rocket launch", "space launch",
    ]):
        return "Science/Weather"

    return "Other"


def parse_json_field(value, default=None):
    """Safely parse a JSON string field from API responses."""
    if default is None:
        default = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


def get_winning_token(market: dict) -> tuple[int | None, str, str]:
    """Extract winning token info from a resolved market.

    Returns (winning_idx, winning_outcome, winning_token_id) or (None, "", "").
    """
    outcome_prices = parse_json_field(market.get("outcomePrices", "[]"))
    outcomes = parse_json_field(market.get("outcomes", "[]"))
    token_ids = parse_json_field(market.get("clobTokenIds", "[]"))

    for i, price in enumerate(outcome_prices):
        try:
            if float(price) == 1.0:
                outcome = outcomes[i] if i < len(outcomes) else "Unknown"
                token_id = token_ids[i] if i < len(token_ids) else ""
                return i, outcome, token_id
        except (ValueError, TypeError):
            continue

    return None, "", ""


def passes_all_filters(market: dict, blocked_categories: list[str],
                       surprise_cutoff: float) -> tuple[bool, str]:
    """Run all filters on a market. Returns (passed, reason_if_filtered)."""
    question = market.get("question", "")

    # 1. Category check (blocklist — block risky categories)
    category = infer_category(question)
    if category in blocked_categories:
        return False, f"blocked_category={category}"

    # 2. Crypto short-term fee markets
    if is_crypto_short_term(question):
        return False, "crypto_short_term_fees"

    # 3. Surprise pricing filter
    ltp = market.get("lastTradePrice")
    if ltp is not None:
        try:
            if float(ltp) < surprise_cutoff:
                return False, f"surprise_price={ltp}"
        except (ValueError, TypeError):
            pass

    # 4. Must have order book enabled
    if not market.get("enableOrderBook", True):
        return False, "orderbook_disabled"

    # 5. Must be resolved with a winning token
    winning_idx, _, winning_token = get_winning_token(market)
    if winning_idx is None or not winning_token:
        return False, "not_resolved"

    return True, ""


# -----------------------------------------------------------------------
# Enhanced scanner filters (data-analysis-driven)
# -----------------------------------------------------------------------

_SUBJECTIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bsignificant(?:ly)?\b", r"\bsubstantial(?:ly)?\b", r"\bnotabl[ey]\b",
        r"\bmajor\b", r"\bgenerally\b", r"\bmostly\b", r"\bwidely\b",
        r"\baccepted\b", r"\bbest\b", r"\bworst\b",
        r"\bsuccessful(?:ly)?\b", r"\bfamous\b", r"\bwell[- ]known\b",
    ]
]

# Live-event behavior markets: outcome unknown until event happens in real-time.
# E.g. "Will Trump say X during speech", "Will player score Y in game"
_LIVE_EVENT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^will\b.+\bsay\b",            # "Will X say ..." (any context)
        r"^will\b.+\bpost\b",           # "Will X post ..." (Truth Social, Twitter, etc.)
        r"^will\b.+\btweet\b",          # "Will X tweet ..."
        r"\bmention\b",                 # "Will X mention ..." (any context)
        r"\bwear\b.+\bduring\b",        # "Will X wear ... during ..."
        r"\b\d+\s*times?\b.+\bduring\b",  # "... 15 times during ..."
        r"\bduring\b.+\bspeech\b",      # "during ... speech"
        r"\bduring\b.+\bvisit\b",       # "during ... visit"
        r"\bduring\b.+\binterview\b",   # "during ... interview"
        r"\bduring\b.+\bpress\s*conference\b",
        r"\bduring\b.+\baddress\b",
        r"\bduring\b.+\brally\b",
        r"\bsay\b.+\bin\s+(a\s+)?tweet\b",  # "Will X say ... in a tweet"
        # Pure speculation — unpredictable events with no data edge
        r"\boutage\b",                   # service outages
        r"\bhack(ed)?\b",               # security breaches
        r"\bbreach\b",                   # data breaches
        r"\bresign\b",                   # personnel decisions
        r"\bfired?\b",                   # personnel decisions
        r"\bstep\s*down\b",             # personnel decisions
        r"\barrest\b",                   # legal actions
        r"\bindict\b",                   # legal actions
        r"\bassassinat",                 # assassination
        r"\bkidnap",                     # kidnapping
        r"\bbankrupt",                   # bankruptcy filings
        r"\bdefault\b",                  # debt defaults
        r"\bcoup\b",                     # coups
        r"\bshutdown\b",                 # government shutdowns
        r"\bimpeach",                    # impeachment
        r"\bpardon\b",                   # presidential pardons
        r"\bexecut(e|ed|ion)\b",         # executions
        r"\bdeport",                     # deportations
        r"\bextraditi",                  # extraditions
        r"\bipo\b",                      # IPO filings — unpredictable timing
    ]
]


def is_live_event_market(question: str) -> bool:
    """Return True if the question describes a live-event behavior market.

    These markets resolve based on real-time events (speeches, visits, etc.)
    where the outcome is unknowable until the event occurs — similar risk
    to sports betting despite being categorized as Politics.
    """
    return any(p.search(question) for p in _LIVE_EVENT_PATTERNS)


def has_subjective_language(question: str, description: str) -> bool:
    """Return True if resolution criteria contain subjective language.

    Subjective criteria increase UMA dispute risk, which could result
    in full position loss.
    """
    text = f"{question} {description}"
    return any(p.search(text) for p in _SUBJECTIVE_PATTERNS)


def check_end_date_timing(end_date_str: str, grace_minutes: int) -> tuple[bool, str]:
    """Check if enough time has passed since end_date.

    Returns (passes, reason). Fails if end_date was less than grace_minutes ago.
    The UMA oracle has a 2-hour challenge period — we require at least 30 minutes
    to have passed before considering entry.
    """
    if not end_date_str:
        return False, "missing_end_date"
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        # Ensure timezone-aware (naive dates assumed UTC)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_minutes = (now - end_dt).total_seconds() / 60
        if elapsed_minutes < grace_minutes:
            return False, f"too_soon={elapsed_minutes:.1f}min"
        return True, ""
    except (ValueError, TypeError):
        return False, "invalid_end_date"


# City → UTC offset (hours). Covers Polymarket weather markets.
# Uses standard time; DST adds +1 for US cities Mar-Nov.
_CITY_UTC_OFFSETS: dict[str, float] = {
    # US — Eastern (EDT = UTC-4)
    "new york": -4, "nyc": -4, "new york city": -4,
    "boston": -4, "philadelphia": -4, "philly": -4,
    "washington": -4, "washington dc": -4, "dc": -4,
    "atlanta": -4, "miami": -4, "orlando": -4, "tampa": -4,
    "jacksonville": -4, "charlotte": -4, "raleigh": -4, "richmond": -4,
    "detroit": -4, "cleveland": -4, "columbus": -4, "cincinnati": -4,
    "pittsburgh": -4, "indianapolis": -4, "baltimore": -4,
    "buffalo": -4, "rochester": -4, "hartford": -4, "providence": -4,
    "norfolk": -4, "virginia beach": -4, "wilmington": -4,
    "charleston": -4, "savannah": -4, "knoxville": -4, "lexington": -4,
    "louisville": -4, "grand rapids": -4, "fort lauderdale": -4,
    "newark": -4, "jersey city": -4, "stamford": -4, "new haven": -4,
    "worcester": -4, "springfield": -4, "syracuse": -4, "albany": -4,
    "dayton": -4, "akron": -4, "toledo": -4, "erie": -4,
    "scranton": -4, "allentown": -4, "trenton": -4, "camden": -4,
    "greensboro": -4, "durham": -4, "winston-salem": -4, "asheville": -4,
    "columbia": -4, "greenville": -4, "myrtle beach": -4,
    "augusta": -4, "tallahassee": -4, "gainesville": -4,
    "st. petersburg fl": -4, "hialeah": -4, "pompano beach": -4,
    "west palm beach": -4, "port st. lucie": -4, "cape coral": -4,
    "pensacola": -5, "mobile": -5,
    "huntsville": -5, "chattanooga": -4,
    # US — Central (CDT = UTC-5)
    "chicago": -5, "dallas": -5, "houston": -5, "austin": -5,
    "san antonio": -5, "fort worth": -5, "el paso": -6,
    "nashville": -5, "memphis": -5, "minneapolis": -5, "st. louis": -5,
    "kansas city": -5, "milwaukee": -5, "new orleans": -5,
    "oklahoma city": -5, "omaha": -5, "tulsa": -5, "wichita": -5,
    "little rock": -5, "des moines": -5, "madison": -5,
    "birmingham": -5, "montgomery": -5, "baton rouge": -5,
    "jackson": -5, "corpus christi": -5, "lubbock": -5,
    "fargo": -5, "sioux falls": -5, "lincoln": -5,
    "st. paul": -5, "green bay": -5, "appleton": -5, "racine": -5,
    "cedar rapids": -5, "davenport": -5, "iowa city": -5,
    "springfield il": -5, "peoria": -5, "rockford": -5,
    "topeka": -5, "lawrence": -5, "overland park": -5,
    "amarillo": -5, "midland": -5, "odessa": -5, "abilene": -5,
    "waco": -5, "killeen": -5, "mcallen": -5, "brownsville": -5,
    "laredo": -5, "beaumont": -5, "tyler": -5, "shreveport": -5,
    "lafayette": -5, "lake charles": -5,
    "duluth": -5, "rochester mn": -5, "st. cloud": -5,
    "rapid city": -6, "bismarck": -5, "grand forks": -5,
    # US — Mountain (MDT = UTC-6)
    "denver": -6, "salt lake": -6, "salt lake city": -6,
    "albuquerque": -6, "boise": -6, "colorado springs": -6,
    "tucson": -7, "phoenix": -7,  # Arizona no DST
    "billings": -6, "cheyenne": -6, "missoula": -6,
    "santa fe": -6, "las cruces": -6, "provo": -6, "ogden": -6,
    "fort collins": -6, "boulder": -6, "pueblo": -6, "aurora co": -6,
    "lakewood": -6, "thornton": -6, "arvada": -6,
    "idaho falls": -6, "pocatello": -6, "twin falls": -6,
    "great falls": -6, "helena": -6, "butte": -6,
    "casper": -6, "laramie": -6, "gillette": -6,
    "scottsdale": -7, "mesa": -7, "tempe": -7, "chandler": -7,
    "glendale az": -7, "flagstaff": -7, "yuma": -7,
    # US — Pacific (PDT = UTC-7)
    "los angeles": -7, "san francisco": -7, "seattle": -7, "portland": -7,
    "las vegas": -7, "san diego": -7, "san jose": -7, "sacramento": -7,
    "fresno": -7, "oakland": -7, "bakersfield": -7, "riverside": -7,
    "stockton": -7, "spokane": -7, "tacoma": -7, "reno": -7,
    "long beach": -7, "anaheim": -7, "santa ana": -7, "irvine": -7,
    "glendale ca": -7, "pasadena": -7, "santa barbara": -7,
    "santa cruz": -7, "modesto": -7, "visalia": -7, "oxnard": -7,
    "ventura": -7, "santa rosa": -7, "hayward": -7, "sunnyvale": -7,
    "fremont": -7, "berkeley": -7, "concord": -7, "vallejo": -7,
    "antioch": -7, "richmond ca": -7, "el monte": -7, "downey": -7,
    "inglewood": -7, "costa mesa": -7, "carlsbad": -7, "escondido": -7,
    "temecula": -7, "murrieta": -7, "fontana": -7, "moreno valley": -7,
    "rancho cucamonga": -7, "ontario ca": -7, "pomona": -7,
    "corona": -7, "victorville": -7, "palmdale": -7, "lancaster ca": -7,
    "eugene": -7, "salem": -7, "bend": -7, "medford": -7, "corvallis": -7,
    "olympia": -7, "bellingham": -7, "yakima": -7, "kennewick": -7,
    "henderson": -7, "north las vegas": -7, "sparks": -7, "carson city": -7,
    # US — Alaska/Hawaii
    "anchorage": -8, "fairbanks": -8, "juneau": -8,
    "honolulu": -10, "hilo": -10, "maui": -10, "kailua": -10,
    # Canada — Atlantic (ADT = UTC-3)
    "halifax": -3, "st. john's": -2.5, "fredericton": -3, "moncton": -3,
    "charlottetown": -3, "sydney ns": -3, "dartmouth": -3,
    # Canada — Eastern (EDT = UTC-4)
    "toronto": -4, "montreal": -4, "ottawa": -4, "quebec city": -4,
    "hamilton": -4, "kitchener": -4, "london ontario": -4,
    "windsor": -4, "mississauga": -4, "brampton": -4, "markham": -4,
    "vaughan": -4, "oakville": -4, "burlington": -4, "oshawa": -4,
    "barrie": -4, "guelph": -4, "cambridge": -4, "waterloo": -4,
    "kingston on": -4, "sudbury": -4, "sault ste. marie": -4,
    "laval": -4, "gatineau": -4, "sherbrooke": -4, "longueuil": -4,
    "trois-rivieres": -4, "chicoutimi": -4,
    # Canada — Central (CDT = UTC-5)
    "winnipeg": -5, "regina": -6, "saskatoon": -6, "thunder bay": -4,
    "brandon": -5, "moose jaw": -6, "prince albert": -6,
    # Canada — Mountain/Pacific
    "calgary": -6, "edmonton": -6, "vancouver": -7, "victoria": -7,
    "kelowna": -7, "surrey": -7, "burnaby": -7, "richmond bc": -7,
    "abbotsford": -7, "nanaimo": -7, "kamloops": -7, "prince george": -7,
    "red deer": -6, "lethbridge": -6, "medicine hat": -6,
    "whitehorse": -7, "yellowknife": -6, "iqaluit": -4,
    # Mexico
    "mexico city": -5, "guadalajara": -5, "monterrey": -5,
    "cancun": -5, "tijuana": -7, "puebla": -5, "juarez": -6,
    "leon": -5, "zapopan": -5, "merida": -5, "chihuahua": -6,
    "acapulco": -5, "hermosillo": -7, "culiacan": -6,
    "morelia": -5, "aguascalientes": -5, "queretaro": -5,
    "san luis potosi": -5, "toluca": -5, "villahermosa": -5,
    "veracruz": -5, "oaxaca": -5, "durango": -5, "mazatlan": -6,
    "tampico": -5, "saltillo": -5, "reynosa": -5, "matamoros": -5,
    # Central America & Caribbean
    "havana": -4, "san juan": -4, "santo domingo": -4,
    "guatemala city": -6, "san salvador": -6, "tegucigalpa": -6,
    "managua": -6, "san jose cr": -6, "panama city": -5,
    "kingston jm": -5, "port-au-prince": -4,
    "nassau": -4, "bridgetown": -4, "port of spain": -4,
    "belmopan": -6, "belize city": -6,
    # South America
    "são paulo": -3, "sao paulo": -3, "rio": -3, "rio de janeiro": -3,
    "brasilia": -3, "buenos aires": -3, "bogota": -5, "lima": -5,
    "santiago": -3, "caracas": -4, "quito": -5, "montevideo": -3,
    "asuncion": -4, "la paz": -4, "medellin": -5, "cali": -5,
    "recife": -3, "belo horizonte": -3, "curitiba": -3,
    "porto alegre": -3, "salvador": -3, "fortaleza": -3, "manaus": -4,
    "belem": -3, "goiania": -3, "campinas": -3, "florianopolis": -3,
    "vitoria": -3, "santos": -3, "natal": -3, "maceio": -3,
    "barranquilla": -5, "cartagena": -5, "bucaramanga": -5,
    "santa cruz bo": -4, "cochabamba": -4, "sucre": -4,
    "guayaquil": -5, "cuenca": -5,
    "arequipa": -5, "trujillo": -5, "cusco": -5,
    "cordoba ar": -3, "rosario": -3, "mendoza": -3, "tucuman": -3,
    "mar del plata": -3, "salta": -3, "san juan ar": -3,
    "valparaiso": -3, "concepcion": -3, "temuco": -3,
    "maracaibo": -4, "valencia ve": -4, "barquisimeto": -4,
    "georgetown": -4, "paramaribo": -3, "cayenne": -3,
    # UK & Ireland
    "london": 1, "manchester": 1, "birmingham uk": 1, "glasgow": 1,
    "edinburgh": 1, "liverpool": 1, "bristol": 1, "leeds": 1,
    "dublin": 1, "belfast": 1, "cardiff": 1,
    "sheffield": 1, "nottingham": 1, "leicester": 1, "newcastle": 1,
    "brighton": 1, "southampton": 1, "portsmouth": 1, "plymouth": 1,
    "oxford": 1, "cambridge uk": 1, "bath": 1, "york": 1,
    "aberdeen": 1, "dundee": 1, "inverness": 1, "swansea": 1,
    "coventry": 1, "stoke": 1, "wolverhampton": 1, "derby": 1,
    "reading": 1, "luton": 1, "sunderland": 1, "middlesbrough": 1,
    "cork": 1, "galway": 1, "limerick": 1, "waterford": 1,
    # Western Europe (CEST = UTC+2)
    "paris": 2, "berlin": 2, "madrid": 2, "rome": 2, "milan": 2,
    "amsterdam": 2, "brussels": 2, "vienna": 2, "zurich": 2,
    "munich": 2, "hamburg": 2, "barcelona": 2, "lisbon": 1,
    "prague": 2, "warsaw": 2, "budapest": 2, "copenhagen": 2,
    "stockholm": 2, "oslo": 2, "helsinki": 3, "athens": 3,
    "bucharest": 3, "sofia": 3, "belgrade": 2, "zagreb": 2,
    "lyon": 2, "marseille": 2, "naples": 2, "turin": 2,
    "frankfurt": 2, "cologne": 2, "dusseldorf": 2, "geneva": 2,
    "nice": 2, "toulouse": 2, "bordeaux": 2, "strasbourg": 2,
    "nantes": 2, "montpellier": 2, "lille": 2, "rennes": 2,
    "reims": 2, "grenoble": 2, "rouen": 2, "toulon": 2,
    "seville": 2, "valencia": 2, "malaga": 2, "bilbao": 2,
    "zaragoza": 2, "murcia": 2, "palma": 2, "granada": 2,
    "alicante": 2, "cordoba es": 2, "vigo": 2, "las palmas": 1,
    "tenerife": 1, "porto": 1, "braga": 1, "coimbra": 1,
    "florence": 2, "bologna": 2, "genoa": 2, "palermo": 2,
    "catania": 2, "bari": 2, "verona": 2, "venice": 2, "padua": 2,
    "trieste": 2, "brescia": 2, "parma": 2, "modena": 2,
    "stuttgart": 2, "leipzig": 2, "dresden": 2, "hanover": 2,
    "nuremberg": 2, "bremen": 2, "essen": 2, "dortmund": 2,
    "bonn": 2, "mannheim": 2, "karlsruhe": 2, "augsburg": 2,
    "wiesbaden": 2, "munster": 2, "kiel": 2, "rostock": 2,
    "rotterdam": 2, "the hague": 2, "utrecht": 2, "eindhoven": 2,
    "antwerp": 2, "ghent": 2, "bruges": 2, "liege": 2,
    "basel": 2, "bern": 2, "lausanne": 2, "lucerne": 2,
    "graz": 2, "linz": 2, "salzburg": 2, "innsbruck": 2,
    "krakow": 2, "wroclaw": 2, "gdansk": 2, "poznan": 2, "lodz": 2,
    "brno": 2, "ostrava": 2, "pilsen": 2,
    "debrecen": 2, "szeged": 2, "miskolc": 2,
    "gothenburg": 2, "malmo": 2, "uppsala": 2,
    "bergen": 2, "trondheim": 2, "stavanger": 2,
    "aarhus": 2, "odense": 2, "aalborg": 2,
    "tampere": 3, "turku": 3, "oulu": 3, "espoo": 3,
    "thessaloniki": 3, "patras": 3, "heraklion": 3,
    "cluj-napoca": 3, "timisoara": 3, "iasi": 3, "constanta": 3,
    "plovdiv": 3, "varna": 3,
    "ljubljana": 2, "maribor": 2,
    "bratislava": 2, "kosice": 2,
    "sarajevo": 2, "banja luka": 2,
    "skopje": 2, "pristina": 2, "tirana": 2, "podgorica": 2,
    "vilnius": 3, "kaunas": 3, "riga": 3, "tallinn": 3,
    "reykjavik": 0,
    "luxembourg": 2, "monaco": 2, "andorra": 2, "valletta": 2,
    "nicosia": 3, "limassol": 3,
    # Eastern Europe & Turkey
    "istanbul": 3, "moscow": 3, "st. petersburg": 3, "kyiv": 3,
    "minsk": 3, "tbilisi": 4, "yerevan": 4, "baku": 4,
    "ankara": 3, "izmir": 3, "antalya": 3, "bursa": 3, "adana": 3,
    "gaziantep": 3, "konya": 3, "trabzon": 3, "kayseri": 3,
    "novosibirsk": 7, "yekaterinburg": 5, "kazan": 3,
    "nizhny novgorod": 3, "chelyabinsk": 5, "samara": 4,
    "rostov-on-don": 3, "ufa": 5, "volgograd": 3, "perm": 5,
    "krasnoyarsk": 7, "voronezh": 3, "saratov": 4,
    "krasnodar": 3, "sochi": 3, "vladivostok": 10,
    "irkutsk": 8, "khabarovsk": 10, "omsk": 6, "tomsk": 7,
    "tyumen": 5, "barnaul": 7, "kemerovo": 7,
    "kharkiv": 3, "odesa": 3, "dnipro": 3, "lviv": 3, "zaporizhzhia": 3,
    "chisinau": 3,
    # Middle East
    "dubai": 4, "abu dhabi": 4, "doha": 3, "riyadh": 3, "jeddah": 3,
    "kuwait city": 3, "muscat": 4, "amman": 3, "beirut": 3,
    "tel aviv": 3, "jerusalem": 3, "baghdad": 3, "tehran": 3.5,
    "sharjah": 4, "ajman": 4, "al ain": 4, "fujairah": 4,
    "mecca": 3, "medina": 3, "dammam": 3, "khobar": 3, "tabuk": 3,
    "manama": 3, "sana'a": 3, "aden": 3,
    "haifa": 3, "erbil": 3, "basra": 3, "mosul": 3, "sulaymaniyah": 3,
    "isfahan": 3.5, "mashhad": 3.5, "tabriz": 3.5, "shiraz": 3.5,
    "ahvaz": 3.5, "kerman": 3.5, "rasht": 3.5,
    "aleppo": 3, "damascus": 3, "tripoli lb": 3,
    # Central Asia
    "almaty": 6, "astana": 6, "nur-sultan": 6, "shymkent": 6,
    "tashkent": 5, "samarkand": 5, "bukhara": 5,
    "bishkek": 6, "dushanbe": 5, "ashgabat": 5,
    # South Asia
    "mumbai": 5.5, "delhi": 5.5, "new delhi": 5.5, "bangalore": 5.5,
    "bengaluru": 5.5, "chennai": 5.5, "kolkata": 5.5, "hyderabad": 5.5,
    "pune": 5.5, "ahmedabad": 5.5, "jaipur": 5.5,
    "lucknow": 5.5, "kanpur": 5.5, "nagpur": 5.5, "indore": 5.5,
    "bhopal": 5.5, "patna": 5.5, "vadodara": 5.5, "ludhiana": 5.5,
    "agra": 5.5, "nashik": 5.5, "varanasi": 5.5, "srinagar": 5.5,
    "amritsar": 5.5, "chandigarh": 5.5, "jodhpur": 5.5, "udaipur": 5.5,
    "guwahati": 5.5, "thiruvananthapuram": 5.5, "kochi": 5.5,
    "coimbatore": 5.5, "visakhapatnam": 5.5, "madurai": 5.5,
    "mangalore": 5.5, "mysore": 5.5, "ranchi": 5.5, "raipur": 5.5,
    "dehradun": 5.5, "shimla": 5.5, "goa": 5.5, "surat": 5.5,
    "rajkot": 5.5, "vijayawada": 5.5, "tiruchirappalli": 5.5,
    "karachi": 5, "lahore": 5, "islamabad": 5,
    "faisalabad": 5, "rawalpindi": 5, "multan": 5, "peshawar": 5,
    "quetta": 5, "hyderabad pk": 5,
    "dhaka": 6, "chittagong": 6, "khulna": 6, "rajshahi": 6,
    "colombo": 5.5, "kandy": 5.5,
    "kathmandu": 5.75, "pokhara": 5.75,
    "thimphu": 6, "male": 5,
    # Southeast Asia
    "bangkok": 7, "jakarta": 7, "singapore": 8, "kuala lumpur": 8,
    "manila": 8, "ho chi minh": 7, "hanoi": 7, "phnom penh": 7,
    "yangon": 6.5, "naypyidaw": 6.5,
    "chiang mai": 7, "phuket": 7, "pattaya": 7,
    "surabaya": 7, "bandung": 7, "medan": 7, "semarang": 7,
    "makassar": 8, "bali": 8, "denpasar": 8, "yogyakarta": 7,
    "johor bahru": 8, "penang": 8, "ipoh": 8, "kota kinabalu": 8,
    "kuching": 8, "cebu": 8, "davao": 8, "quezon city": 8,
    "vientiane": 7, "luang prabang": 7,
    "siem reap": 7, "battambang": 7,
    "bandar seri begawan": 8, "dili": 9,
    # East Asia
    "tokyo": 9, "osaka": 9, "seoul": 9, "busan": 9,
    "beijing": 8, "shanghai": 8, "guangzhou": 8, "shenzhen": 8,
    "chengdu": 8, "wuhan": 8, "nanjing": 8, "hangzhou": 8,
    "hong kong": 8, "taipei": 8,
    "yokohama": 9, "nagoya": 9, "sapporo": 9, "kobe": 9, "kyoto": 9,
    "fukuoka": 9, "kawasaki": 9, "hiroshima": 9, "sendai": 9,
    "kitakyushu": 9, "naha": 9, "okinawa": 9,
    "incheon": 9, "daegu": 9, "daejeon": 9, "gwangju": 9, "ulsan": 9,
    "tianjin": 8, "chongqing": 8, "xian": 8, "xi'an": 8,
    "suzhou": 8, "dongguan": 8, "foshan": 8, "zhengzhou": 8,
    "changsha": 8, "kunming": 8, "dalian": 8, "qingdao": 8,
    "xiamen": 8, "fuzhou": 8, "harbin": 8, "jinan": 8,
    "hefei": 8, "changchun": 8, "urumqi": 8, "guiyang": 8,
    "nanning": 8, "lanzhou": 8, "taiyuan": 8, "haikou": 8,
    "shenyang": 8, "wuxi": 8, "ningbo": 8, "wenzhou": 8,
    "macau": 8, "kaohsiung": 8, "taichung": 8, "tainan": 8,
    "ulaanbaatar": 8, "pyongyang": 9,
    # Oceania
    "sydney": 11, "melbourne": 11, "brisbane": 10, "perth": 8,
    "adelaide": 10.5, "auckland": 13, "wellington": 13,
    "canberra": 11, "gold coast": 10, "hobart": 11,
    "darwin": 9.5, "cairns": 10, "townsville": 10, "toowoomba": 10,
    "newcastle nsw": 11, "wollongong": 11, "geelong": 11,
    "ballarat": 11, "bendigo": 11, "launceston": 11,
    "christchurch": 13, "hamilton nz": 13, "tauranga": 13,
    "dunedin": 13, "palmerston north": 13, "napier": 13, "rotorua": 13,
    "suva": 12, "noumea": 11, "port moresby": 10,
    "apia": 13, "nuku'alofa": 13, "port vila": 11,
    # Africa
    "cairo": 2, "johannesburg": 2, "cape town": 2, "lagos": 1,
    "nairobi": 3, "casablanca": 1, "accra": 0, "addis ababa": 3,
    "dar es salaam": 3, "kinshasa": 1, "luanda": 1, "tunis": 1,
    "algiers": 1, "khartoum": 2, "kampala": 3,
    "alexandria": 2, "giza": 2, "luxor": 2, "aswan": 2,
    "durban": 2, "pretoria": 2, "port elizabeth": 2, "bloemfontein": 2,
    "east london": 2, "pietermaritzburg": 2, "polokwane": 2,
    "maputo": 2, "harare": 2, "bulawayo": 2,
    "lusaka": 2, "ndola": 2, "lilongwe": 2, "blantyre": 2,
    "windhoek": 2, "gaborone": 2, "mbabane": 2,
    "abuja": 1, "kano": 1, "ibadan": 1, "port harcourt": 1,
    "benin city": 1, "kaduna": 1, "enugu": 1, "calabar": 1,
    "douala": 1, "yaounde": 1,
    "dakar": 0, "abidjan": 0, "kumasi": 0, "bamako": 0, "conakry": 0,
    "lome": 0, "cotonou": 1, "niamey": 1, "ouagadougou": 0,
    "freetown": 0, "monrovia": 0, "banjul": 0, "nouakchott": 0,
    "antananarivo": 3, "port louis": 4,
    "mombasa": 3, "kisumu": 3, "nakuru": 3,
    "zanzibar": 3, "arusha": 3, "dodoma": 3, "mwanza": 3,
    "kigali": 2, "bujumbura": 2,
    "mogadishu": 3, "djibouti": 3, "asmara": 3,
    "juba": 2, "ndjamena": 1, "bangui": 1, "libreville": 1,
    "brazzaville": 1, "malabo": 1, "sao tome": 0,
    "praia": -1, "mindelo": -1,
}


def is_weather_temp_known(question: str) -> bool:
    """Check if it's late enough in the city's local time for daily high temp.

    Daily high temperatures are typically recorded by early-mid afternoon.
    Extracts the market date from the question and compares to local date/time.
    Returns True only if the market date is today (local) AND local time >= 3 PM,
    OR the market date is in the past (local).
    Returns False if city can't be identified (block unknown — safer to skip).
    Returns True if date can't be parsed (don't block non-date weather markets).
    """
    q_lower = question.lower()
    matched_city = None
    for city in _CITY_UTC_OFFSETS:
        if city in q_lower:
            if matched_city is None or len(city) > len(matched_city):
                matched_city = city

    if matched_city is None:
        return False  # Unknown city, block to be safe

    offset = _CITY_UTC_OFFSETS[matched_city]
    utc_now = datetime.now(timezone.utc)
    local_now = utc_now + timedelta(hours=offset)
    local_date = local_now.date()
    local_hour = local_now.hour

    # Extract date from question: "on March 17" or "on March 17?"
    date_match = re.search(
        r'\bon\s+(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+(\d{1,2})',
        q_lower)
    if not date_match:
        return True  # Can't parse date, don't block

    month_name = date_match.group(1)
    day = int(date_match.group(2))
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = month_map[month_name]

    try:
        from datetime import date as date_cls
        market_date = date_cls(local_date.year, month, day)
    except ValueError:
        return True  # Invalid date, don't block

    if market_date < local_date:
        return True  # Past date, temp is known
    if market_date > local_date:
        return False  # Future date, temp not known yet
    # Today: check if past 3 PM local
    return local_hour >= 15


def was_below_threshold_pre_close(
    history: list[dict],
    end_date_str: str,
    window_hours: int,
    cutoff: float,
) -> bool:
    """Return True if price dropped below cutoff in the window before end_date.

    This catches "surprise wins" — markets where there was genuine uncertainty
    near the close. 33% of markets in our analysis had prices below $0.50 near
    close, indicating these aren't safe settlement-lag plays.

    Args:
        history: Price history from CLOB API [{"t": unix_ts, "p": price}].
        end_date_str: ISO format end date string.
        window_hours: How far back to look (default 2 hours).
        cutoff: Price threshold (default $0.50).
    """
    if not history or not end_date_str:
        return False
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False

    window_start_ts = (end_dt - timedelta(hours=window_hours)).timestamp()
    end_ts = end_dt.timestamp()

    for point in history:
        t = point.get("t", 0)
        p = point.get("p", 1.0)
        if window_start_ts <= t <= end_ts:
            try:
                if float(p) < cutoff:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def check_liquidity_at_threshold(
    asks: list[dict],
    threshold: float,
    min_usdc: float,
) -> tuple[float, bool]:
    """Check USDC value of asks available at or below the threshold price.

    Returns (available_usdc, passes_minimum).
    Uses price * size for USDC cost, not raw share count.
    """
    total_usdc = 0.0
    for ask in asks:
        try:
            price = float(ask.get("price", 1.0))
            size = float(ask.get("size", 0.0))
            if price <= threshold:
                total_usdc += price * size
        except (ValueError, TypeError):
            continue
    return total_usdc, total_usdc >= min_usdc


# Category settlement speed scores (from analysis data)
_CATEGORY_SCORE = {
    "Crypto": 1.0,    # 3.02h median settlement — best for capital velocity
    "Esports": 0.8,   # 1.97h median settlement — fastest but lower volume
}


def score_opportunity(
    category: str,
    liquidity_usdc: float,
    minutes_since_end: float,
) -> float:
    """Score opportunity 0.0–10.0. Higher = more attractive.

    Components:
      - Category base (0–4): Crypto=4.0, Esports=3.2 (based on settlement speed)
      - Liquidity score (0–4): log2(usdc/10), capped at 4.0
      - Time score (0–2): optimal window is 30min–24h after end_date
    """
    cat_score = _CATEGORY_SCORE.get(category, 0.5) * 4.0

    # Liquidity: log2(usdc/10), capped at 4
    liq_score = min(4.0, math.log2(max(1.0, liquidity_usdc / 10.0)))

    # Time: reward the sweet spot, penalize very fresh or very stale
    hours = minutes_since_end / 60.0
    if hours < 0.5:
        time_score = 0.0
    elif hours <= 24.0:
        time_score = 2.0
    elif hours <= 48.0:
        time_score = 1.0
    else:
        time_score = 0.5

    return round(cat_score + liq_score + time_score, 2)
