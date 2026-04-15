"""
News Sentiment Analysis - XAU/USD
Project: Trading Bot XAU/USD
Step 5: Collect news and analyze sentiment for gold

Score:
- 100 = Very bullish for gold
- 0 = Very bearish for gold
- 50 = Neutral
"""

import requests
from datetime import datetime, timedelta
from tz_utils import utc_iso  # FLO-309
import json
import os
import re
from collections import defaultdict
import hashlib

# ============================================================================
# CONFIGURATION
# ============================================================================

# NewsAPI Key - YOU NEED TO CREATE AN ACCOUNT AT newsapi.org AND GET YOUR KEY
# Free plan: 100 requests/day
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# Cache settings - update news every 30-60min to save requests
NEWS_CACHE_MINUTES = 45  # Updates every 45 min (~32 requests/day)
_news_cache = {
    "score": None,
    "result": None,
    "last_update": None,
}

# Directory to save history
DATA_DIR = "data"
NEWS_HISTORY_FILE = os.path.join(DATA_DIR, "news_history.json")

# Keywords to search for gold-related news
SEARCH_KEYWORDS = [
    "gold price",
    "XAU USD",
    "Federal Reserve",
    "Fed interest rate",
    "inflation CPI",
    "dollar index DXY",
    "treasury yields",
    "safe haven gold",
]

# Trusted sources (higher weight)
PREMIUM_SOURCES = [
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "cnbc.com",
    "marketwatch.com",
]

# Gold-specific sources
GOLD_SOURCES = [
    "kitco.com",
    "fxstreet.com",
    "investing.com",
    "dailyfx.com",
]

# ============================================================================
# KEYWORDS FOR SENTIMENT ANALYSIS
# ============================================================================

# BULLISH keywords for gold (things that make gold rise)
BULLISH_KEYWORDS = {
    # Fed dovish / rate cuts
    "rate cut": 5,
    "cut rates": 5,
    "dovish": 5,
    "lower rates": 4,
    "pause rate": 3,
    "hold rates": 2,
    "rate pause": 3,
    
    # High inflation (gold is a hedge)
    "inflation rising": 4,
    "inflation higher": 4,
    "cpi higher": 4,
    "cpi rises": 4,
    "inflation concerns": 3,
    "inflation fears": 3,
    "sticky inflation": 3,
    "inflation surges": 4,
    "inflation spikes": 4,
    
    # Geopolitical tension (safe haven)
    "geopolitical tension": 5,
    "geopolitical risk": 5,
    "war": 4,
    "conflict": 4,
    "military": 3,
    "sanctions": 3,
    "crisis": 3,
    "escalation": 4,
    "invasion": 4,
    "attack": 3,
    "missile": 3,
    "nuclear": 4,
    "tariff": 3,
    "trade war": 4,
    "tensions": 3,
    
    # Weak dollar
    "dollar weak": 4,
    "dollar falls": 4,
    "dollar drops": 4,
    "dxy falls": 4,
    "dxy drops": 4,
    "dollar index down": 4,
    "dollar slips": 3,
    "dollar tumbles": 4,
    "dollar plunges": 5,
    "dollar selloff": 4,
    "dollar sell off": 4,
    
    # Recession / weak economy
    "recession": 4,
    "recession fears": 4,
    "economic slowdown": 3,
    "growth slows": 3,
    "unemployment rises": 3,
    "job losses": 3,
    "layoffs": 3,
    "downturn": 3,
    "contraction": 3,
    
    # Safe haven / flight to safety
    "safe haven": 5,
    "flight to safety": 5,
    "risk-off": 4,
    "uncertainty": 3,
    "volatility": 2,
    "market fear": 4,
    "panic": 4,
    "turmoil": 4,
    
    # Yields falling
    "yields fall": 3,
    "yields drop": 3,
    "yields decline": 3,
    "bond yields down": 3,
    "yields tumble": 4,
    "yields plunge": 4,
    
    # Gold-specific bullish
    "gold rallies": 5,
    "gold surges": 5,
    "gold gains": 4,
    "gold rises": 4,
    "gold climbs": 4,
    "gold higher": 3,
    "gold demand": 3,
    "central bank buying": 4,
    "gold buying": 3,
    "gold soars": 5,
    "gold jumps": 4,
    "gold spikes": 4,
    "gold record": 5,
    "gold all-time": 5,
    "gold rush": 4,
    "gold hits": 3,
    "gold tops": 3,
    "gold above": 3,
    "gold breaks": 3,
    "bullish gold": 4,
    "buy gold": 4,
    "gold bulls": 4,
    
    # Silver bullish (correlated with gold)
    "silver rallies": 3,
    "silver surges": 3,
    "silver gains": 3,
    
    # Precious metals generic
    "precious metals rise": 4,
    "precious metals rally": 4,
    "metals surge": 3,
    "metals gain": 3,
    
    # QE / monetary stimulus
    "quantitative easing": 4,
    "stimulus": 3,
    "money printing": 4,
    "easing": 3,
    "accommodative": 3,
}

