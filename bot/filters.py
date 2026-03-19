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
        "stock", "s&p", "nasdaq", "dow jones", "fed",
        "interest rate", "inflation", "gdp", "unemployment",
        "earnings", "revenue", "ipo", "market cap",
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
        "climate", "nasa", "spacex", "launch",
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
    "new york": -4, "nyc": -4, "boston": -4, "philadelphia": -4,
    "washington": -4, "washington dc": -4, "dc": -4,
    "atlanta": -4, "miami": -4, "orlando": -4, "tampa": -4,
    "jacksonville": -4, "charlotte": -4, "raleigh": -4, "richmond": -4,
    "detroit": -4, "cleveland": -4, "columbus": -4, "cincinnati": -4,
    "pittsburgh": -4, "indianapolis": -4, "baltimore": -4,
    "buffalo": -4, "rochester": -4, "hartford": -4, "providence": -4,
    "norfolk": -4, "virginia beach": -4, "wilmington": -4,
    "charleston": -4, "savannah": -4, "knoxville": -4, "lexington": -4,
    "louisville": -4, "grand rapids": -4, "fort lauderdale": -4,
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
    # US — Mountain (MDT = UTC-6)
    "denver": -6, "salt lake": -6, "salt lake city": -6,
    "albuquerque": -6, "boise": -6, "colorado springs": -6,
    "tucson": -7, "phoenix": -7,  # Arizona no DST
    "billings": -6, "cheyenne": -6, "missoula": -6,
    # US — Pacific (PDT = UTC-7)
    "los angeles": -7, "san francisco": -7, "seattle": -7, "portland": -7,
    "las vegas": -7, "san diego": -7, "san jose": -7, "sacramento": -7,
    "fresno": -7, "oakland": -7, "bakersfield": -7, "riverside": -7,
    "stockton": -7, "spokane": -7, "tacoma": -7, "reno": -7,
    # US — Alaska/Hawaii
    "anchorage": -8, "fairbanks": -8, "juneau": -8,
    "honolulu": -10,
    # Canada — Atlantic (ADT = UTC-3)
    "halifax": -3, "st. john's": -2.5, "fredericton": -3, "moncton": -3,
    "charlottetown": -3,
    # Canada — Eastern (EDT = UTC-4)
    "toronto": -4, "montreal": -4, "ottawa": -4, "quebec city": -4,
    "hamilton": -4, "kitchener": -4, "london ontario": -4,
    "windsor": -4, "mississauga": -4, "brampton": -4, "markham": -4,
    # Canada — Central (CDT = UTC-5)
    "winnipeg": -5, "regina": -6, "saskatoon": -6, "thunder bay": -4,
    # Canada — Mountain/Pacific
    "calgary": -6, "edmonton": -6, "vancouver": -7, "victoria": -7,
    "kelowna": -7, "surrey": -7,
    # Mexico
    "mexico city": -5, "guadalajara": -5, "monterrey": -5,
    "cancun": -5, "tijuana": -7, "puebla": -5, "juarez": -6,
    # Central America & Caribbean
    "havana": -4, "san juan": -4, "santo domingo": -4,
    "guatemala city": -6, "san salvador": -6, "tegucigalpa": -6,
    "managua": -6, "san jose cr": -6, "panama city": -5,
    "kingston": -5, "port-au-prince": -4,
    # South America
    "são paulo": -3, "sao paulo": -3, "rio": -3, "rio de janeiro": -3,
    "brasilia": -3, "buenos aires": -3, "bogota": -5, "lima": -5,
    "santiago": -3, "caracas": -4, "quito": -5, "montevideo": -3,
    "asuncion": -4, "la paz": -4, "medellin": -5, "cali": -5,
    "recife": -3, "belo horizonte": -3, "curitiba": -3,
    # UK & Ireland
    "london": 1, "manchester": 1, "birmingham uk": 1, "glasgow": 1,
    "edinburgh": 1, "liverpool": 1, "bristol": 1, "leeds": 1,
    "dublin": 1, "belfast": 1, "cardiff": 1,
    # Western Europe (CEST = UTC+2)
    "paris": 2, "berlin": 2, "madrid": 2, "rome": 2, "milan": 2,
    "amsterdam": 2, "brussels": 2, "vienna": 2, "zurich": 2,
    "munich": 2, "hamburg": 2, "barcelona": 2, "lisbon": 1,
    "prague": 2, "warsaw": 2, "budapest": 2, "copenhagen": 2,
    "stockholm": 2, "oslo": 2, "helsinki": 3, "athens": 3,
    "bucharest": 3, "sofia": 3, "belgrade": 2, "zagreb": 2,
    "lyon": 2, "marseille": 2, "naples": 2, "turin": 2,
    "frankfurt": 2, "cologne": 2, "dusseldorf": 2, "geneva": 2,
    # Eastern Europe & Turkey
    "istanbul": 3, "moscow": 3, "st. petersburg": 3, "kyiv": 3,
    "minsk": 3, "tbilisi": 4, "yerevan": 4, "baku": 4,
    # Middle East
    "dubai": 4, "abu dhabi": 4, "doha": 3, "riyadh": 3, "jeddah": 3,
    "kuwait city": 3, "muscat": 4, "amman": 3, "beirut": 3,
    "tel aviv": 3, "jerusalem": 3, "baghdad": 3, "tehran": 3.5,
    # South Asia
    "mumbai": 5.5, "delhi": 5.5, "new delhi": 5.5, "bangalore": 5.5,
    "bengaluru": 5.5, "chennai": 5.5, "kolkata": 5.5, "hyderabad": 5.5,
    "pune": 5.5, "ahmedabad": 5.5, "jaipur": 5.5,
    "karachi": 5, "lahore": 5, "islamabad": 5,
    "dhaka": 6, "colombo": 5.5, "kathmandu": 5.75,
    # Southeast Asia
    "bangkok": 7, "jakarta": 7, "singapore": 8, "kuala lumpur": 8,
    "manila": 8, "ho chi minh": 7, "hanoi": 7, "phnom penh": 7,
    "yangon": 6.5,
    # East Asia
    "tokyo": 9, "osaka": 9, "seoul": 9, "busan": 9,
    "beijing": 8, "shanghai": 8, "guangzhou": 8, "shenzhen": 8,
    "chengdu": 8, "wuhan": 8, "nanjing": 8, "hangzhou": 8,
    "hong kong": 8, "taipei": 8,
    # Oceania
    "sydney": 11, "melbourne": 11, "brisbane": 10, "perth": 8,
    "adelaide": 10.5, "auckland": 13, "wellington": 13,
    "canberra": 11, "gold coast": 10, "hobart": 11,
    # Africa
    "cairo": 2, "johannesburg": 2, "cape town": 2, "lagos": 1,
    "nairobi": 3, "casablanca": 1, "accra": 0, "addis ababa": 3,
    "dar es salaam": 3, "kinshasa": 1, "luanda": 1, "tunis": 1,
    "algiers": 1, "khartoum": 2, "kampala": 3,
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
