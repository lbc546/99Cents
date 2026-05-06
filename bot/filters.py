"""Market filtering logic for the arbitrage bot.

Reuses category inference and crypto detection from the analysis scripts.
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


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


def _extract_tags(tags) -> set:
    """Extract lowercase label set from Gamma tags list."""
    if not tags or not isinstance(tags, list):
        return set()
    return {t.get("label", "").lower() for t in tags if isinstance(t, dict)}


def get_market_tags(market: dict):
    """Extract Gamma tags from a market dict.

    Tags live at the event level, not the market level. They may be
    present as ``market["_event_tags"]`` (attached by the events poller)
    or nested inside ``market["events"][0]["tags"]`` (some Gamma
    endpoints embed the parent event).  Returns the tags list or None.
    """
    tags = market.get("_event_tags")
    if tags:
        return tags
    evts = market.get("events")
    if isinstance(evts, list) and evts:
        tags = evts[0].get("tags") if isinstance(evts[0], dict) else None
    return tags


def infer_category(question: str, tags=None) -> str:
    """Infer market category from Gamma tags (if available) or question text.

    Gamma events carry a ``tags`` list of dicts with a ``label`` key
    (e.g. "Sports", "Golf", "PGA").  When present these are checked
    first; keyword matching on the question is the fallback.
    """
    # --- Gamma tags (authoritative when available) ---
    tag_labels = _extract_tags(tags)
    if tag_labels:
        if tag_labels & {"sports", "golf", "pga", "pga tour", "tennis",
                         "nba", "nfl", "nhl", "mlb", "ufc", "mma",
                         "boxing", "cricket", "f1", "formula 1",
                         "nascar", "rugby", "cycling", "soccer",
                         "football", "baseball", "basketball", "hockey"}:
            return "Sports"
        if tag_labels & {"esports", "dota", "counter-strike", "cs2",
                         "league of legends", "valorant"}:
            return "Esports"
        if tag_labels & {"entertainment", "tv", "movies", "music",
                         "reality tv", "celebrity"}:
            return "Entertainment"
        if tag_labels & {"crypto", "bitcoin", "ethereum"}:
            return "Crypto"
        if tag_labels & {"politics", "elections", "government"}:
            return "Politics"
        if tag_labels & {"economics", "finance", "markets"}:
            return "Economics/Finance"
        if tag_labels & {"weather", "science", "climate", "space"}:
            return "Science/Weather"

    # --- Keyword fallback ---
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
        "love is blind", "bachelor", "bachelorette", "survivor",
        "big brother", "reality tv", "engaged on",
        "eliminated", "rose ceremony", "tribal council",
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
    category = infer_category(question, tags=get_market_tags(market))
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
        # Sports miscategorized as "Other" — tournament endDate ≠ match end
        r"\bwin the\b.+\b(open|championship|cup|tour|tournament|classic|invitational|memorial|masters)\b",
        r"\bpga\b",                      # PGA Tour golf
        r"\bufc\b",                      # UFC fights
        r"\bko\b.*\btko\b",             # knockout/TKO (MMA/boxing)
        r"\bsubmission\b",              # MMA
        r"\bfight\b.+\bdistance\b",     # MMA "go the distance"
        r"\bround \d+\b",               # boxing/MMA rounds
        r"\bpenalt(y|ies)\b",           # soccer penalties
        r"\bovertime\b",                # sports overtime
        r"\bhalf-time\b",               # sports half-time
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
        r"\bhormuz\b",                   # Strait of Hormuz transit counts — unpredictable
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


# City → IANA timezone. Uses zoneinfo for automatic DST handling.
_CITY_TIMEZONES: dict[str, str] = {
    # US — Eastern
    "new york": "America/New_York", "nyc": "America/New_York",
    "new york city": "America/New_York",
    "boston": "America/New_York", "philadelphia": "America/New_York",
    "philly": "America/New_York",
    "washington": "America/New_York", "washington dc": "America/New_York",
    "dc": "America/New_York",
    "atlanta": "America/New_York", "miami": "America/New_York",
    "orlando": "America/New_York", "tampa": "America/New_York",
    "jacksonville": "America/New_York", "charlotte": "America/New_York",
    "raleigh": "America/New_York", "richmond": "America/New_York",
    "detroit": "America/Detroit", "cleveland": "America/New_York",
    "columbus": "America/New_York", "cincinnati": "America/New_York",
    "pittsburgh": "America/New_York", "indianapolis": "America/Indiana/Indianapolis",
    "baltimore": "America/New_York",
    "buffalo": "America/New_York", "rochester": "America/New_York",
    "hartford": "America/New_York", "providence": "America/New_York",
    "norfolk": "America/New_York", "virginia beach": "America/New_York",
    "wilmington": "America/New_York",
    "charleston": "America/New_York", "savannah": "America/New_York",
    "knoxville": "America/New_York", "lexington": "America/New_York",
    "louisville": "America/Kentucky/Louisville",
    "grand rapids": "America/Detroit",
    "fort lauderdale": "America/New_York",
    "newark": "America/New_York", "jersey city": "America/New_York",
    "stamford": "America/New_York", "new haven": "America/New_York",
    "worcester": "America/New_York", "springfield": "America/New_York",
    "syracuse": "America/New_York", "albany": "America/New_York",
    "dayton": "America/New_York", "akron": "America/New_York",
    "toledo": "America/New_York", "erie": "America/New_York",
    "scranton": "America/New_York", "allentown": "America/New_York",
    "trenton": "America/New_York", "camden": "America/New_York",
    "greensboro": "America/New_York", "durham": "America/New_York",
    "winston-salem": "America/New_York", "asheville": "America/New_York",
    "columbia": "America/New_York", "greenville": "America/New_York",
    "myrtle beach": "America/New_York",
    "augusta": "America/New_York", "tallahassee": "America/New_York",
    "gainesville": "America/New_York",
    "st. petersburg fl": "America/New_York",
    "hialeah": "America/New_York", "pompano beach": "America/New_York",
    "west palm beach": "America/New_York",
    "port st. lucie": "America/New_York",
    "cape coral": "America/New_York",
    "pensacola": "America/Chicago", "mobile": "America/Chicago",
    "huntsville": "America/Chicago", "chattanooga": "America/New_York",
    # US — Central
    "chicago": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago", "austin": "America/Chicago",
    "san antonio": "America/Chicago", "fort worth": "America/Chicago",
    "el paso": "America/Denver",
    "nashville": "America/Chicago", "memphis": "America/Chicago",
    "minneapolis": "America/Chicago", "st. louis": "America/Chicago",
    "kansas city": "America/Chicago", "milwaukee": "America/Chicago",
    "new orleans": "America/Chicago",
    "oklahoma city": "America/Chicago", "omaha": "America/Chicago",
    "tulsa": "America/Chicago", "wichita": "America/Chicago",
    "little rock": "America/Chicago", "des moines": "America/Chicago",
    "madison": "America/Chicago",
    "birmingham": "America/Chicago", "montgomery": "America/Chicago",
    "baton rouge": "America/Chicago",
    "jackson": "America/Chicago", "corpus christi": "America/Chicago",
    "lubbock": "America/Chicago",
    "fargo": "America/Chicago", "sioux falls": "America/Chicago",
    "lincoln": "America/Chicago",
    "st. paul": "America/Chicago", "green bay": "America/Chicago",
    "appleton": "America/Chicago", "racine": "America/Chicago",
    "cedar rapids": "America/Chicago", "davenport": "America/Chicago",
    "iowa city": "America/Chicago",
    "springfield il": "America/Chicago", "peoria": "America/Chicago",
    "rockford": "America/Chicago",
    "topeka": "America/Chicago", "lawrence": "America/Chicago",
    "overland park": "America/Chicago",
    "amarillo": "America/Chicago", "midland": "America/Chicago",
    "odessa": "America/Chicago", "abilene": "America/Chicago",
    "waco": "America/Chicago", "killeen": "America/Chicago",
    "mcallen": "America/Chicago", "brownsville": "America/Chicago",
    "laredo": "America/Chicago", "beaumont": "America/Chicago",
    "tyler": "America/Chicago", "shreveport": "America/Chicago",
    "lafayette": "America/Chicago", "lake charles": "America/Chicago",
    "duluth": "America/Chicago", "rochester mn": "America/Chicago",
    "st. cloud": "America/Chicago",
    "rapid city": "America/Denver", "bismarck": "America/Chicago",
    "grand forks": "America/Chicago",
    # US — Mountain
    "denver": "America/Denver", "salt lake": "America/Denver",
    "salt lake city": "America/Denver",
    "albuquerque": "America/Denver", "boise": "America/Boise",
    "colorado springs": "America/Denver",
    "tucson": "America/Phoenix", "phoenix": "America/Phoenix",
    "billings": "America/Denver", "cheyenne": "America/Denver",
    "missoula": "America/Denver",
    "santa fe": "America/Denver", "las cruces": "America/Denver",
    "provo": "America/Denver", "ogden": "America/Denver",
    "fort collins": "America/Denver", "boulder": "America/Denver",
    "pueblo": "America/Denver", "aurora co": "America/Denver",
    "lakewood": "America/Denver", "thornton": "America/Denver",
    "arvada": "America/Denver",
    "idaho falls": "America/Boise", "pocatello": "America/Boise",
    "twin falls": "America/Boise",
    "great falls": "America/Denver", "helena": "America/Denver",
    "butte": "America/Denver",
    "casper": "America/Denver", "laramie": "America/Denver",
    "gillette": "America/Denver",
    "scottsdale": "America/Phoenix", "mesa": "America/Phoenix",
    "tempe": "America/Phoenix", "chandler": "America/Phoenix",
    "glendale az": "America/Phoenix", "flagstaff": "America/Phoenix",
    "yuma": "America/Phoenix",
    # US — Pacific
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "san jose": "America/Los_Angeles",
    "sacramento": "America/Los_Angeles",
    "fresno": "America/Los_Angeles", "oakland": "America/Los_Angeles",
    "bakersfield": "America/Los_Angeles",
    "riverside": "America/Los_Angeles",
    "stockton": "America/Los_Angeles",
    "spokane": "America/Los_Angeles", "tacoma": "America/Los_Angeles",
    "reno": "America/Los_Angeles",
    "long beach": "America/Los_Angeles",
    "anaheim": "America/Los_Angeles",
    "santa ana": "America/Los_Angeles",
    "irvine": "America/Los_Angeles",
    "glendale ca": "America/Los_Angeles",
    "pasadena": "America/Los_Angeles",
    "santa barbara": "America/Los_Angeles",
    "santa cruz": "America/Los_Angeles",
    "modesto": "America/Los_Angeles",
    "visalia": "America/Los_Angeles", "oxnard": "America/Los_Angeles",
    "ventura": "America/Los_Angeles",
    "santa rosa": "America/Los_Angeles",
    "hayward": "America/Los_Angeles",
    "sunnyvale": "America/Los_Angeles",
    "fremont": "America/Los_Angeles",
    "berkeley": "America/Los_Angeles",
    "concord": "America/Los_Angeles",
    "vallejo": "America/Los_Angeles",
    "antioch": "America/Los_Angeles",
    "richmond ca": "America/Los_Angeles",
    "el monte": "America/Los_Angeles",
    "downey": "America/Los_Angeles",
    "inglewood": "America/Los_Angeles",
    "costa mesa": "America/Los_Angeles",
    "carlsbad": "America/Los_Angeles",
    "escondido": "America/Los_Angeles",
    "temecula": "America/Los_Angeles",
    "murrieta": "America/Los_Angeles",
    "fontana": "America/Los_Angeles",
    "moreno valley": "America/Los_Angeles",
    "rancho cucamonga": "America/Los_Angeles",
    "ontario ca": "America/Los_Angeles",
    "pomona": "America/Los_Angeles",
    "corona": "America/Los_Angeles",
    "victorville": "America/Los_Angeles",
    "palmdale": "America/Los_Angeles",
    "lancaster ca": "America/Los_Angeles",
    "eugene": "America/Los_Angeles", "salem": "America/Los_Angeles",
    "bend": "America/Los_Angeles", "medford": "America/Los_Angeles",
    "corvallis": "America/Los_Angeles",
    "olympia": "America/Los_Angeles",
    "bellingham": "America/Los_Angeles",
    "yakima": "America/Los_Angeles",
    "kennewick": "America/Los_Angeles",
    "henderson": "America/Los_Angeles",
    "north las vegas": "America/Los_Angeles",
    "sparks": "America/Los_Angeles",
    "carson city": "America/Los_Angeles",
    # US — Alaska/Hawaii
    "anchorage": "America/Anchorage", "fairbanks": "America/Anchorage",
    "juneau": "America/Juneau",
    "honolulu": "Pacific/Honolulu", "hilo": "Pacific/Honolulu",
    "maui": "Pacific/Honolulu", "kailua": "Pacific/Honolulu",
    # Canada — Atlantic
    "halifax": "America/Halifax", "st. john's": "America/St_Johns",
    "fredericton": "America/Halifax", "moncton": "America/Moncton",
    "charlottetown": "America/Halifax",
    "sydney ns": "America/Halifax", "dartmouth": "America/Halifax",
    # Canada — Eastern
    "toronto": "America/Toronto", "montreal": "America/Toronto",
    "ottawa": "America/Toronto", "quebec city": "America/Toronto",
    "hamilton": "America/Toronto", "kitchener": "America/Toronto",
    "london ontario": "America/Toronto",
    "windsor": "America/Toronto", "mississauga": "America/Toronto",
    "brampton": "America/Toronto", "markham": "America/Toronto",
    "vaughan": "America/Toronto", "oakville": "America/Toronto",
    "burlington": "America/Toronto", "oshawa": "America/Toronto",
    "barrie": "America/Toronto", "guelph": "America/Toronto",
    "cambridge": "America/Toronto", "waterloo": "America/Toronto",
    "kingston on": "America/Toronto", "sudbury": "America/Toronto",
    "sault ste. marie": "America/Toronto",
    "laval": "America/Toronto", "gatineau": "America/Toronto",
    "sherbrooke": "America/Toronto", "longueuil": "America/Toronto",
    "trois-rivieres": "America/Toronto",
    "chicoutimi": "America/Toronto",
    # Canada — Central
    "winnipeg": "America/Winnipeg", "regina": "America/Regina",
    "saskatoon": "America/Regina", "thunder bay": "America/Toronto",
    "brandon": "America/Winnipeg", "moose jaw": "America/Regina",
    "prince albert": "America/Regina",
    # Canada — Mountain/Pacific
    "calgary": "America/Edmonton", "edmonton": "America/Edmonton",
    "vancouver": "America/Vancouver", "victoria": "America/Vancouver",
    "kelowna": "America/Vancouver", "surrey": "America/Vancouver",
    "burnaby": "America/Vancouver",
    "richmond bc": "America/Vancouver",
    "abbotsford": "America/Vancouver",
    "nanaimo": "America/Vancouver",
    "kamloops": "America/Vancouver",
    "prince george": "America/Vancouver",
    "red deer": "America/Edmonton", "lethbridge": "America/Edmonton",
    "medicine hat": "America/Edmonton",
    "whitehorse": "America/Whitehorse",
    "yellowknife": "America/Yellowknife",
    "iqaluit": "America/Iqaluit",
    # Mexico
    "mexico city": "America/Mexico_City",
    "guadalajara": "America/Mexico_City",
    "monterrey": "America/Monterrey",
    "cancun": "America/Cancun", "tijuana": "America/Tijuana",
    "puebla": "America/Mexico_City", "juarez": "America/Ciudad_Juarez",
    "leon": "America/Mexico_City", "zapopan": "America/Mexico_City",
    "merida": "America/Merida", "chihuahua": "America/Chihuahua",
    "acapulco": "America/Mexico_City",
    "hermosillo": "America/Hermosillo",
    "culiacan": "America/Mazatlan",
    "morelia": "America/Mexico_City",
    "aguascalientes": "America/Mexico_City",
    "queretaro": "America/Mexico_City",
    "san luis potosi": "America/Mexico_City",
    "toluca": "America/Mexico_City",
    "villahermosa": "America/Mexico_City",
    "veracruz": "America/Mexico_City",
    "oaxaca": "America/Mexico_City",
    "durango": "America/Mexico_City",
    "mazatlan": "America/Mazatlan",
    "tampico": "America/Mexico_City",
    "saltillo": "America/Monterrey",
    "reynosa": "America/Matamoros",
    "matamoros": "America/Matamoros",
    # Central America & Caribbean
    "havana": "America/Havana", "san juan": "America/Puerto_Rico",
    "santo domingo": "America/Santo_Domingo",
    "guatemala city": "America/Guatemala",
    "san salvador": "America/El_Salvador",
    "tegucigalpa": "America/Tegucigalpa",
    "managua": "America/Managua",
    "san jose cr": "America/Costa_Rica",
    "panama city": "America/Panama",
    "kingston jm": "America/Jamaica",
    "port-au-prince": "America/Port-au-Prince",
    "nassau": "America/Nassau", "bridgetown": "America/Barbados",
    "port of spain": "America/Port_of_Spain",
    "belmopan": "America/Belize", "belize city": "America/Belize",
    # South America
    "são paulo": "America/Sao_Paulo", "sao paulo": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "brasilia": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "bogota": "America/Bogota", "lima": "America/Lima",
    "santiago": "America/Santiago", "caracas": "America/Caracas",
    "quito": "America/Guayaquil",
    "montevideo": "America/Montevideo",
    "asuncion": "America/Asuncion", "la paz": "America/La_Paz",
    "medellin": "America/Bogota", "cali": "America/Bogota",
    "recife": "America/Recife",
    "belo horizonte": "America/Sao_Paulo",
    "curitiba": "America/Sao_Paulo",
    "porto alegre": "America/Sao_Paulo",
    "salvador": "America/Bahia", "fortaleza": "America/Fortaleza",
    "manaus": "America/Manaus",
    "belem": "America/Belem", "goiania": "America/Sao_Paulo",
    "campinas": "America/Sao_Paulo",
    "florianopolis": "America/Sao_Paulo",
    "vitoria": "America/Sao_Paulo", "santos": "America/Sao_Paulo",
    "natal": "America/Fortaleza", "maceio": "America/Maceio",
    "barranquilla": "America/Bogota",
    "cartagena": "America/Bogota",
    "bucaramanga": "America/Bogota",
    "santa cruz bo": "America/La_Paz",
    "cochabamba": "America/La_Paz", "sucre": "America/La_Paz",
    "guayaquil": "America/Guayaquil",
    "cuenca": "America/Guayaquil",
    "arequipa": "America/Lima", "trujillo": "America/Lima",
    "cusco": "America/Lima",
    "cordoba ar": "America/Argentina/Cordoba",
    "rosario": "America/Argentina/Cordoba",
    "mendoza": "America/Argentina/Mendoza",
    "tucuman": "America/Argentina/Tucuman",
    "mar del plata": "America/Argentina/Buenos_Aires",
    "salta": "America/Argentina/Salta",
    "san juan ar": "America/Argentina/San_Juan",
    "valparaiso": "America/Santiago",
    "concepcion": "America/Santiago",
    "temuco": "America/Santiago",
    "maracaibo": "America/Caracas",
    "valencia ve": "America/Caracas",
    "barquisimeto": "America/Caracas",
    "georgetown": "America/Guyana",
    "paramaribo": "America/Paramaribo",
    "cayenne": "America/Cayenne",
    # UK & Ireland
    "london": "Europe/London", "manchester": "Europe/London",
    "birmingham uk": "Europe/London", "glasgow": "Europe/London",
    "edinburgh": "Europe/London", "liverpool": "Europe/London",
    "bristol": "Europe/London", "leeds": "Europe/London",
    "dublin": "Europe/Dublin", "belfast": "Europe/London",
    "cardiff": "Europe/London",
    "sheffield": "Europe/London", "nottingham": "Europe/London",
    "leicester": "Europe/London", "newcastle": "Europe/London",
    "brighton": "Europe/London", "southampton": "Europe/London",
    "portsmouth": "Europe/London", "plymouth": "Europe/London",
    "oxford": "Europe/London", "cambridge uk": "Europe/London",
    "bath": "Europe/London", "york": "Europe/London",
    "aberdeen": "Europe/London", "dundee": "Europe/London",
    "inverness": "Europe/London", "swansea": "Europe/London",
    "coventry": "Europe/London", "stoke": "Europe/London",
    "wolverhampton": "Europe/London", "derby": "Europe/London",
    "reading": "Europe/London", "luton": "Europe/London",
    "sunderland": "Europe/London", "middlesbrough": "Europe/London",
    "cork": "Europe/Dublin", "galway": "Europe/Dublin",
    "limerick": "Europe/Dublin", "waterford": "Europe/Dublin",
    # Western Europe
    "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "milan": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "brussels": "Europe/Brussels",
    "vienna": "Europe/Vienna", "zurich": "Europe/Zurich",
    "munich": "Europe/Berlin", "hamburg": "Europe/Berlin",
    "barcelona": "Europe/Madrid", "lisbon": "Europe/Lisbon",
    "prague": "Europe/Prague", "warsaw": "Europe/Warsaw",
    "budapest": "Europe/Budapest", "copenhagen": "Europe/Copenhagen",
    "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo",
    "helsinki": "Europe/Helsinki", "athens": "Europe/Athens",
    "bucharest": "Europe/Bucharest", "sofia": "Europe/Sofia",
    "belgrade": "Europe/Belgrade", "zagreb": "Europe/Zagreb",
    "lyon": "Europe/Paris", "marseille": "Europe/Paris",
    "naples": "Europe/Rome", "turin": "Europe/Rome",
    "frankfurt": "Europe/Berlin", "cologne": "Europe/Berlin",
    "dusseldorf": "Europe/Berlin", "geneva": "Europe/Zurich",
    "nice": "Europe/Paris", "toulouse": "Europe/Paris",
    "bordeaux": "Europe/Paris", "strasbourg": "Europe/Paris",
    "nantes": "Europe/Paris", "montpellier": "Europe/Paris",
    "lille": "Europe/Paris", "rennes": "Europe/Paris",
    "reims": "Europe/Paris", "grenoble": "Europe/Paris",
    "rouen": "Europe/Paris", "toulon": "Europe/Paris",
    "seville": "Europe/Madrid", "valencia": "Europe/Madrid",
    "malaga": "Europe/Madrid", "bilbao": "Europe/Madrid",
    "zaragoza": "Europe/Madrid", "murcia": "Europe/Madrid",
    "palma": "Europe/Madrid", "granada": "Europe/Madrid",
    "alicante": "Europe/Madrid", "cordoba es": "Europe/Madrid",
    "vigo": "Europe/Madrid",
    "las palmas": "Atlantic/Canary", "tenerife": "Atlantic/Canary",
    "porto": "Europe/Lisbon", "braga": "Europe/Lisbon",
    "coimbra": "Europe/Lisbon",
    "florence": "Europe/Rome", "bologna": "Europe/Rome",
    "genoa": "Europe/Rome", "palermo": "Europe/Rome",
    "catania": "Europe/Rome", "bari": "Europe/Rome",
    "verona": "Europe/Rome", "venice": "Europe/Rome",
    "padua": "Europe/Rome",
    "trieste": "Europe/Rome", "brescia": "Europe/Rome",
    "parma": "Europe/Rome", "modena": "Europe/Rome",
    "stuttgart": "Europe/Berlin", "leipzig": "Europe/Berlin",
    "dresden": "Europe/Berlin", "hanover": "Europe/Berlin",
    "nuremberg": "Europe/Berlin", "bremen": "Europe/Berlin",
    "essen": "Europe/Berlin", "dortmund": "Europe/Berlin",
    "bonn": "Europe/Berlin", "mannheim": "Europe/Berlin",
    "karlsruhe": "Europe/Berlin", "augsburg": "Europe/Berlin",
    "wiesbaden": "Europe/Berlin", "munster": "Europe/Berlin",
    "kiel": "Europe/Berlin", "rostock": "Europe/Berlin",
    "rotterdam": "Europe/Amsterdam",
    "the hague": "Europe/Amsterdam",
    "utrecht": "Europe/Amsterdam",
    "eindhoven": "Europe/Amsterdam",
    "antwerp": "Europe/Brussels", "ghent": "Europe/Brussels",
    "bruges": "Europe/Brussels", "liege": "Europe/Brussels",
    "basel": "Europe/Zurich", "bern": "Europe/Zurich",
    "lausanne": "Europe/Zurich", "lucerne": "Europe/Zurich",
    "graz": "Europe/Vienna", "linz": "Europe/Vienna",
    "salzburg": "Europe/Vienna", "innsbruck": "Europe/Vienna",
    "krakow": "Europe/Warsaw", "wroclaw": "Europe/Warsaw",
    "gdansk": "Europe/Warsaw", "poznan": "Europe/Warsaw",
    "lodz": "Europe/Warsaw",
    "brno": "Europe/Prague", "ostrava": "Europe/Prague",
    "pilsen": "Europe/Prague",
    "debrecen": "Europe/Budapest", "szeged": "Europe/Budapest",
    "miskolc": "Europe/Budapest",
    "gothenburg": "Europe/Stockholm",
    "malmo": "Europe/Stockholm", "uppsala": "Europe/Stockholm",
    "bergen": "Europe/Oslo", "trondheim": "Europe/Oslo",
    "stavanger": "Europe/Oslo",
    "aarhus": "Europe/Copenhagen", "odense": "Europe/Copenhagen",
    "aalborg": "Europe/Copenhagen",
    "tampere": "Europe/Helsinki", "turku": "Europe/Helsinki",
    "oulu": "Europe/Helsinki", "espoo": "Europe/Helsinki",
    "thessaloniki": "Europe/Athens", "patras": "Europe/Athens",
    "heraklion": "Europe/Athens",
    "cluj-napoca": "Europe/Bucharest",
    "timisoara": "Europe/Bucharest",
    "iasi": "Europe/Bucharest", "constanta": "Europe/Bucharest",
    "plovdiv": "Europe/Sofia", "varna": "Europe/Sofia",
    "ljubljana": "Europe/Ljubljana", "maribor": "Europe/Ljubljana",
    "bratislava": "Europe/Bratislava",
    "kosice": "Europe/Bratislava",
    "sarajevo": "Europe/Sarajevo",
    "banja luka": "Europe/Sarajevo",
    "skopje": "Europe/Skopje", "pristina": "Europe/Belgrade",
    "tirana": "Europe/Tirane", "podgorica": "Europe/Podgorica",
    "vilnius": "Europe/Vilnius", "kaunas": "Europe/Vilnius",
    "riga": "Europe/Riga", "tallinn": "Europe/Tallinn",
    "reykjavik": "Atlantic/Reykjavik",
    "luxembourg": "Europe/Luxembourg", "monaco": "Europe/Monaco",
    "andorra": "Europe/Andorra", "valletta": "Europe/Malta",
    "nicosia": "Asia/Nicosia", "limassol": "Asia/Nicosia",
    # Eastern Europe & Turkey
    "istanbul": "Europe/Istanbul", "moscow": "Europe/Moscow",
    "st. petersburg": "Europe/Moscow", "kyiv": "Europe/Kyiv",
    "minsk": "Europe/Minsk", "tbilisi": "Asia/Tbilisi",
    "yerevan": "Asia/Yerevan", "baku": "Asia/Baku",
    "ankara": "Europe/Istanbul", "izmir": "Europe/Istanbul",
    "antalya": "Europe/Istanbul", "bursa": "Europe/Istanbul",
    "adana": "Europe/Istanbul",
    "gaziantep": "Europe/Istanbul", "konya": "Europe/Istanbul",
    "trabzon": "Europe/Istanbul", "kayseri": "Europe/Istanbul",
    "novosibirsk": "Asia/Novosibirsk",
    "yekaterinburg": "Asia/Yekaterinburg",
    "kazan": "Europe/Moscow",
    "nizhny novgorod": "Europe/Moscow",
    "chelyabinsk": "Asia/Yekaterinburg",
    "samara": "Europe/Samara",
    "rostov-on-don": "Europe/Moscow", "ufa": "Asia/Yekaterinburg",
    "volgograd": "Europe/Volgograd", "perm": "Asia/Yekaterinburg",
    "krasnoyarsk": "Asia/Krasnoyarsk",
    "voronezh": "Europe/Moscow", "saratov": "Europe/Saratov",
    "krasnodar": "Europe/Moscow", "sochi": "Europe/Moscow",
    "vladivostok": "Asia/Vladivostok",
    "irkutsk": "Asia/Irkutsk", "khabarovsk": "Asia/Vladivostok",
    "omsk": "Asia/Omsk", "tomsk": "Asia/Tomsk",
    "tyumen": "Asia/Yekaterinburg",
    "barnaul": "Asia/Barnaul", "kemerovo": "Asia/Novokuznetsk",
    "kharkiv": "Europe/Kyiv", "odesa": "Europe/Kyiv",
    "dnipro": "Europe/Kyiv", "lviv": "Europe/Kyiv",
    "zaporizhzhia": "Europe/Kyiv",
    "chisinau": "Europe/Chisinau",
    # Middle East
    "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai",
    "doha": "Asia/Qatar", "riyadh": "Asia/Riyadh",
    "jeddah": "Asia/Riyadh",
    "kuwait city": "Asia/Kuwait", "muscat": "Asia/Muscat",
    "amman": "Asia/Amman", "beirut": "Asia/Beirut",
    "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem",
    "baghdad": "Asia/Baghdad", "tehran": "Asia/Tehran",
    "sharjah": "Asia/Dubai", "ajman": "Asia/Dubai",
    "al ain": "Asia/Dubai", "fujairah": "Asia/Dubai",
    "mecca": "Asia/Riyadh", "medina": "Asia/Riyadh",
    "dammam": "Asia/Riyadh", "khobar": "Asia/Riyadh",
    "tabuk": "Asia/Riyadh",
    "manama": "Asia/Bahrain", "sana'a": "Asia/Aden",
    "aden": "Asia/Aden",
    "haifa": "Asia/Jerusalem", "erbil": "Asia/Baghdad",
    "basra": "Asia/Baghdad", "mosul": "Asia/Baghdad",
    "sulaymaniyah": "Asia/Baghdad",
    "isfahan": "Asia/Tehran", "mashhad": "Asia/Tehran",
    "tabriz": "Asia/Tehran", "shiraz": "Asia/Tehran",
    "ahvaz": "Asia/Tehran", "kerman": "Asia/Tehran",
    "rasht": "Asia/Tehran",
    "aleppo": "Asia/Damascus", "damascus": "Asia/Damascus",
    "tripoli lb": "Asia/Beirut",
    # Central Asia
    "almaty": "Asia/Almaty", "astana": "Asia/Almaty",
    "nur-sultan": "Asia/Almaty", "shymkent": "Asia/Almaty",
    "tashkent": "Asia/Tashkent", "samarkand": "Asia/Samarkand",
    "bukhara": "Asia/Samarkand",
    "bishkek": "Asia/Bishkek", "dushanbe": "Asia/Dushanbe",
    "ashgabat": "Asia/Ashgabat",
    # South Asia
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata", "ahmedabad": "Asia/Kolkata",
    "jaipur": "Asia/Kolkata",
    "lucknow": "Asia/Kolkata", "kanpur": "Asia/Kolkata",
    "nagpur": "Asia/Kolkata", "indore": "Asia/Kolkata",
    "bhopal": "Asia/Kolkata", "patna": "Asia/Kolkata",
    "vadodara": "Asia/Kolkata", "ludhiana": "Asia/Kolkata",
    "agra": "Asia/Kolkata", "nashik": "Asia/Kolkata",
    "varanasi": "Asia/Kolkata", "srinagar": "Asia/Kolkata",
    "amritsar": "Asia/Kolkata", "chandigarh": "Asia/Kolkata",
    "jodhpur": "Asia/Kolkata", "udaipur": "Asia/Kolkata",
    "guwahati": "Asia/Kolkata",
    "thiruvananthapuram": "Asia/Kolkata",
    "kochi": "Asia/Kolkata",
    "coimbatore": "Asia/Kolkata",
    "visakhapatnam": "Asia/Kolkata",
    "madurai": "Asia/Kolkata",
    "mangalore": "Asia/Kolkata", "mysore": "Asia/Kolkata",
    "ranchi": "Asia/Kolkata", "raipur": "Asia/Kolkata",
    "dehradun": "Asia/Kolkata", "shimla": "Asia/Kolkata",
    "goa": "Asia/Kolkata", "surat": "Asia/Kolkata",
    "rajkot": "Asia/Kolkata", "vijayawada": "Asia/Kolkata",
    "tiruchirappalli": "Asia/Kolkata",
    "karachi": "Asia/Karachi", "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi",
    "faisalabad": "Asia/Karachi", "rawalpindi": "Asia/Karachi",
    "multan": "Asia/Karachi", "peshawar": "Asia/Karachi",
    "quetta": "Asia/Karachi", "hyderabad pk": "Asia/Karachi",
    "dhaka": "Asia/Dhaka", "chittagong": "Asia/Dhaka",
    "khulna": "Asia/Dhaka", "rajshahi": "Asia/Dhaka",
    "colombo": "Asia/Colombo", "kandy": "Asia/Colombo",
    "kathmandu": "Asia/Kathmandu", "pokhara": "Asia/Kathmandu",
    "thimphu": "Asia/Thimphu", "male": "Indian/Maldives",
    # Southeast Asia
    "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "singapore": "Asia/Singapore",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "manila": "Asia/Manila", "ho chi minh": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Ho_Chi_Minh", "phnom penh": "Asia/Phnom_Penh",
    "yangon": "Asia/Yangon", "naypyidaw": "Asia/Yangon",
    "chiang mai": "Asia/Bangkok", "phuket": "Asia/Bangkok",
    "pattaya": "Asia/Bangkok",
    "surabaya": "Asia/Jakarta", "bandung": "Asia/Jakarta",
    "medan": "Asia/Jakarta", "semarang": "Asia/Jakarta",
    "makassar": "Asia/Makassar", "bali": "Asia/Makassar",
    "denpasar": "Asia/Makassar", "yogyakarta": "Asia/Jakarta",
    "johor bahru": "Asia/Kuala_Lumpur",
    "penang": "Asia/Kuala_Lumpur", "ipoh": "Asia/Kuala_Lumpur",
    "kota kinabalu": "Asia/Kuching",
    "kuching": "Asia/Kuching", "cebu": "Asia/Manila",
    "davao": "Asia/Manila", "quezon city": "Asia/Manila",
    "vientiane": "Asia/Vientiane",
    "luang prabang": "Asia/Vientiane",
    "siem reap": "Asia/Phnom_Penh",
    "battambang": "Asia/Phnom_Penh",
    "bandar seri begawan": "Asia/Brunei", "dili": "Asia/Dili",
    # East Asia
    "tokyo": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "busan": "Asia/Seoul",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai", "shenzhen": "Asia/Shanghai",
    "chengdu": "Asia/Shanghai", "wuhan": "Asia/Shanghai",
    "nanjing": "Asia/Shanghai", "hangzhou": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong", "taipei": "Asia/Taipei",
    "yokohama": "Asia/Tokyo", "nagoya": "Asia/Tokyo",
    "sapporo": "Asia/Tokyo", "kobe": "Asia/Tokyo",
    "kyoto": "Asia/Tokyo",
    "fukuoka": "Asia/Tokyo", "kawasaki": "Asia/Tokyo",
    "hiroshima": "Asia/Tokyo", "sendai": "Asia/Tokyo",
    "kitakyushu": "Asia/Tokyo", "naha": "Asia/Tokyo",
    "okinawa": "Asia/Tokyo",
    "incheon": "Asia/Seoul", "daegu": "Asia/Seoul",
    "daejeon": "Asia/Seoul", "gwangju": "Asia/Seoul",
    "ulsan": "Asia/Seoul",
    "tianjin": "Asia/Shanghai", "chongqing": "Asia/Shanghai",
    "xian": "Asia/Shanghai", "xi'an": "Asia/Shanghai",
    "suzhou": "Asia/Shanghai", "dongguan": "Asia/Shanghai",
    "foshan": "Asia/Shanghai", "zhengzhou": "Asia/Shanghai",
    "changsha": "Asia/Shanghai", "kunming": "Asia/Shanghai",
    "dalian": "Asia/Shanghai", "qingdao": "Asia/Shanghai",
    "xiamen": "Asia/Shanghai", "fuzhou": "Asia/Shanghai",
    "harbin": "Asia/Shanghai", "jinan": "Asia/Shanghai",
    "hefei": "Asia/Shanghai", "changchun": "Asia/Shanghai",
    "urumqi": "Asia/Urumqi", "guiyang": "Asia/Shanghai",
    "nanning": "Asia/Shanghai", "lanzhou": "Asia/Shanghai",
    "taiyuan": "Asia/Shanghai", "haikou": "Asia/Shanghai",
    "shenyang": "Asia/Shanghai", "wuxi": "Asia/Shanghai",
    "ningbo": "Asia/Shanghai", "wenzhou": "Asia/Shanghai",
    "macau": "Asia/Macau", "kaohsiung": "Asia/Taipei",
    "taichung": "Asia/Taipei", "tainan": "Asia/Taipei",
    "ulaanbaatar": "Asia/Ulaanbaatar", "pyongyang": "Asia/Pyongyang",
    # Oceania
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane", "perth": "Australia/Perth",
    "adelaide": "Australia/Adelaide",
    "auckland": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "canberra": "Australia/Sydney",
    "gold coast": "Australia/Brisbane",
    "hobart": "Australia/Hobart",
    "darwin": "Australia/Darwin",
    "cairns": "Australia/Brisbane",
    "townsville": "Australia/Brisbane",
    "toowoomba": "Australia/Brisbane",
    "newcastle nsw": "Australia/Sydney",
    "wollongong": "Australia/Sydney",
    "geelong": "Australia/Melbourne",
    "ballarat": "Australia/Melbourne",
    "bendigo": "Australia/Melbourne",
    "launceston": "Australia/Hobart",
    "christchurch": "Pacific/Auckland",
    "hamilton nz": "Pacific/Auckland",
    "tauranga": "Pacific/Auckland",
    "dunedin": "Pacific/Auckland",
    "palmerston north": "Pacific/Auckland",
    "napier": "Pacific/Auckland", "rotorua": "Pacific/Auckland",
    "suva": "Pacific/Fiji", "noumea": "Pacific/Noumea",
    "port moresby": "Pacific/Port_Moresby",
    "apia": "Pacific/Apia", "nuku'alofa": "Pacific/Tongatapu",
    "port vila": "Pacific/Efate",
    # Africa
    "cairo": "Africa/Cairo", "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg", "lagos": "Africa/Lagos",
    "nairobi": "Africa/Nairobi", "casablanca": "Africa/Casablanca",
    "accra": "Africa/Accra", "addis ababa": "Africa/Addis_Ababa",
    "dar es salaam": "Africa/Dar_es_Salaam",
    "kinshasa": "Africa/Kinshasa", "luanda": "Africa/Luanda",
    "tunis": "Africa/Tunis",
    "algiers": "Africa/Algiers", "khartoum": "Africa/Khartoum",
    "kampala": "Africa/Kampala",
    "alexandria": "Africa/Cairo", "giza": "Africa/Cairo",
    "luxor": "Africa/Cairo", "aswan": "Africa/Cairo",
    "durban": "Africa/Johannesburg",
    "pretoria": "Africa/Johannesburg",
    "port elizabeth": "Africa/Johannesburg",
    "bloemfontein": "Africa/Johannesburg",
    "east london": "Africa/Johannesburg",
    "pietermaritzburg": "Africa/Johannesburg",
    "polokwane": "Africa/Johannesburg",
    "maputo": "Africa/Maputo", "harare": "Africa/Harare",
    "bulawayo": "Africa/Harare",
    "lusaka": "Africa/Lusaka", "ndola": "Africa/Lusaka",
    "lilongwe": "Africa/Blantyre", "blantyre": "Africa/Blantyre",
    "windhoek": "Africa/Windhoek", "gaborone": "Africa/Gaborone",
    "mbabane": "Africa/Mbabane",
    "abuja": "Africa/Lagos", "kano": "Africa/Lagos",
    "ibadan": "Africa/Lagos", "port harcourt": "Africa/Lagos",
    "benin city": "Africa/Lagos", "kaduna": "Africa/Lagos",
    "enugu": "Africa/Lagos", "calabar": "Africa/Lagos",
    "douala": "Africa/Douala", "yaounde": "Africa/Douala",
    "dakar": "Africa/Dakar", "abidjan": "Africa/Abidjan",
    "kumasi": "Africa/Accra", "bamako": "Africa/Bamako",
    "conakry": "Africa/Conakry",
    "lome": "Africa/Lome", "cotonou": "Africa/Porto-Novo",
    "niamey": "Africa/Niamey", "ouagadougou": "Africa/Ouagadougou",
    "freetown": "Africa/Freetown", "monrovia": "Africa/Monrovia",
    "banjul": "Africa/Banjul", "nouakchott": "Africa/Nouakchott",
    "antananarivo": "Indian/Antananarivo",
    "port louis": "Indian/Mauritius",
    "mombasa": "Africa/Nairobi", "kisumu": "Africa/Nairobi",
    "nakuru": "Africa/Nairobi",
    "zanzibar": "Africa/Dar_es_Salaam",
    "arusha": "Africa/Dar_es_Salaam",
    "dodoma": "Africa/Dar_es_Salaam",
    "mwanza": "Africa/Dar_es_Salaam",
    "kigali": "Africa/Kigali", "bujumbura": "Africa/Bujumbura",
    "mogadishu": "Africa/Mogadishu", "djibouti": "Africa/Djibouti",
    "asmara": "Africa/Asmara",
    "juba": "Africa/Juba", "ndjamena": "Africa/Ndjamena",
    "bangui": "Africa/Bangui", "libreville": "Africa/Libreville",
    "brazzaville": "Africa/Brazzaville",
    "malabo": "Africa/Malabo", "sao tome": "Africa/Sao_Tome",
    "praia": "Atlantic/Cape_Verde", "mindelo": "Atlantic/Cape_Verde",
}

# Precompiled word-boundary patterns for city matching (avoids re.compile per call)
_CITY_PATTERNS: dict[str, re.Pattern] = {
    city: re.compile(r'\b' + re.escape(city) + r'\b')
    for city in _CITY_TIMEZONES
}


def is_weather_lower_tail(question: str) -> bool:
    """Return True for high-temp lower-tail markets (e.g. "X°C or below").

    By the cutoff time the temperature has cleared X (high is monotonic),
    so NO is the locked side. Buying YES on these would be a forecast bet,
    not arb — caller should restrict to outcome index 1 (No).
    """
    if not isinstance(question, str):
        return False
    q_lower = question.lower()
    if "lowest" in q_lower or "low temp" in q_lower:
        return False
    return "or below" in q_lower or "or lower" in q_lower


def is_weather_temp_known(question: str) -> bool:
    """Check if it's late enough in the city's local time for the temp to be known.

    Daily HIGH temperatures are typically recorded by early-mid afternoon → 3 PM.
    Daily LOW temperatures are typically recorded around sunrise → 9 AM.
    Detects which type from the question text ("lowest" vs "highest").

    Returns True only if the market date is today (local) AND local time >= cutoff,
    OR the market date is in the past (local).
    Returns False if city can't be identified (block unknown — safer to skip).
    Returns True if date can't be parsed (don't block non-date weather markets).
    """
    q_lower = question.lower()
    matched_city = None
    for city, pattern in _CITY_PATTERNS.items():
        # Word-boundary match to avoid "tempe" matching "temperature" etc.
        if pattern.search(q_lower):
            if matched_city is None or len(city) > len(matched_city):
                matched_city = city

    if matched_city is None:
        return False  # Unknown city, block to be safe

    tz = ZoneInfo(_CITY_TIMEZONES[matched_city])
    local_now = datetime.now(tz)
    local_date = local_now.date()
    local_hour = local_now.hour

    # Cutoff depends on market structure (high temp only — lows always 8am).
    # Polymarket phrasings: "X°C or below" / "X°C or higher" / "X°C" (middle).
    # Lower tail ("X or below"):  10am — high is monotonic, settled by mid-morning.
    # Upper tail ("X or higher"): 3pm  — afternoon peak typically passed.
    # Middle bucket ("X°C"):      5pm  — wait for late-afternoon drift to settle.
    is_low_temp = "lowest" in q_lower or "low temp" in q_lower
    if is_low_temp:
        cutoff_hour = 8
    else:
        is_lower_tail = "or below" in q_lower or "or lower" in q_lower
        is_upper_tail = "or higher" in q_lower or "or above" in q_lower
        if is_lower_tail:
            cutoff_hour = 10
        elif is_upper_tail:
            cutoff_hour = 15
        else:
            cutoff_hour = 17

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
    # Today: check if past cutoff hour
    return local_hour >= cutoff_hour


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