# BEARISH keywords for gold (things that make gold fall)
BEARISH_KEYWORDS = {
    # Fed hawkish / rate hikes
    "rate hike": -5,
    "raise rates": -5,
    "hawkish": -5,
    "higher rates": -4,
    "tighten": -3,
    "restrictive": -3,
    "higher for longer": -4,
    "interest rate": -2,
    "rate increase": -4,
    "rate rise": -4,
    
    # Inflation falling
    "inflation cooling": -4,
    "inflation falls": -4,
    "cpi lower": -4,
    "cpi falls": -4,
    "inflation eases": -3,
    "disinflation": -3,
    "inflation slows": -3,
    "inflation drops": -4,
    
    # Strong dollar
    "dollar strong": -4,
    "dollar rises": -4,
    "dollar gains": -4,
    "dxy rises": -4,
    "dxy gains": -4,
    "dollar index up": -4,
    "dollar rallies": -4,
    "dollar surges": -5,
    "dollar soars": -5,
    "dollar jumps": -4,
    
    # Economia forte
    "economic growth": -3,
    "gdp strong": -3,
    "gdp growth": -3,
    "strong economy": -3,
    "job growth": -3,
    "unemployment falls": -3,
    "nonfarm payrolls beat": -4,
    "jobs beat": -3,
    "strong jobs": -3,
    
    # Risk-on
    "risk-on": -3,
    "risk appetite": -3,
    "stocks rally": -2,
    "equities gain": -2,
    "stocks surge": -3,
    "market rally": -2,
    "s&p 500 record": -2,
    "nasdaq record": -2,
    
    # Yields rising
    "yields rise": -3,
    "yields jump": -3,
    "yields surge": -4,
    "bond yields up": -3,
    "yields climb": -3,
    "yields higher": -3,
    
    # Gold-specific bearish
    "gold falls": -4,
    "gold drops": -4,
    "gold declines": -4,
    "gold slips": -3,
    "gold lower": -3,
    "gold selling": -3,
    "gold outflows": -3,
    "gold tumbles": -5,
    "gold plunges": -5,
    "gold crashes": -5,
    "gold sinks": -4,
    "gold retreats": -3,
    "gold weakens": -3,
    "bearish gold": -4,
    "sell gold": -4,
    "gold bears": -4,
    "gold loses": -3,
    "gold dips": -3,
    "gold slides": -4,
    
    # Generic sell off (very common in headlines)
    "sell off": -4,
    "selloff": -4,
    "sell-off": -4,
    "selling pressure": -3,
    "liquidation": -3,
    "profit taking": -3,
    "profit-taking": -3,
    
    # Generic decline (precious metals)
    "plunge": -4,
    "plummet": -5,
    "tumble": -4,
    "crash": -5,
    "collapse": -5,
    "rout": -4,
    "nosedive": -5,
    "freefall": -5,
    
    # Silver bearish (correlated with gold)
    "silver falls": -3,
    "silver drops": -3,
    "silver tumbles": -3,
    "silver sell off": -3,
    "silver selloff": -3,
    
    # Precious metals generic bearish
    "precious metals fall": -4,
    "precious metals drop": -4,
    "metals decline": -3,
    "metals fall": -3,
    
    # Tightening / tapering
    "tapering": -3,
    "taper": -3,
    "quantitative tightening": -4,
}

# ============================================================================
# HIGH-IMPACT EVENTS
# ============================================================================

HIGH_IMPACT_EVENTS = [
    "fomc",
    "fed decision",
    "federal reserve decision",
    "interest rate decision",
    "nfp",
    "non-farm payroll",
    "nonfarm payroll",
    "cpi release",
    "cpi report",
    "inflation report",
    "pce",
    "gdp release",
    "gdp report",
    "jackson hole",
    "powell speech",
    "fed chair",
]


# ============================================================================
# NEWS COLLECTION FUNCTIONS
# ============================================================================

