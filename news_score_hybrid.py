"""
Hybrid News Score - XAU/USD
Project: Trading Bot XAU/USD
Step 5 (REVISED): Score combining multiple real-time sources

Score Composition (0-100):
- 40% = Headline sentiment (web scraping Kitco)
- 30% = Dollar Index DXY (inverse correlation)
- 20% = Treasury Yields 10Y (inverse correlation)
- 10% = VIX Fear Index (direct correlation)

Score:
- 100 = Very bullish for gold
- 0 = Very bearish for gold
- 50 = Neutral
"""

import requests
from bs4 import BeautifulSoup
import yfinance as yf
from datetime import datetime, timedelta
import json
import os
import re
import xml.etree.ElementTree as ET
import config
from logger import log

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = "data"
HYBRID_HISTORY_FILE = os.path.join(DATA_DIR, "news_hybrid_history.json")
FEED_HEALTH_FILE = os.path.join(DATA_DIR, "echo_feed_health.json")

# Cache settings
CACHE_MINUTES = 30  # Updates every 30 min
_hybrid_cache = {
    "result": None,
    "last_update": None,
}

# Component weights
WEIGHTS = {
    "headlines": 0.40,
    "dxy": 0.30,
    "yields": 0.20,
    "vix": 0.10,
}

# User agent for scraping
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ============================================================================
# KEYWORDS FOR SENTIMENT (same as previous file)
# ============================================================================

BULLISH_KEYWORDS = {
    # Fed dovish / rate cuts
    "rate cut": 5, "cut rates": 5, "dovish": 5, "lower rates": 4,
    "pause rate": 3, "hold rates": 2, "rate pause": 3,
    # High inflation (gold is a hedge)
    "inflation rising": 4, "inflation higher": 4, "cpi higher": 4,
    "cpi rises": 4, "inflation concerns": 3, "inflation fears": 3,
    "sticky inflation": 3, "inflation surges": 4, "inflation spikes": 4,
    # Geopolitical tension (safe haven)
    "geopolitical tension": 5, "geopolitical risk": 5, "war": 4, "conflict": 4,
    "military": 3, "sanctions": 3, "crisis": 3, "escalation": 4,
    "invasion": 4, "attack": 3, "missile": 3, "nuclear": 4, "tariff": 3,
    "trade war": 4, "tensions": 3,
    # Weak dollar
    "dollar weak": 4, "dollar falls": 4, "dollar drops": 4, "dxy falls": 4,
    "dxy drops": 4, "dollar index down": 4, "dollar slips": 3, "dollar tumbles": 4,
    "dollar plunges": 5, "dollar selloff": 4, "dollar sell off": 4,
    # Recession / weak economy
    "recession": 4, "recession fears": 4, "economic slowdown": 3,
    "growth slows": 3, "unemployment rises": 3, "job losses": 3,
    "layoffs": 3, "downturn": 3, "contraction": 3,
    # Safe haven / flight to safety
    "safe haven": 5, "flight to safety": 5, "risk-off": 4, "uncertainty": 3,
    "volatility": 2, "market fear": 4, "panic": 4, "turmoil": 4,
    # Yields falling
    "yields fall": 3, "yields drop": 3, "yields decline": 3,
    "bond yields down": 3, "yields tumble": 4, "yields plunge": 4,
    # Gold-specific bullish
    "gold rallies": 5, "gold surges": 5, "gold gains": 4, "gold rises": 4,
    "gold climbs": 4, "gold higher": 3, "gold demand": 3, "gold soars": 5,
    "gold jumps": 4, "gold spikes": 4, "gold record": 5, "gold all-time": 5,
    "gold buying": 3, "central bank buying": 4, "gold rush": 4,
    "gold hits": 3, "gold tops": 3, "gold above": 3, "gold breaks": 3,
    "bullish gold": 4, "buy gold": 4, "gold bulls": 4,
    # Silver bullish (correlated with gold)
    "silver rallies": 3, "silver surges": 3, "silver gains": 3,
    # Precious metals generic
    "precious metals rise": 4, "precious metals rally": 4,
    "metals surge": 3, "metals gain": 3,
    # QE / monetary stimulus
    "quantitative easing": 4, "stimulus": 3, "money printing": 4,
    "easing": 3, "accommodative": 3,
}

BEARISH_KEYWORDS = {
    # Fed hawkish / rate hikes
    "rate hike": -5, "raise rates": -5, "hawkish": -5, "higher rates": -4,
    "tighten": -3, "restrictive": -3, "higher for longer": -4,
    "interest rate": -2, "rate increase": -4, "rate rise": -4,
    # Inflation falling (less need for hedge)
    "inflation cooling": -4, "inflation falls": -4, "cpi lower": -4,
    "cpi falls": -4, "inflation eases": -3, "disinflation": -3,
    "inflation slows": -3, "inflation drops": -4,
    # Strong dollar
    "dollar strong": -4, "dollar rises": -4, "dollar gains": -4, "dxy rises": -4,
    "dxy gains": -4, "dollar index up": -4, "dollar rallies": -4,
    "dollar surges": -5, "dollar soars": -5, "dollar jumps": -4,
    # Strong economy
    "economic growth": -3, "gdp strong": -3, "strong economy": -3,
    "gdp growth": -3, "job growth": -3, "unemployment falls": -3,
    "nonfarm payrolls beat": -4, "jobs beat": -3, "strong jobs": -3,
    # Risk-on
    "risk-on": -3, "risk appetite": -3, "stocks rally": -2,
    "equities gain": -2, "stocks surge": -3, "market rally": -2,
    "s&p 500 record": -2, "nasdaq record": -2,
    # Yields rising
    "yields rise": -3, "yields jump": -3, "yields surge": -4,
    "bond yields up": -3, "yields climb": -3, "yields higher": -3,
    # Gold-specific bearish
    "gold falls": -4, "gold drops": -4, "gold declines": -4, "gold slips": -3,
    "gold lower": -3, "gold selling": -3, "gold outflows": -3,
    "gold tumbles": -5, "gold plunges": -5, "gold crashes": -5,
    "gold sinks": -4, "gold retreats": -3, "gold weakens": -3,
    "bearish gold": -4, "sell gold": -4, "gold bears": -4,
    "gold loses": -3, "gold dips": -3, "gold slides": -4,
    # Generic sell off (very common in headlines)
    "sell off": -4, "selloff": -4, "sell-off": -4,
    "selling pressure": -3, "liquidation": -3, "profit taking": -3,
    "profit-taking": -3,
    # Generic decline (precious metals)
    "plunge": -4, "plummet": -5, "tumble": -4, "crash": -5,
    "collapse": -5, "rout": -4, "nosedive": -5, "freefall": -5,
    # Silver bearish (correlated with gold)
    "silver falls": -3, "silver drops": -3, "silver tumbles": -3,
    "silver sell off": -3, "silver selloff": -3,
    # Precious metals generic bearish
    "precious metals fall": -4, "precious metals drop": -4,
    "metals decline": -3, "metals fall": -3,
    # Tightening / tapering
    "tapering": -3, "taper": -3, "quantitative tightening": -4,
}


# ============================================================================
# FEED HEALTH TRACKING
# ============================================================================