def get_recent_news(days=7, max_results=50):
    """
    Fetch news from the last X days using NewsAPI
    Note: Free plan has delay, so we fetch last 7 days
    Returns list of dicts with: title, description, source, published_at, url
    """
    if NEWSAPI_KEY == "YOUR_API_KEY_HERE":
        print("⚠️  NewsAPI key not configured!")
        print("   Go to https://newsapi.org and create a free account")
        print("   Then replace YOUR_API_KEY_HERE with your key")
        return []
    
    news_list = []
    seen_titles = set()  # To avoid duplicates
    
    # Calculate start date (last 7 days for free plan)
    from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # Search for each keyword
    for keyword in SEARCH_KEYWORDS[:3]:  # Limit to avoid exceeding quota
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": keyword,
                "from": from_date,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 20,
                "apiKey": NEWSAPI_KEY,
            }
            
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            if data.get("status") == "ok":
                for article in data.get("articles", []):
                    # Create title hash to detect duplicates
                    title_hash = hashlib.md5(article["title"].lower().encode()).hexdigest()
                    
                    if title_hash not in seen_titles:
                        seen_titles.add(title_hash)
                        
                        news_item = {
                            "title": article["title"],
                            "description": article.get("description", ""),
                            "source": article["source"]["name"],
                            "source_url": article.get("url", ""),
                            "published_at": article["publishedAt"],
                            "keyword": keyword,
                        }
                        news_list.append(news_item)
            
        except Exception as e:
            print(f"Error fetching '{keyword}': {e}")
            continue
    
    # Sort by date (most recent first)
    news_list.sort(key=lambda x: x["published_at"], reverse=True)
    
    return news_list[:max_results]


def get_mock_news():
    """
    Return example news for testing (when no API key is available)
    """
    mock_news = [
        {
            "title": "Federal Reserve signals potential rate cuts in 2024 amid cooling inflation",
            "description": "Fed officials indicated they may begin cutting interest rates as inflation shows signs of easing toward the 2% target.",
            "source": "Reuters",
            "source_url": "https://reuters.com/example1",
            "published_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "keyword": "Federal Reserve",
        },
        {
            "title": "Gold prices surge to new highs as geopolitical tensions escalate",
            "description": "Safe haven demand pushes gold above $2,700 as investors seek protection amid Middle East conflict.",
            "source": "Bloomberg",
            "source_url": "https://bloomberg.com/example2",
            "published_at": (datetime.now() - timedelta(hours=4)).isoformat(),
            "keyword": "gold price",
        },
        {
            "title": "Dollar weakens against major currencies after soft jobs data",
            "description": "The dollar index fell 0.5% as unemployment claims rose more than expected.",
            "source": "CNBC",
            "source_url": "https://cnbc.com/example3",
            "published_at": (datetime.now() - timedelta(hours=6)).isoformat(),
            "keyword": "dollar index DXY",
        },
        {
            "title": "Treasury yields drop as investors flee to bonds",
            "description": "10-year Treasury yield falls to 4.2% amid flight to safety.",
            "source": "MarketWatch",
            "source_url": "https://marketwatch.com/example4",
            "published_at": (datetime.now() - timedelta(hours=8)).isoformat(),
            "keyword": "treasury yields",
        },
        {
            "title": "Gold holds steady near record levels ahead of Fed decision",
            "description": "Precious metal consolidates gains as traders await FOMC meeting outcome.",
            "source": "Kitco",
            "source_url": "https://kitco.com/example5",
            "published_at": (datetime.now() - timedelta(hours=10)).isoformat(),
            "keyword": "gold price",
        },
        {
            "title": "Inflation remains sticky despite Fed efforts",
            "description": "Core CPI came in higher than expected at 3.8%, raising concerns about prolonged high rates.",
            "source": "FXStreet",
            "source_url": "https://fxstreet.com/example6",
            "published_at": (datetime.now() - timedelta(hours=12)).isoformat(),
            "keyword": "inflation CPI",
        },
        {
            "title": "Central banks continue gold buying spree",
            "description": "Global central banks added 50 tons of gold to reserves in Q3, supporting prices.",
            "source": "Reuters",
            "source_url": "https://reuters.com/example7",
            "published_at": (datetime.now() - timedelta(hours=14)).isoformat(),
            "keyword": "gold price",
        },
        {
            "title": "Risk-off sentiment dominates markets amid recession fears",
            "description": "Investors move to safe haven assets as economic data disappoints.",
            "source": "Bloomberg",
            "source_url": "https://bloomberg.com/example8",
            "published_at": (datetime.now() - timedelta(hours=18)).isoformat(),
            "keyword": "safe haven gold",
        },
    ]
    return mock_news


# ============================================================================
# SENTIMENT ANALYSIS FUNCTIONS
# ============================================================================

def analyze_sentiment(text):
    """
    Analyze text sentiment using keyword matching
    Returns: score (0-100), keywords_found (dict)
    """
    if not text:
        return 50, {}
    
    text_lower = text.lower()
    
    bullish_score = 0
    bearish_score = 0
    keywords_found = {"bullish": [], "bearish": []}
    
    # Search bullish keywords
    for keyword, points in BULLISH_KEYWORDS.items():
        if keyword in text_lower:
            bullish_score += points
            keywords_found["bullish"].append((keyword, points))
    
    # Search bearish keywords
    for keyword, points in BEARISH_KEYWORDS.items():
        if keyword in text_lower:
            bearish_score += abs(points)
            keywords_found["bearish"].append((keyword, points))
    
    # Calculate final score (0-100)
    # If only bullish: tends to 100
    # If only bearish: tends to 0
    # If balanced: tends to 50
    
    total_points = bullish_score + bearish_score
    
    if total_points == 0:
        return 50, keywords_found  # Neutral if no keywords found
    
    # Score = bullish proportion of total, scaled to 0-100
    raw_score = (bullish_score / total_points) * 100
    
    # Adjust to avoid extremes without many keywords
    # If few keywords, pull toward 50 (neutral)
    confidence = min(total_points / 10, 1)  # Confidence based on quantity
    adjusted_score = 50 + (raw_score - 50) * confidence
    
    return round(adjusted_score, 1), keywords_found


def get_source_weight(source_name):
    """
    Return source weight (premium sources have higher weight)
    """
    source_lower = source_name.lower()
    
    for premium in PREMIUM_SOURCES:
        if premium.replace(".com", "") in source_lower:
            return 1.5
    
    for gold_source in GOLD_SOURCES:
        if gold_source.replace(".com", "") in source_lower:
            return 1.3
    
    return 1.0


def get_recency_weight(published_at):
    """
    Return weight based on how recent the news is
    More recent = higher weight
    """
    try:
        if isinstance(published_at, str):
            # Parse ISO format
            pub_date = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
            pub_date = pub_date.replace(tzinfo=None)
        else:
            pub_date = published_at
        
        hours_ago = (datetime.now() - pub_date).total_seconds() / 3600
        
        if hours_ago < 2:
            return 2.0  # Very recent
        elif hours_ago < 6:
            return 1.5
        elif hours_ago < 12:
            return 1.2
        elif hours_ago < 24:
            return 1.0
        else:
            return 0.7  # Older
            
    except:
        return 1.0


def detect_high_impact_event(news_list):
    """
    Detect if there are mentions of high-impact events in the news
    Returns: list of detected events
    """
    events_found = []
    
    for news in news_list:
        text = f"{news['title']} {news.get('description', '')}".lower()
        
        for event in HIGH_IMPACT_EVENTS:
            if event in text:
                events_found.append({
                    "event": event,
                    "source": news["source"],
                    "title": news["title"],
                })
                break
    
    return events_found


# ============================================================================
# MAIN SCORE FUNCTION
# ============================================================================