def _load_feed_health() -> dict:
    """Load feed health data from disk."""
    try:
        if os.path.exists(FEED_HEALTH_FILE):
            with open(FEED_HEALTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_feed_health(health: dict) -> None:
    """Save feed health data to disk."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(FEED_HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump(health, f, indent=2, default=str)
    except Exception:
        pass


def record_feed_success(feed_name: str, headlines_count: int) -> None:
    """Record a successful feed fetch."""
    health = _load_feed_health()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entry = health.get(feed_name, {})
    entry["last_success"] = datetime.utcnow().isoformat()
    entry["consecutive_failures"] = 0
    # Daily headline counter
    if entry.get("_count_date") != today:
        entry["headlines_delivered_today"] = 0
        entry["_count_date"] = today
    entry["headlines_delivered_today"] = entry.get("headlines_delivered_today", 0) + headlines_count
    health[feed_name] = entry
    _save_feed_health(health)


def record_feed_failure(feed_name: str, error_type: str) -> None:
    """Record a feed fetch failure. Logs warning at 3+ consecutive failures."""
    health = _load_feed_health()
    entry = health.get(feed_name, {})
    entry["last_failure"] = datetime.utcnow().isoformat()
    entry["last_error"] = error_type
    consec = entry.get("consecutive_failures", 0) + 1
    entry["consecutive_failures"] = consec
    health[feed_name] = entry
    _save_feed_health(health)

    if consec >= 3:
        first_fail = entry.get("last_success", "unknown")
        log.warning(
            f"ECHO_HEALTH | WARN: feed {feed_name} has {consec} consecutive "
            f"failures since {first_fail}"
        )


def get_feed_health_summary() -> dict:
    """Return feed health summary for API/dashboard."""
    health = _load_feed_health()
    total = len(health) if health else 0
    failing = []
    for name, entry in health.items():
        if entry.get("consecutive_failures", 0) >= 3:
            failing.append({
                "name": name,
                "consecutive_failures": entry["consecutive_failures"],
                "last_error": entry.get("last_error", "unknown"),
                "last_success": entry.get("last_success"),
                "last_failure": entry.get("last_failure"),
            })
    healthy = total - len(failing)
    return {
        "total_feeds": total,
        "healthy": healthy,
        "failing": len(failing),
        "failing_feeds": failing,
        "feeds": health,
    }


# ============================================================================
# PART 1: HEADLINE WEB SCRAPING
# ============================================================================

def parse_rss_date(date_str):
    """
    Parse RSS date (RFC 2822 format)
    Returns datetime or None on failure
    """
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str)
    except:
        return None


def get_rss_headlines(max_headlines=20, max_age_hours=24):
    """
    Fetch headlines via RSS feeds (more reliable than scraping)
    FILTERS only news from the last 24 hours
    14 feeds covering: gold, US Fed/dollar, geopolitics, crises, global CB, inflation, reserves, recession, market risk, sanctions, crisis events
    """
    headlines = []
    now = datetime.now().astimezone()  # Timezone-aware
    cutoff = now - timedelta(hours=max_age_hours)
    
    _BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    
    # RSS feeds — (url, source_label, category)
    rss_feeds = [
        # Gold-specific
        (_BASE.format(q="gold+price"), "Google News", "gold"),
        (_BASE.format(q="XAUUSD+forex"), "Google News", "gold"),
        # US monetary policy & dollar
        (_BASE.format(q="federal+reserve+interest+rate"), "Google News", "us_monetary"),
        (_BASE.format(q="dollar+index+DXY"), "Google News", "us_monetary"),
        # Geopolitics
        (_BASE.format(q="geopolitical+tension+OR+military+conflict"), "Google News", "geopolitics"),
        (_BASE.format(q="Middle+East+conflict+OR+NATO+tensions"), "Google News", "geopolitics"),
        # Financial crises
        (_BASE.format(q="bank+crisis+OR+sovereign+debt+default"), "Google News", "financial_crisis"),
        # Global central banks
        (_BASE.format(q="ECB+rate+OR+Bank+of+Japan+rate+OR+PBOC"), "Google News", "global_monetary"),
        # Inflation & commodities
        (_BASE.format(q="global+inflation+OR+oil+price+crude"), "Google News", "inflation_commodities"),
        # Safe haven / gold reserves
        (_BASE.format(q="central+bank+gold+reserves+OR+dedollarization"), "Google News", "safe_haven"),
        # Recession signals
        (_BASE.format(q="recession+OR+economic+slowdown+layoffs"), "Google News", "recession"),
        # Market risk (equity crash → gold safe haven)
        (_BASE.format(q="stock+market+crash+OR+equity+selloff"), "Google News", "market_risk"),
        # Sanctions & trade wars
        (_BASE.format(q="economic+sanctions+OR+trade+war+tariffs"), "Google News", "sanctions"),
        # Crisis events (black swan)
        (_BASE.format(q="nuclear+threat+OR+terrorist+attack+OR+pandemic+crisis"), "Google News", "crisis_events"),
    ]
    
    FEED_TIMEOUT = 5  # seconds per feed — skip slow/down feeds gracefully
    total_found = 0
    filtered_out = 0
    feeds_ok = 0
    feeds_failed = 0
    
    for feed_url, source, category in rss_feeds:
        _feed_label = f"google:{category}"
        _feed_headlines_count = 0
        try:
            response = requests.get(feed_url, headers=HEADERS, timeout=FEED_TIMEOUT)
            response.raise_for_status()
            feeds_ok += 1

            # Sanitize response to strip UTF-16 surrogates that break downstream encoding
            clean_text = response.text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
            soup = BeautifulSoup(clean_text, 'lxml-xml')
            items = soup.find_all('item')

            for item in items[:10]:  # Check more items for filtering
                title = item.find('title')
                pub_date = item.find('pubDate')
                description = item.find('description')
                link = item.find('link')
                
                if not title:
                    continue
                
                title_text = title.get_text(strip=True)
                # Strip surrogates that break UTF-8 encoding (common in RSS feeds)
                title_text = title_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
                if len(title_text) < 15:
                    continue
                
                total_found += 1
                
                # Extract description (article snippet)
                desc_text = ""
                if description:
                    desc_raw = description.get_text(strip=True)
                    desc_clean = re.sub(r'<[^>]+>', '', desc_raw).strip()
                    if len(desc_clean) > 10:
                        desc_text = desc_clean[:500]
                        desc_text = desc_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
                
                # Extract link
                link_text = link.get_text(strip=True) if link else ""
                
                # FILTER BY DATE - last 24h only
                if pub_date:
                    parsed_date = parse_rss_date(pub_date.get_text())
                    if parsed_date:
                        if parsed_date < cutoff:
                            filtered_out += 1
                            continue  # News too old, skip
                        
                        age_hours = (now - parsed_date).total_seconds() / 3600
                        timestamp_str = parsed_date.isoformat()
                    else:
                        age_hours = 0
                        timestamp_str = datetime.now().isoformat()
                else:
                    age_hours = 0
                    timestamp_str = datetime.now().isoformat()
                
                headlines.append({
                    "title": title_text,
                    "description": desc_text,
                    "link": link_text,
                    "source": source,
                    "category": category,
                    "timestamp": timestamp_str,
                    "age_hours": round(age_hours, 1),
                })
                _feed_headlines_count += 1

            record_feed_success(_feed_label, _feed_headlines_count)
        except requests.exceptions.Timeout:
            feeds_failed += 1
            record_feed_failure(_feed_label, "timeout")
            print(f"⚠️ RSS timeout ({FEED_TIMEOUT}s) {category}: skipped")
            continue
        except Exception as e:
            feeds_failed += 1
            record_feed_failure(_feed_label, str(type(e).__name__))
            print(f"⚠️ RSS error {category}: {e}")
            continue

    # Remove duplicates
    seen = set()
    unique = []
    for h in headlines:
        title_lower = h["title"].lower()[:50]
        if title_lower not in seen:
            seen.add(title_lower)
            unique.append(h)
    
    # Sort by most recent first
    unique.sort(key=lambda x: x.get("age_hours", 999))
    
    log.info(f"RSS: {total_found} found, {filtered_out} filtered (>24h), {len(unique)} unique | feeds: {feeds_ok} ok, {feeds_failed} failed")
    
    return unique[:max_headlines]


# ============================================================================
# ECHO DIRECT RSS FEEDS (11 sources — faster than Google News for breaking news)
# ============================================================================
ECHO_DIRECT_FEEDS = [
    # Forex / Gold breaking news
    ("https://www.fxstreet.com/rss", "FXStreet", "forex_gold"),
    ("https://www.fxstreet.com/rss/analysis", "FXStreet Analysis", "technical"),
    # InvestingLive — fast breaking
    ("https://investinglive.com/feed/news/", "InvestingLive", "breaking"),
    ("https://investinglive.com/feed/forexorders/", "InvestingLive Orders", "forex_flow"),
    ("https://investinglive.com/feed/", "InvestingLive All", "general"),
    # Investing.com — commodities & economy
    ("https://investing.com/rss/news_11.rss", "Investing.com", "commodities"),
    ("https://investing.com/rss/news_14.rss", "Investing.com", "economy"),
    # DailyForex
    ("https://www.dailyforex.com/rss/forexnews.xml", "DailyForex", "forex_news"),
    ("https://www.dailyforex.com/rss/technicalanalysis.xml", "DailyForex", "technical"),
    # Community & geopolitics
    ("https://www.myfxbook.com/rss/latest-forex-news", "Myfxbook", "forex_community"),
    ("https://oilprice.com/rss/main", "OilPrice", "oil_geopolitics"),
]


def get_direct_rss_headlines(max_headlines=30, max_age_hours=None):
    """
    Fetch headlines from 11 direct RSS feeds (Echo News Sentinel sources).
    Same pattern as get_rss_headlines() but for non-Google direct feeds.
    Called every 5 min by Echo (ECHO_SCAN_INTERVAL_SECONDS).
    """
    if max_age_hours is None:
        max_age_hours = float(getattr(config, "ECHO_MAX_AGE_HOURS_DIRECT", 6))
    headlines = []
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=max_age_hours)

    FEED_TIMEOUT = 5
    feeds_ok = 0
    feeds_failed = 0
    total_found = 0
    filtered_out = 0

    for feed_url, source, category in ECHO_DIRECT_FEEDS:
        _feed_label = f"direct:{source}"
        _feed_headlines_count = 0
        try:
            response = requests.get(feed_url, headers=HEADERS, timeout=FEED_TIMEOUT)
            response.raise_for_status()
            feeds_ok += 1

            clean_text = response.text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
            soup = BeautifulSoup(clean_text, 'lxml-xml')
            items = soup.find_all('item')

            for item in items[:10]:
                title = item.find('title')
                pub_date = item.find('pubDate')
                description = item.find('description')
                link = item.find('link')

                if not title:
                    continue

                title_text = title.get_text(strip=True)
                title_text = title_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
                if len(title_text) < 15:
                    continue

                total_found += 1

                desc_text = ""
                if description:
                    desc_raw = description.get_text(strip=True)
                    desc_clean = re.sub(r'<[^>]+>', '', desc_raw).strip()
                    if len(desc_clean) > 10:
                        desc_text = desc_clean[:500]
                        desc_text = desc_text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')

                link_text = link.get_text(strip=True) if link else ""

                if pub_date:
                    parsed_date = parse_rss_date(pub_date.get_text())
                    if parsed_date:
                        if parsed_date < cutoff:
                            filtered_out += 1
                            continue
                        age_hours = (now - parsed_date).total_seconds() / 3600
                        timestamp_str = parsed_date.isoformat()
                    else:
                        age_hours = 0
                        timestamp_str = datetime.now().isoformat()
                else:
                    age_hours = 0
                    timestamp_str = datetime.now().isoformat()

                headlines.append({
                    "title": title_text,
                    "description": desc_text,
                    "link": link_text,
                    "source": source,
                    "category": category,
                    "timestamp": timestamp_str,
                    "age_hours": round(age_hours, 1),
                })
                _feed_headlines_count += 1

            record_feed_success(_feed_label, _feed_headlines_count)
        except requests.exceptions.Timeout:
            feeds_failed += 1
            record_feed_failure(_feed_label, "timeout")
            continue
        except Exception as e:
            feeds_failed += 1
            record_feed_failure(_feed_label, str(type(e).__name__))
            continue

    # Deduplicate
    seen = set()
    unique = []
    for h in headlines:
        title_lower = h["title"].lower()[:50]
        if title_lower not in seen:
            seen.add(title_lower)
            unique.append(h)

    unique.sort(key=lambda x: x.get("age_hours", 999))

    log.info(f"[ECHO] Direct RSS: {total_found} found, {filtered_out} filtered (>24h), {len(unique)} unique | feeds: {feeds_ok}/{len(ECHO_DIRECT_FEEDS)} ok, {feeds_failed} failed")

    return unique[:max_headlines]


def get_headlines():
    """
    Fetch headlines via RSS (more reliable)
    """
    headlines = get_rss_headlines()
    
    if not headlines:
        print("⚠️ Could not fetch headlines")
    else:
        print(f"   ✅ {len(headlines)} headlines fetched via RSS")
    
    return headlines


def analyze_headline_sentiment(title):
    """
    Analyze sentiment of a headline
    Returns score 0-100
    """
    text_lower = title.lower()
    
    bullish_score = 0
    bearish_score = 0
    
    for keyword, points in BULLISH_KEYWORDS.items():
        if keyword in text_lower:
            bullish_score += points
    
    for keyword, points in BEARISH_KEYWORDS.items():
        if keyword in text_lower:
            bearish_score += abs(points)
    
    total = bullish_score + bearish_score
    
    if total == 0:
        return 50  # Neutral
    
    # Score = bullish proportion
    raw_score = (bullish_score / total) * 100
    
    # Adjust confidence based on keyword count
    confidence = min(total / 8, 1)
    adjusted_score = 50 + (raw_score - 50) * confidence
    
    return round(adjusted_score, 1)


# ============================================================================
# GPT HEADLINE ANALYSIS
# ============================================================================

GPT_SYSTEM_PROMPT = """You are a gold (XAU/USD) market analyst. Your job is to score news headlines by their likely impact on the PRICE OF GOLD specifically (not stocks, not the general market).

Scale: 0 = very bearish for gold price, 50 = neutral/irrelevant, 100 = very bullish for gold price.

Key relationships:
- Dollar strength / rate hikes / hawkish Fed → BEARISH for gold (lower score)
- Dollar weakness / rate cuts / dovish Fed → BULLISH for gold (higher score)
- Geopolitical tension / war / crisis → BULLISH for gold (safe haven)
- Strong economy / risk-on / stocks rally → BEARISH for gold
- Inflation fears → BULLISH for gold (inflation hedge)
- Gold sell-off / gold drops → BEARISH for gold
- Gold rally / gold surges → BULLISH for gold

If a headline is unrelated to gold or has no clear directional impact, score it 50.

Score reflects expected FUTURE impact on gold price direction, not what already happened. A headline reporting a past sell-off means bearish sentiment persists, but the move is already priced in — score 15-25, not 0. Reserve 0-10 only for events that signal continued catastrophic decline ahead. Reserve 90-100 only for events that signal major sustained rally ahead."""


def analyze_headlines_with_gpt(headlines):
    """
    Analyze headlines via GPT-4o-mini.
    Sends batch of titles + descriptions in a single prompt.
    
    Args:
        headlines: List of dicts with "title" and optionally "description"
    
    Returns:
        Dict {index: score} or None on failure (fallback to keywords)
    """
    if not _openai_available:
        log.warning("OpenAI package not installed. Using keywords as fallback.")
        return None
    
    if not getattr(config, 'USE_GPT_HEADLINES', False):
        return None
    
    api_key = getattr(config, 'OPENAI_API_KEY', '')
    if not api_key:
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Build headline list for the prompt
        headline_lines = []
        for i, h in enumerate(headlines):
            title = h.get("title", "")
            desc = h.get("description", "")
            if desc:
                headline_lines.append(f"{i+1}. {title} — {desc}")
            else:
                headline_lines.append(f"{i+1}. {title}")
        
        headlines_text = "\n".join(headline_lines)
        
        user_prompt = (
            "Score each headline for its impact on gold price. "
            "Return ONLY a JSON object with a \"scores\" key containing an array of "
            "objects with \"index\" (0-based) and \"score\" (integer 0-100). No explanation.\n\n"
            f"Headlines:\n{headlines_text}"
        )
        
        model = getattr(config, 'GPT_MODEL', 'gpt-4o-mini')
        temperature = getattr(config, 'GPT_HEADLINE_TEMPERATURE', 0.1)
        timeout = getattr(config, 'GPT_HEADLINE_TIMEOUT', 15)
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": GPT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=800,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        
        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)
        
        # Extract scores array (may be in "scores" key or be the array itself)
        if isinstance(parsed, dict):
            scores_list = parsed.get("scores", parsed.get("results", parsed.get("headlines", [])))
        elif isinstance(parsed, list):
            scores_list = parsed
        else:
            log.warning(f"GPT returned unexpected format: {type(parsed)}")
            return None
        
        if not isinstance(scores_list, list):
            log.warning(f"GPT scores is not a list: {type(scores_list)}")
            return None
        
        # Validate and extract scores
        result = {}
        for item in scores_list:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            score = item.get("score")
            if idx is None or score is None:
                continue
            try:
                idx = int(idx)
                score = float(score)
            except (ValueError, TypeError):
                continue
            # Ignore indices outside valid range
            if idx < 0 or idx >= len(headlines):
                continue
            # Clamp 0-100
            score = max(0, min(100, score))
            result[idx] = round(score, 1)
        
        if len(result) == 0:
            log.warning("GPT returned 0 valid scores.")
            return None
        
        # Check if we have scores for most headlines
        coverage = len(result) / len(headlines)
        if coverage < 0.5:
            log.warning(f"GPT low coverage: {len(result)}/{len(headlines)} headlines. Using fallback.")
            return None
        
        log.info(f"   🧠 GPT analyzed {len(result)}/{len(headlines)} headlines (model: {model})")
        return result
        
    except json.JSONDecodeError as e:
        log.warning(f"GPT returned invalid JSON: {e}")
        return None
    except Exception as e:
        log.warning(f"Error in GPT headline analysis: {e}")
        return None


def calculate_headlines_score(headlines):
    """
    Calculate average headline score.
    Uses GPT if available, otherwise fallback to keywords.
    More recent headlines have higher weight.
    """
    if not headlines:
        return {
            "score": 50,
            "headlines_count": 0,
            "headlines": [],
            "message": "No headlines available",
        }
    
    # Try GPT first
    gpt_scores = analyze_headlines_with_gpt(headlines)
    used_gpt = gpt_scores is not None
    
    analyzed = []
    total_weighted_score = 0
    total_weight = 0
    
    for i, headline in enumerate(headlines):
        # Use GPT score if available, otherwise keywords
        if used_gpt and i in gpt_scores:
            score = gpt_scores[i]
            method = "gpt"
        else:
            score = analyze_headline_sentiment(headline["title"])
            method = "keywords"
        
        # Decreasing weight (more recent = higher weight)
        weight = 1.0 - (i * 0.05)  # 1.0, 0.95, 0.90, ...
        weight = max(weight, 0.5)
        
        total_weighted_score += score * weight
        total_weight += weight
        
        analyzed.append({
            "title": headline["title"],
            "source": headline["source"],
            "score": score,
            "method": method,
            "weight": round(weight, 2),
            "age_hours": headline.get("age_hours", 0),
            "timestamp": headline.get("timestamp", ""),
        })
    
    final_score = total_weighted_score / total_weight if total_weight > 0 else 50
    
    return {
        "score": round(final_score, 1),
        "headlines_count": len(headlines),
        "headlines": analyzed,
        "analysis_method": "gpt" if used_gpt else "keywords",
    }


# ============================================================================
# PART 2: DOLLAR INDEX (DXY)
# ============================================================================

def get_dxy_data():
    """
    Fetch Dollar Index data via Yahoo Finance
    Tries multiple symbols: DX-Y.NYB, DX=F, UUP (ETF proxy)
    Returns: current_price, change_percent, score
    """
    symbols_to_try = ["DX-Y.NYB", "DX=F", "UUP"]
    
    for symbol in symbols_to_try:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")  # 5 days to ensure data
            
            if hist.empty or len(hist) < 2:
                log.warning(f"DXY: {symbol} insufficient data (hist empty or < 2 days)")
                continue
            
            current_price = hist['Close'].iloc[-1]
            previous_price = hist['Close'].iloc[-2]
            change_percent = ((current_price - previous_price) / previous_price) * 100
            
            # Calculate score (inverse correlation)
            # DXY fell = bullish for gold
            # Formula: score = 50 - (change * 30)
            score = 50 - (change_percent * 30)
            score = max(0, min(100, score))
            
            return {
                "current": round(current_price, 2),
                "change_percent": round(change_percent, 2),
                "score": round(score, 1),
                "symbol": symbol,
            }
            
        except Exception as e:
            log.warning(f"DXY: error with {symbol} — {e}")
            continue
    
    log.warning("DXY: all symbols failed — neutral fallback (score 50)")
    return {
        "current": None,
        "change_percent": 0,
        "score": 50,
        "error": "DXY data unavailable",
    }


# ============================================================================
# PART 3: TREASURY YIELDS 10Y
# ============================================================================

def _yields_score(change_percent):
    """Calculate yields score (inverse correlation): yields fell = bullish for gold"""
    score = 50 - (change_percent * 4)
    return max(0, min(100, score))


def _get_yields_yahoo():
    """Source 1 (primary): Yahoo Finance ^TNX — real-time data"""
    ticker = yf.Ticker("^TNX")
    hist = ticker.history(period="5d")
    
    if hist.empty or len(hist) < 2:
        return None
    
    current_yield = float(hist['Close'].iloc[-1])
    previous_yield = float(hist['Close'].iloc[-2])
    change_percent = ((current_yield - previous_yield) / previous_yield) * 100
    
    return {
        "current": round(current_yield, 2),
        "change_percent": round(change_percent, 2),
        "score": round(_yields_score(change_percent), 1),
        "source": "yahoo",
    }


def _get_yields_treasury_gov():
    """Source 2 (fallback): Treasury.gov XML feed — end-of-day data, no API key"""
    now = datetime.utcnow()
    month_str = now.strftime("%Y%m")
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
        f"?data=daily_treasury_yield_curve&field_tdr_date_value_month={month_str}"
    )
    
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    
    root = ET.fromstring(resp.content)
    
    # Namespace handling — Treasury.gov uses Atom + OData namespaces
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    }
    
    entries = root.findall(".//atom:entry/atom:content/m:properties", ns)
    if len(entries) < 2:
        # If current month has <2 days, try previous month
        prev_month = (now.replace(day=1) - timedelta(days=1))
        prev_month_str = prev_month.strftime("%Y%m")
        url_prev = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
            f"?data=daily_treasury_yield_curve&field_tdr_date_value_month={prev_month_str}"
        )
        resp_prev = requests.get(url_prev, timeout=10)
        resp_prev.raise_for_status()
        root_prev = ET.fromstring(resp_prev.content)
        entries_prev = root_prev.findall(".//atom:entry/atom:content/m:properties", ns)
        # Combine: previous month + current month
        entries = entries_prev + entries
    
    if len(entries) < 2:
        return None
    
    # Last 2 entries (most recent at end)
    latest = entries[-1]
    previous = entries[-2]
    
    val_latest = latest.find("d:BC_10YEAR", ns)
    val_previous = previous.find("d:BC_10YEAR", ns)
    
    if val_latest is None or val_previous is None:
        return None
    if not val_latest.text or not val_previous.text:
        return None
    
    current_yield = float(val_latest.text)
    previous_yield = float(val_previous.text)
    
    if previous_yield == 0:
        return None
    
    change_percent = ((current_yield - previous_yield) / previous_yield) * 100
    
    return {
        "current": round(current_yield, 2),
        "change_percent": round(change_percent, 2),
        "score": round(_yields_score(change_percent), 1),
        "source": "treasury_gov",
    }


def _get_yields_fred():
    """Source 3 (last resort): FRED API series DGS10 — requires FRED_API_KEY"""
    api_key = getattr(config, 'FRED_API_KEY', '')
    if not api_key:
        return None
    
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "DGS10",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,
    }
    
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    
    observations = data.get("observations", [])
    # Filter observations with valid value (FRED uses "." for no data)
    valid = [o for o in observations if o.get("value", ".") != "."]
    
    if len(valid) < 2:
        return None
    
    current_yield = float(valid[0]["value"])
    previous_yield = float(valid[1]["value"])
    
    if previous_yield == 0:
        return None
    
    change_percent = ((current_yield - previous_yield) / previous_yield) * 100
    
    return {
        "current": round(current_yield, 2),
        "change_percent": round(change_percent, 2),
        "score": round(_yields_score(change_percent), 1),
        "source": "fred",
    }


def get_yields_data():
    """
    Fetch Treasury Yields 10Y data via cascade of sources:
    1. Yahoo Finance ^TNX (real-time)
    2. Treasury.gov XML feed (end-of-day, no API key)
    3. FRED API DGS10 (end-of-day, requires FRED_API_KEY)
    
    Returns: current_yield, change_percent, score, source
    """
    sources = [
        ("Yahoo ^TNX", _get_yields_yahoo),
        ("Treasury.gov XML", _get_yields_treasury_gov),
        ("FRED DGS10", _get_yields_fred),
    ]
    
    for name, fetch_fn in sources:
        try:
            result = fetch_fn()
            if result is not None:
                log.info(f"Yields 10Y: {result['current']}% via {result['source']} ({result['change_percent']:+.2f}%)")
                return result
            else:
                log.debug(f"Yields 10Y: {name} — insufficient data")
        except Exception as e:
            log.debug(f"Yields 10Y: {name} — error: {e}")
    
    log.warning("Yields 10Y: all sources failed — neutral fallback (score 50)")
    return {
        "current": None,
        "change_percent": 0,
        "score": 50,
        "source": "fallback",
        "error": "All Yields sources unavailable",
    }


# ============================================================================
# PART 4: VIX (FEAR INDEX)
# ============================================================================

def get_vix_data():
    """
    Fetch VIX data via Yahoo Finance
    Symbol: ^VIX
    Returns: current_value, change_percent, score
    """
    try:
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="5d")
        
        if hist.empty or len(hist) < 2:
            log.warning("VIX: data unavailable (hist empty or < 2 days) — neutral fallback (score 50)")
            return {
                "current": None,
                "change_percent": 0,
                "score": 50,
                "error": "VIX data unavailable",
            }
        
        current_vix = hist['Close'].iloc[-1]
        previous_vix = hist['Close'].iloc[-2]
        change_percent = ((current_vix - previous_vix) / previous_vix) * 100
        
        # Calculate score (direct correlation)
        # VIX rose = bullish for gold (safe haven)
        # Formula: score = 50 + (change * 2)
        score = 50 + (change_percent * 2)
        score = max(0, min(100, score))
        
        # Flag for extreme VIX
        is_extreme = current_vix > 30
        
        return {
            "current": round(current_vix, 2),
            "change_percent": round(change_percent, 2),
            "score": round(score, 1),
            "is_extreme": is_extreme,
        }
        
    except Exception as e:
        log.warning(f"VIX: error fetching data — {e}")
        return {
            "current": None,
            "change_percent": 0,
            "score": 50,
            "error": str(e),
        }


# ============================================================================
# PART 4b: OIL + S&P 500 (for Luna Macro Analyst)
# ============================================================================

def get_oil_data():
    """
    Fetch Crude Oil price via Yahoo Finance.
    Symbol: CL=F (WTI Crude Futures).
    Returns: current, change_percent (24h), change_1h_percent.
    """
    try:
        ticker = yf.Ticker("CL=F")
        hist = ticker.history(period="5d", interval="1h")

        if hist.empty or len(hist) < 2:
            return {"current": None, "change_percent": 0, "change_1h_percent": 0, "error": "Oil data unavailable"}

        current = float(hist['Close'].iloc[-1])
        prev_1h = float(hist['Close'].iloc[-2])
        change_1h = ((current - prev_1h) / prev_1h) * 100

        # 24h change: find bar ~24h ago
        daily = ticker.history(period="5d")
        if len(daily) >= 2:
            prev_day = float(daily['Close'].iloc[-2])
            change_24h = ((current - prev_day) / prev_day) * 100
        else:
            change_24h = change_1h

        return {
            "current": round(current, 2),
            "change_percent": round(change_24h, 2),
            "change_1h_percent": round(change_1h, 2),
        }
    except Exception as e:
        log.warning(f"OIL: error fetching data — {e}")
        return {"current": None, "change_percent": 0, "change_1h_percent": 0, "error": str(e)}


def get_sp500_data():
    """
    Fetch S&P 500 data via Yahoo Finance.
    Symbol: ^GSPC (index) with ES=F (futures) fallback.
    Returns: current, change_percent (24h).
    """
    for symbol in ["^GSPC", "ES=F"]:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")

            if hist.empty or len(hist) < 2:
                continue

            current = float(hist['Close'].iloc[-1])
            previous = float(hist['Close'].iloc[-2])
            change_percent = ((current - previous) / previous) * 100

            return {
                "current": round(current, 2),
                "change_percent": round(change_percent, 2),
                "symbol": symbol,
            }
        except Exception as e:
            log.warning(f"S&P500: error with {symbol} — {e}")
            continue

    return {"current": None, "change_percent": 0, "error": "S&P 500 data unavailable"}


# ============================================================================
# PART 4c: FRED API (Real Yields, Fed Funds, Breakeven Inflation, CPI)
# ============================================================================

FRED_API_KEY = os.environ.get("FRED_API_KEY", getattr(config, "FRED_API_KEY", ""))
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


def get_fred_series(series_id, limit=2):
    """
    Fetch a FRED series. Returns latest + previous observation.
    Graceful fallback if key missing or API fails.
    """
    if not FRED_API_KEY:
        return {"current": None, "previous": None, "change": None, "date": None, "error": "FRED_API_KEY not set"}

    try:
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        resp = requests.get(FRED_BASE, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        obs = data.get("observations", [])
        if not obs:
            return {"current": None, "previous": None, "change": None, "date": None, "error": f"No observations for {series_id}"}

        # FRED returns "." for missing values
        current_raw = obs[0].get("value", ".")
        current = float(current_raw) if current_raw != "." else None

        previous = None
        if len(obs) >= 2:
            prev_raw = obs[1].get("value", ".")
            previous = float(prev_raw) if prev_raw != "." else None

        change = None
        if current is not None and previous is not None:
            change = round(current - previous, 4)

        return {
            "current": current,
            "previous": previous,
            "change": change,
            "date": obs[0].get("date"),
        }
    except Exception as e:
        log.warning(f"FRED {series_id}: error — {e}")
        return {"current": None, "previous": None, "change": None, "date": None, "error": str(e)}


def get_real_yields():
    """10-Year Real Yield (TIPS) — FRED series DFII10."""
    result = get_fred_series("DFII10")
    return {
        "current": result.get("current"),
        "change": result.get("change"),
        "date": result.get("date"),
        "error": result.get("error"),
    }


def get_fed_funds_rate():
    """Effective Federal Funds Rate — FRED series FEDFUNDS."""
    result = get_fred_series("FEDFUNDS")
    return {
        "current": result.get("current"),
        "change": result.get("change"),
        "date": result.get("date"),
        "error": result.get("error"),
    }


def get_breakeven_inflation():
    """10-Year Breakeven Inflation Rate — FRED series T10YIE."""
    result = get_fred_series("T10YIE")
    return {
        "current": result.get("current"),
        "change": result.get("change"),
        "date": result.get("date"),
        "error": result.get("error"),
    }


def get_cpi_data():
    """Consumer Price Index (All Urban) — FRED series CPIAUCSL."""
    result = get_fred_series("CPIAUCSL")
    return {
        "current": result.get("current"),
        "change": result.get("change"),
        "date": result.get("date"),
        "error": result.get("error"),
    }


# ============================================================================
# PART 4d: YAHOO FINANCE EXTRAS (GLD ETF, USD/CNY)
# ============================================================================

def get_gld_data():
    """
    GLD ETF — gold proxy with real volume data.
    Returns: price, volume, change_24h_pct.
    """
    try:
        ticker = yf.Ticker("GLD")
        hist = ticker.history(period="5d")

        if hist.empty or len(hist) < 2:
            return {"current": None, "volume": None, "change_percent": 0, "error": "GLD data unavailable"}

        current = float(hist["Close"].iloc[-1])
        previous = float(hist["Close"].iloc[-2])
        volume = int(hist["Volume"].iloc[-1])
        change = ((current - previous) / previous) * 100

        return {
            "current": round(current, 2),
            "volume": volume,
            "change_percent": round(change, 2),
        }
    except Exception as e:
        log.warning(f"GLD: error — {e}")
        return {"current": None, "volume": None, "change_percent": 0, "error": str(e)}


def get_usdcny_data():
    """
    USD/CNY exchange rate — yuan weakness signals capital flight → gold demand.
    Returns: value, change_24h_pct.
    """
    try:
        ticker = yf.Ticker("CNY=X")
        hist = ticker.history(period="5d")

        if hist.empty or len(hist) < 2:
            return {"current": None, "change_percent": 0, "error": "USD/CNY data unavailable"}

        current = float(hist["Close"].iloc[-1])
        previous = float(hist["Close"].iloc[-2])
        change = ((current - previous) / previous) * 100

        return {
            "current": round(current, 4),
            "change_percent": round(change, 2),
        }
    except Exception as e:
        log.warning(f"USD/CNY: error — {e}")
        return {"current": None, "change_percent": 0, "error": str(e)}


# ============================================================================
# PART 4e: GLD WEEKLY FLOWS (FLO-77)
# ============================================================================

GLD_FLOWS_FILE = os.path.join(DATA_DIR, "gld_weekly_flows.json")


def get_gld_weekly_flows():
    """
    GLD ETF volume-based sentiment indicator.
    Compares last 5 vs previous 5 trading days: volume trend + price direction.

    NOTE: This is a SENTIMENT proxy, not actual ETF flows. yfinance does not
    provide historical shares outstanding, so real flow calculation is not
    possible. Volume + price direction indicates institutional conviction:
    - High volume + price rising = buying conviction (ACCUMULATION)
    - High volume + price falling = selling conviction (DISTRIBUTION)
    - Low volume = no strong conviction (QUIET)

    Stores result in gld_weekly_flows.json (updated once per day).
    """
    # Check cache — only recalculate once per day
    try:
        if os.path.exists(GLD_FLOWS_FILE):
            cached = json.loads(open(GLD_FLOWS_FILE, "r", encoding="utf-8").read())
            if cached.get("last_updated") == datetime.utcnow().strftime("%Y-%m-%d"):
                return cached
    except Exception:
        pass

    try:
        ticker = yf.Ticker("GLD")
        hist = ticker.history(period="1mo")

        if hist.empty or len(hist) < 10:
            return {"direction": None, "volume_change_pct": None, "error": "Insufficient GLD data"}

        closes = hist["Close"].values
        volumes = hist["Volume"].values

        # Volume comparison: last 5 vs previous 5 trading days
        last5_avg_vol = float(volumes[-5:].mean())
        prev5_avg_vol = float(volumes[-10:-5].mean())
        vol_change_pct = ((last5_avg_vol - prev5_avg_vol) / prev5_avg_vol) * 100 if prev5_avg_vol > 0 else 0

        # Price direction
        last5_avg_price = float(closes[-5:].mean())
        prev5_avg_price = float(closes[-10:-5].mean())
        price_change_pct = ((last5_avg_price - prev5_avg_price) / prev5_avg_price) * 100

        price_rising = last5_avg_price > prev5_avg_price
        volume_rising = last5_avg_vol > prev5_avg_vol * 1.1  # >10% increase = meaningful

        if volume_rising and price_rising:
            direction = "ACCUMULATION"
        elif volume_rising and not price_rising:
            direction = "DISTRIBUTION"
        elif not volume_rising and price_rising:
            direction = "QUIET_BID"
        else:
            direction = "QUIET"

        result = {
            "direction": direction,
            "volume_change_pct": round(vol_change_pct, 1),
            "price_change_pct": round(price_change_pct, 1),
            "last5_avg_vol": int(last5_avg_vol),
            "prev5_avg_vol": int(prev5_avg_vol),
            "last5_avg_price": round(last5_avg_price, 2),
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d"),
        }

        # Save to file
        try:
            with open(GLD_FLOWS_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception:
            pass

        return result
    except Exception as e:
        log.warning(f"GLD sentiment: error — {e}")
        return {"direction": None, "volume_change_pct": None, "error": str(e)}


# ============================================================================
# PART 5: COMBINE ALL
# ============================================================================

def calculate_news_score_hybrid():
    """
    Calculate the Hybrid News Score combining all sources
    
    Weights:
    - Headlines: 40%
    - DXY: 30%
    - Yields: 20%
    - VIX: 10%
    """
    verbose = getattr(config, 'VERBOSE_NEWS_LOG', False)
    
    print("Calculating Hybrid News Score...")
    
    # 1. Headlines
    print("  Fetching headlines...")
    headlines = get_headlines()
    headlines_data = calculate_headlines_score(headlines)
    
    if verbose and headlines_data.get("headlines"):
        log.info("   \u2705 Top 3 headlines:")
        for h in headlines_data["headlines"][:3]:
            try:
                ts = datetime.fromisoformat(h.get("timestamp", "")).strftime("%H:%M UTC") if h.get("timestamp") else "??:??"
            except Exception:
                ts = "??:??"
            title_short = h["title"][:60] + ("..." if len(h["title"]) > 60 else "")
            log.info(f'      \u2022 [{ts}] "{title_short}" (score: {h["score"]})')
    
    # 2. DXY
    print("  Fetching DXY...")
    dxy_data = get_dxy_data()
    
    if verbose and dxy_data.get("current"):
        chg = dxy_data.get("change_percent", 0)
        impact = "bullish for gold" if chg < -0.5 else ("bearish for gold" if chg > 0.5 else "neutral")
        log.info(f'  DXY: {dxy_data["current"]} ({chg:+.2f}% today) -> {impact} (score: {dxy_data["score"]})')
    elif not dxy_data.get("current"):
        log.warning(f'  DXY: unavailable - neutral fallback (score {dxy_data["score"]})')
    
    # 3. Yields
    print("  Fetching Treasury Yields...")
    yields_data = get_yields_data()
    
    if verbose and yields_data.get("current"):
        chg = yields_data.get("change_percent", 0)
        impact = "bullish for gold" if chg < -0.5 else ("bearish for gold" if chg > 0.5 else "neutral")
        log.info(f'  Yields 10Y: {yields_data["current"]}% ({chg:+.2f}% today) -> {impact} (score: {yields_data["score"]})')
    elif not yields_data.get("current"):
        log.warning(f'  Yields 10Y: unavailable - neutral fallback (score {yields_data["score"]})')
    
    # 4. VIX
    print("  Fetching VIX...")
    vix_data = get_vix_data()

    if verbose and vix_data.get("current"):
        chg = vix_data.get("change_percent", 0)
        impact = "bullish for gold" if chg > 0.5 else ("bearish for gold" if chg < -0.5 else "neutral")
        log.info(f'  VIX: {vix_data["current"]} ({chg:+.2f}% today) -> {impact} (score: {vix_data["score"]})')
    elif not vix_data.get("current"):
        log.warning(f'  VIX: unavailable - neutral fallback (score {vix_data["score"]})')

    # 5. Oil + S&P 500 (for Luna, not weighted into news score)
    oil_data = get_oil_data()
    sp500_data = get_sp500_data()

    # 6. Extended macro feeds (for Luna + dashboards, not weighted into news score)
    gld_data = get_gld_data()
    usdcny_data = get_usdcny_data()
    real_yields_data = get_real_yields()
    fed_funds_data = get_fed_funds_rate()
    breakeven_data = get_breakeven_inflation()
    cpi_data_result = get_cpi_data()

    # Calculate weighted final score
    final_score = (
        headlines_data["score"] * WEIGHTS["headlines"] +
        dxy_data["score"] * WEIGHTS["dxy"] +
        yields_data["score"] * WEIGHTS["yields"] +
        vix_data["score"] * WEIGHTS["vix"]
    )
    final_score = round(final_score, 1)
    
    if verbose:
        log.info(f'  📊 News Score: {final_score}/100 (headlines: {headlines_data["score"]}, DXY: {dxy_data["score"]}, Yields: {yields_data["score"]}, VIX: {vix_data["score"]})')
    
    # Interpretation
    if final_score >= 70:
        interpretation = "🟢 VERY BULLISH FOR GOLD"
    elif final_score >= 60:
        interpretation = "🟢 BULLISH FOR GOLD"
    elif final_score >= 55:
        interpretation = "🟡 SLIGHTLY BULLISH"
    elif final_score <= 30:
        interpretation = "🔴 VERY BEARISH FOR GOLD"
    elif final_score <= 40:
        interpretation = "🔴 BEARISH FOR GOLD"
    elif final_score <= 45:
        interpretation = "🟡 SLIGHTLY BEARISH"
    else:
        interpretation = "⚪ NEUTRAL"
    
    # Detect anomalies
    anomalies = []
    if dxy_data.get("change_percent") and abs(dxy_data["change_percent"]) > 2:
        anomalies.append(f"DXY extreme move: {dxy_data['change_percent']:+.2f}%")
    if vix_data.get("is_extreme"):
        anomalies.append(f"VIX at panic level: {vix_data['current']}")
    
    return {
        "score": final_score,
        "interpretation": interpretation,
        "timestamp": datetime.now().isoformat(),
        "components": {
            "headlines": {
                "score": headlines_data["score"],
                "weight": WEIGHTS["headlines"],
                "count": headlines_data["headlines_count"],
                "details": headlines_data.get("headlines", [])[:8],
            },
            "dxy": {
                "score": dxy_data["score"],
                "weight": WEIGHTS["dxy"],
                "current": dxy_data.get("current"),
                "change_percent": dxy_data.get("change_percent"),
            },
            "yields": {
                "score": yields_data["score"],
                "weight": WEIGHTS["yields"],
                "current": yields_data.get("current"),
                "change_percent": yields_data.get("change_percent"),
            },
            "vix": {
                "score": vix_data["score"],
                "weight": WEIGHTS["vix"],
                "current": vix_data.get("current"),
                "change_percent": vix_data.get("change_percent"),
            },
            "oil": {
                "current": oil_data.get("current"),
                "change_percent": oil_data.get("change_percent"),
                "change_1h_percent": oil_data.get("change_1h_percent"),
            },
            "sp500": {
                "current": sp500_data.get("current"),
                "change_percent": sp500_data.get("change_percent"),
            },
            "gld": {
                "current": gld_data.get("current"),
                "volume": gld_data.get("volume"),
                "change_percent": gld_data.get("change_percent"),
            },
            "usdcny": {
                "current": usdcny_data.get("current"),
                "change_percent": usdcny_data.get("change_percent"),
            },
            "real_yields": {
                "current": real_yields_data.get("current"),
                "change": real_yields_data.get("change"),
                "date": real_yields_data.get("date"),
            },
            "fed_funds": {
                "current": fed_funds_data.get("current"),
                "change": fed_funds_data.get("change"),
                "date": fed_funds_data.get("date"),
            },
            "breakeven": {
                "current": breakeven_data.get("current"),
                "change": breakeven_data.get("change"),
                "date": breakeven_data.get("date"),
            },
            "cpi": {
                "current": cpi_data_result.get("current"),
                "change": cpi_data_result.get("change"),
                "date": cpi_data_result.get("date"),
            },
        },
        "anomalies": anomalies,
    }


# ============================================================================
# CACHE
# ============================================================================

def get_hybrid_score_cached(force_refresh=False):
    """
    Return the hybrid news score using cache
    Cache valid for CACHE_MINUTES (default: 30 min)
    """
    global _hybrid_cache
    
    now = datetime.now()
    
    # Check if cache is valid
    if not force_refresh and _hybrid_cache["last_update"] is not None:
        cache_age = (now - _hybrid_cache["last_update"]).total_seconds() / 60
        
        if cache_age < CACHE_MINUTES:
            if getattr(config, 'VERBOSE_NEWS_LOG', False):
                log.info(f"  📰 News Score: {_hybrid_cache['result']['score']}/100 (cache: {round(cache_age, 0):.0f}min ago)")
            return {
                "score": _hybrid_cache["result"]["score"],
                "result": _hybrid_cache["result"],
                "from_cache": True,
                "cache_age_minutes": round(cache_age, 1),
                "next_update_minutes": round(CACHE_MINUTES - cache_age, 1),
            }
    
    # Cache expired - fetch new data
    print(f"🔄 Updating Hybrid News Score...")
    result = calculate_news_score_hybrid()
    
    # Update cache
    _hybrid_cache["result"] = result
    _hybrid_cache["last_update"] = now
    
    # Save history
    save_hybrid_history(result)
    
    return {
        "score": result["score"],
        "result": result,
        "from_cache": False,
        "cache_age_minutes": 0,
        "next_update_minutes": CACHE_MINUTES,
    }


# ============================================================================
# HISTORY
# ============================================================================

def save_hybrid_history(result):
    """Save result to history"""
    history = load_hybrid_history()
    
    entry = {
        "timestamp": result["timestamp"],
        "score": result["score"],
        "interpretation": result["interpretation"],
        "headlines_score": result["components"]["headlines"]["score"],
        "dxy_score": result["components"]["dxy"]["score"],
        "dxy_value": result["components"]["dxy"]["current"],
        "yields_score": result["components"]["yields"]["score"],
        "yields_value": result["components"]["yields"]["current"],
        "vix_score": result["components"]["vix"]["score"],
        "vix_value": result["components"]["vix"]["current"],
    }
    
    history.append(entry)
    
    # Keep only last 7 days
    cutoff = datetime.now() - timedelta(days=7)
    history = [h for h in history if datetime.fromisoformat(h["timestamp"]) > cutoff]
    
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HYBRID_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)


def load_hybrid_history():
    """Load history"""
    if os.path.exists(HYBRID_HISTORY_FILE):
        with open(HYBRID_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return []


# ============================================================================
# DISPLAY
# ============================================================================

def display_hybrid_score(result):
    """Display the hybrid score in formatted output"""
    print("\n" + "━" * 60)
    print("📰 HYBRID NEWS SCORE - Real Time")
    print("━" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Headlines
    h = result["components"]["headlines"]
    print(f"\n🌐 HEADLINES - last 24h (weight: {int(h['weight']*100)}%)")
    print(f"   Found: {h['count']} recent headlines")
    if h.get("details"):
        for i, headline in enumerate(h["details"][:5], 1):
            emoji = "🟢" if headline["score"] >= 60 else ("🔴" if headline["score"] <= 40 else "⚪")
            age = headline.get("age_hours", "?")
            print(f"   {i}. [{age}h ago] \"{headline['title'][:45]}...\"")
            print(f"      → Score: {headline['score']} {emoji}")
    else:
        print(f"   ⚠️ No headlines from the last 24h")
    print(f"   → Headlines Score: {h['score']}/100")
    
    # DXY
    d = result["components"]["dxy"]
    print(f"\n💵 DOLLAR INDEX - DXY (weight: {int(d['weight']*100)}%)")
    if d["current"]:
        direction = "📉" if d["change_percent"] < 0 else "📈"
        print(f"   Current: {d['current']} | Change 24h: {d['change_percent']:+.2f}% {direction}")
        interpretation = "bearish for dollar = bullish for gold" if d["change_percent"] < 0 else "bullish for dollar = bearish for gold"
        print(f"   → DXY Score: {d['score']}/100 ({interpretation})")
    else:
        print(f"   ⚠️ Data unavailable")
    
    # Yields
    y = result["components"]["yields"]
    print(f"\n📊 TREASURY YIELDS 10Y (weight: {int(y['weight']*100)}%)")
    if y["current"]:
        direction = "📉" if y["change_percent"] < 0 else "📈"
        print(f"   Current: {y['current']}% | Change 24h: {y['change_percent']:+.2f}% {direction}")
        print(f"   → Yields Score: {y['score']}/100")
    else:
        print(f"   ⚠️ Data unavailable")
    
    # VIX
    v = result["components"]["vix"]
    print(f"\n😱 VIX - Fear Index (weight: {int(v['weight']*100)}%)")
    if v["current"]:
        direction = "📈" if v["change_percent"] > 0 else "📉"
        level = "🔴 HIGH" if v["current"] > 25 else ("🟡 ELEVATED" if v["current"] > 20 else "🟢 NORMAL")
        print(f"   Current: {v['current']} ({level}) | Change 24h: {v['change_percent']:+.2f}% {direction}")
        print(f"   → VIX Score: {v['score']}/100")
    else:
        print(f"   ⚠️ Data unavailable")
    
    # Anomalies
    if result.get("anomalies"):
        print(f"\n⚠️ ANOMALIES DETECTED:")
        for anomaly in result["anomalies"]:
            print(f"   • {anomaly}")
    
    # Score Final
    print("\n" + "━" * 60)
    print(f"📊 NEWS SCORE FINAL: {result['score']}/100")
    print(f"   → {result['interpretation']}")
    print("━" * 60)


# ============================================================================
# DETAILED ANALYSIS (for the Central Brain)
# ============================================================================

def get_news_detailed() -> dict:
    """
    Return detailed news/fundamentals data for the Central Brain.
    
    Uses the hybrid score cache to avoid extra requests.
    Returns all raw data + score.
    
    Returns:
        Dict with all detailed fundamentals data
    """
    # Get data via cache
    cached = get_hybrid_score_cached()
    result = cached.get("result", {})
    score = cached.get("score", 50.0)
    
    components = result.get("components", {})
    
    # DXY
    dxy_comp = components.get("dxy", {})
    dxy_value = dxy_comp.get("current")
    dxy_change = dxy_comp.get("change_percent", 0)
    dxy_trend = "falling" if (dxy_change and dxy_change < 0) else "rising"
    
    # Yields
    yields_comp = components.get("yields", {})
    yields_value = yields_comp.get("current")
    yields_change = yields_comp.get("change_percent", 0)
    yields_trend = "falling" if (yields_change and yields_change < 0) else "rising"
    
    # VIX
    vix_comp = components.get("vix", {})
    vix_value = vix_comp.get("current")
    vix_level = "high" if (vix_value and vix_value > 20) else "low"
    
    # Headlines sentiment (normalized from -1 to +1)
    headlines_comp = components.get("headlines", {})
    headlines_score = headlines_comp.get("score", 50)
    # Convert 0-100 to -1 to +1: (score - 50) / 50
    sentiment_normalized = round((headlines_score - 50) / 50, 2)

    # FLO-111: Oil + S&P 500 (pass through from components)
    oil_comp = components.get("oil", {})
    sp500_comp = components.get("sp500", {})

    return {
        "score": score,
        "dxy": {
            "value": dxy_value,
            "trend": dxy_trend,
            "change_24h": dxy_change,
        },
        "yields": {
            "value": yields_value,
            "trend": yields_trend,
            "change_24h": yields_change,
        },
        "vix": {
            "value": vix_value,
            "level": vix_level,
        },
        "oil": {
            "current": oil_comp.get("current"),
            "change_percent": oil_comp.get("change_percent", 0),
        },
        "sp500": {
            "current": sp500_comp.get("current"),
            "change_percent": sp500_comp.get("change_percent", 0),
        },
        "sentiment": {
            "headlines_score": headlines_score,
            "normalized": sentiment_normalized,
        },
        "high_impact_news_soon": False,  # Default: no economic calendar data
        "geopolitical_risk": "low",  # Default: neutral, does not influence decision
        "anomalies": result.get("anomalies", []),
        "error": None,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("HYBRID NEWS SCORE - XAU/USD Trading Bot")
    print("=" * 60)
    print("\nComposition:")
    print("  • 40% = Headlines (web scraping)")
    print("  • 30% = Dollar Index (DXY)")
    print("  • 20% = Treasury Yields 10Y")
    print("  • 10% = VIX (Fear Index)")
    
    # Calculate score
    cached_result = get_hybrid_score_cached(force_refresh=True)
    
    # Display result
    display_hybrid_score(cached_result["result"])
    
    # Cache info
    print(f"\n🔄 Next update in {cached_result['next_update_minutes']:.0f} minutes")
    print(f"✅ History saved to: {HYBRID_HISTORY_FILE}")


if __name__ == "__main__":
    main()