def calculate_news_score(news_list=None, use_mock=False):
    """
    Calculate the final news sentiment score
    Returns: dict with score, details, events
    """
    # Collect news if not provided
    if news_list is None:
        if use_mock or NEWSAPI_KEY == "YOUR_API_KEY_HERE":
            news_list = get_mock_news()
            print("📰 Using example news (mock data)")
        else:
            news_list = get_recent_news()
    
    if not news_list:
        return {
            "score": 50,
            "interpretation": "NEUTRAL",
            "news_count": 0,
            "details": [],
            "high_impact_events": [],
            "message": "No news found in the last 24h",
        }
    
    # Analyze each news item
    analyzed_news = []
    total_weighted_score = 0
    total_weight = 0
    
    for news in news_list:
        # Combine title and description for analysis
        full_text = f"{news['title']} {news.get('description', '')}"
        
        # Analyze sentiment
        sentiment_score, keywords = analyze_sentiment(full_text)
        
        # Calculate weights
        source_weight = get_source_weight(news["source"])
        recency_weight = get_recency_weight(news["published_at"])
        combined_weight = source_weight * recency_weight
        
        # Accumulate for weighted average
        total_weighted_score += sentiment_score * combined_weight
        total_weight += combined_weight
        
        # Determine individual interpretation
        if sentiment_score >= 65:
            interpretation = "BULLISH"
        elif sentiment_score >= 55:
            interpretation = "SLIGHTLY BULLISH"
        elif sentiment_score <= 35:
            interpretation = "BEARISH"
        elif sentiment_score <= 45:
            interpretation = "SLIGHTLY BEARISH"
        else:
            interpretation = "NEUTRAL"
        
        analyzed_news.append({
            "title": news["title"],
            "source": news["source"],
            "published_at": news["published_at"],
            "score": sentiment_score,
            "interpretation": interpretation,
            "keywords": keywords,
            "weight": round(combined_weight, 2),
        })
    
    # Calculate final score
    final_score = total_weighted_score / total_weight if total_weight > 0 else 50
    final_score = round(final_score, 1)
    
    # Final interpretation
    if final_score >= 70:
        final_interpretation = "🟢 VERY BULLISH"
    elif final_score >= 60:
        final_interpretation = "🟢 BULLISH"
    elif final_score >= 55:
        final_interpretation = "🟡 SLIGHTLY BULLISH"
    elif final_score <= 30:
        final_interpretation = "🔴 VERY BEARISH"
    elif final_score <= 40:
        final_interpretation = "🔴 BEARISH"
    elif final_score <= 45:
        final_interpretation = "🟡 SLIGHTLY BEARISH"
    else:
        final_interpretation = "⚪ NEUTRAL"
    
    # Detect high-impact events
    high_impact = detect_high_impact_event(news_list)
    
    return {
        "score": final_score,
        "interpretation": final_interpretation,
        "news_count": len(news_list),
        "details": analyzed_news,
        "high_impact_events": high_impact,
    }


def get_news_score_cached(force_refresh=False):
    """
    Return the news score using cache to save API requests.
    Cache valid for NEWS_CACHE_MINUTES (default: 45 min = ~32 requests/day)
    
    Args:
        force_refresh: If True, ignores cache and fetches new news
    
    Returns:
        dict with score, interpretation, from_cache, cache_age_minutes
    """
    global _news_cache
    
    now = datetime.now()
    
    # Check if cache is valid
    if not force_refresh and _news_cache["last_update"] is not None:
        cache_age = (now - _news_cache["last_update"]).total_seconds() / 60
        
        if cache_age < NEWS_CACHE_MINUTES:
            # Cache still valid
            return {
                "score": _news_cache["score"],
                "result": _news_cache["result"],
                "from_cache": True,
                "cache_age_minutes": round(cache_age, 1),
                "next_update_minutes": round(NEWS_CACHE_MINUTES - cache_age, 1),
            }
    
    # Cache expired or force_refresh - fetch new news
    print(f"📰 Updating news score (cache expired or forced refresh)...")
    result = calculate_news_score()
    
    # Update cache
    _news_cache["score"] = result["score"]
    _news_cache["result"] = result
    _news_cache["last_update"] = now
    
    # Save history
    save_news_history(result)
    
    return {
        "score": result["score"],
        "result": result,
        "from_cache": False,
        "cache_age_minutes": 0,
        "next_update_minutes": NEWS_CACHE_MINUTES,
    }


def get_hybrid_score():
    """
    Main function to get the hybrid news score.
    Compatible with the trading bot's main.py.
    
    Returns:
        dict with score (0-100) and components
    """
    cached = get_news_score_cached()
    
    result = cached.get("result", {})
    score = cached.get("score", 50)
    
    return {
        "score": score,
        "from_cache": cached.get("from_cache", False),
        "cache_age_minutes": cached.get("cache_age_minutes", 0),
        "interpretation": result.get("interpretation", "neutral"),
        "news_count": result.get("news_count", 0),
        "high_impact_events": result.get("high_impact_events", []),
        "components": {
            "headlines_sentiment": result.get("details", {}).get("avg_sentiment", 0) if isinstance(result.get("details"), dict) else 0,
            "news_count": result.get("news_count", 0),
        }
    }


def get_cache_status():
    """Return current news cache status"""
    if _news_cache["last_update"] is None:
        return {
            "has_cache": False,
            "message": "Cache empty - next call will fetch news",
        }
    
    cache_age = (datetime.now() - _news_cache["last_update"]).total_seconds() / 60
    is_valid = cache_age < NEWS_CACHE_MINUTES
    
    return {
        "has_cache": True,
        "is_valid": is_valid,
        "score": _news_cache["score"],
        "cache_age_minutes": round(cache_age, 1),
        "expires_in_minutes": round(max(0, NEWS_CACHE_MINUTES - cache_age), 1),
        "last_update": _news_cache["last_update"].isoformat(),
    }


# ============================================================================
# HISTORY FUNCTIONS
# ============================================================================

def save_news_history(result):
    """Save result to history"""
    history = load_news_history()
    
    entry = {
        "timestamp": utc_iso(),  # FLO-309: was datetime.now() = local
        "score": result["score"],
        "interpretation": result["interpretation"],
        "news_count": result["news_count"],
        "high_impact_events": len(result["high_impact_events"]),
    }
    
    history.append(entry)
    
    # Keep only last 30 days
    cutoff = datetime.now() - timedelta(days=30)
    history = [h for h in history if datetime.fromisoformat(h["timestamp"]) > cutoff]
    
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NEWS_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)


def load_news_history():
    """Load news history"""
    if os.path.exists(NEWS_HISTORY_FILE):
        with open(NEWS_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return []


# ============================================================================
# DISPLAY FUNCTION
# ============================================================================

def display_news_analysis(result):
    """Display analysis in formatted output"""
    print("\n" + "=" * 70)
    print("📰 NEWS SENTIMENT ANALYSIS - XAU/USD")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total news analyzed: {result['news_count']}")
    
    # High impact events warning
    if result["high_impact_events"]:
        print("\n" + "⚠️ " * 20)
        print("⚠️  HIGH-IMPACT EVENTS DETECTED:")
        for event in result["high_impact_events"][:3]:
            print(f"   • {event['event'].upper()} - {event['source']}")
        print("⚠️  RECOMMENDATION: Do not trade 15min before and 30min after!")
        print("⚠️ " * 20)
    
    # Top news
    print("\n" + "-" * 70)
    print("TOP 5 MOST RECENT NEWS:")
    print("-" * 70)
    
    for i, news in enumerate(result["details"][:5], 1):
        # Format time
        try:
            pub_time = datetime.fromisoformat(news["published_at"].replace('Z', '+00:00'))
            pub_time = pub_time.replace(tzinfo=None)
            hours_ago = (datetime.now() - pub_time).total_seconds() / 3600
            time_str = f"{int(hours_ago)}h ago" if hours_ago < 24 else pub_time.strftime('%d/%m')
        except:
            time_str = "?"
        
        # Emoji based on score
        if news["score"] >= 60:
            emoji = "🟢"
        elif news["score"] <= 40:
            emoji = "🔴"
        else:
            emoji = "⚪"
        
        print(f"\n{i}. [{news['source']} - {time_str}]")
        print(f"   \"{news['title'][:70]}{'...' if len(news['title']) > 70 else ''}\"")
        print(f"   → Sentiment: {news['interpretation']} ({emoji} {news['score']})")
        
        # Show found keywords
        if news["keywords"]["bullish"]:
            bulls = [k[0] for k in news["keywords"]["bullish"][:3]]
            print(f"   → Bullish keywords: {', '.join(bulls)}")
        if news["keywords"]["bearish"]:
            bears = [k[0] for k in news["keywords"]["bearish"][:3]]
            print(f"   → Bearish keywords: {', '.join(bears)}")
    
    # Final score
    print("\n" + "=" * 70)
    print(f"📊 FINAL SCORE: {result['score']}/100")
    print(f"📊 INTERPRETATION: {result['interpretation']}")
    print("=" * 70)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("NEWS SENTIMENT ANALYSIS - XAU/USD Trading Bot")
    print("=" * 70)
    
    # Check API key
    if NEWSAPI_KEY == "YOUR_API_KEY_HERE":
        print("\n⚠️  NewsAPI key not configured!")
        print("   To use real news:")
        print("   1. Go to https://newsapi.org")
        print("   2. Create a free account")
        print("   3. Copy your API key")
        print("   4. Replace YOUR_API_KEY_HERE in the file")
        print("\n   Using MOCK DATA for demonstration...\n")
    
    # Calculate score (uses mock if no API key)
    result = calculate_news_score(use_mock=True)
    
    # Display result
    display_news_analysis(result)
    
    # Save history
    save_news_history(result)
    print(f"\n✅ History saved to: {NEWS_HISTORY_FILE}")


if __name__ == "__main__":
    main()
