"""
Echo News Sentinel — 24/7 breaking news monitor for gold trading.
Phase 2 Trading Office (FLO-38).

Flow:
  RSS Feeds (25 sources) → Scanner collects every 5 min
      → Keyword Pre-Filter (Python pure, $0)
          → ~70% filtered as ROUTINE (no AI call)
          → ~30% pass to Echo (GPT model)
              → ROUTINE: log and ignore
              → IMPORTANT: store in echo_alerts.json → Floki reads next call
              → CRITICAL: returned for Simba wake (caller handles trigger)
"""

import json
import hashlib
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict

import config
from logger import log

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ============================================================================
# PATHS
# ============================================================================
DATA_DIR = Path(__file__).parent / "data"
ALERTS_FILE = DATA_DIR / "echo_alerts.json"
SEEN_HASHES_FILE = DATA_DIR / "echo_seen_hashes.json"
COST_FILE = DATA_DIR / "echo_daily_cost.json"
STATUS_FILE = DATA_DIR / "echo_status.json"

# ============================================================================
# KEYWORD PRE-FILTER (reuse from news_score_hybrid)
# ============================================================================
try:
    from news_score_hybrid import BULLISH_KEYWORDS, BEARISH_KEYWORDS
except ImportError:
    BULLISH_KEYWORDS = {}
    BEARISH_KEYWORDS = {}

# Merge both dicts (use absolute weight for relevance scoring)
_ALL_KEYWORDS = {}
_ALL_KEYWORDS.update({k: abs(v) for k, v in BULLISH_KEYWORDS.items()})
_ALL_KEYWORDS.update({k: abs(v) for k, v in BEARISH_KEYWORDS.items()})

# High-severity keywords that always pass through to AI (even if total score is low)
CRITICAL_KEYWORDS = {
    "emergency rate", "emergency cut", "circuit breaker", "flash crash",
    "invasion", "nuclear", "missile strike", "pandemic", "martial law",
    "bank run", "etf liquidation", "gold confiscation", "default",
    "black swan", "systemic risk", "contagion", "war declared",
}


@dataclass
class ClassifiedHeadline:
    title: str
    source: str
    classification: str  # CRITICAL, IMPORTANT, ROUTINE
    relevance_score: int  # 0-100
    gold_impact: str  # BULLISH, BEARISH, NEUTRAL
    summary: str
    category: str = ""
    timestamp: str = ""
    method: str = "ai"  # "ai" or "keyword_fallback"


@dataclass
class EchoScanResult:
    scan_time: str
    headlines_scanned: int
    passed_keyword_filter: int
    ai_classified: int
    critical_alerts: List[ClassifiedHeadline] = field(default_factory=list)
    important_alerts: List[ClassifiedHeadline] = field(default_factory=list)
    routine_count: int = 0
    error: Optional[str] = None


# ============================================================================
# DEDUP ENGINE
# ============================================================================

def _fingerprint(title: str) -> str:
    """Hash the first 50 chars (lowercased) as a dedup fingerprint."""
    normalized = title.strip().lower()[:50]
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _load_seen_hashes() -> Dict[str, float]:
    """Load seen hashes with their timestamps."""
    try:
        if SEEN_HASHES_FILE.exists():
            data = json.loads(SEEN_HASHES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_seen_hashes(hashes: Dict[str, float]) -> None:
    try:
        SEEN_HASHES_FILE.write_text(
            json.dumps(hashes, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _prune_expired_hashes(hashes: Dict[str, float]) -> Dict[str, float]:
    """Remove hashes older than ECHO_COOLDOWN_MINUTES."""
    cooldown_sec = getattr(config, "ECHO_COOLDOWN_MINUTES", 30) * 60
    cutoff = time.time() - cooldown_sec
    return {h: ts for h, ts in hashes.items() if ts > cutoff}


# ============================================================================
# COST TRACKER
# ============================================================================

def _load_daily_cost() -> Dict:
    try:
        if COST_FILE.exists():
            data = json.loads(COST_FILE.read_text(encoding="utf-8"))
            if data.get("date") == datetime.utcnow().strftime("%Y-%m-%d"):
                return data
    except Exception:
        pass
    return {"date": datetime.utcnow().strftime("%Y-%m-%d"), "total_usd": 0.0, "calls": 0}


def _save_daily_cost(cost_data: Dict) -> None:
    try:
        COST_FILE.write_text(json.dumps(cost_data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate cost for gpt-4o-mini ($0.15/M input, $0.60/M output)."""
    return (input_tokens * 0.15 + output_tokens * 0.60) / 1_000_000


# ============================================================================
# KEYWORD PRE-FILTER
# ============================================================================

def keyword_score(title: str) -> int:
    """
    Score a headline against gold-relevant keywords.
    Returns absolute relevance weight (0-10+).
    Higher = more relevant to gold trading.
    """
    title_lower = title.lower()
    score = 0
    for keyword, weight in _ALL_KEYWORDS.items():
        if keyword in title_lower:
            score += weight
    return score


def has_critical_keyword(title: str) -> bool:
    """Check if headline contains a high-severity keyword."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in CRITICAL_KEYWORDS)


def passes_keyword_filter(title: str, threshold: int = 2) -> bool:
    """
    Returns True if headline is relevant enough for AI classification.
    ~70% of headlines should be filtered out.
    """
    if has_critical_keyword(title):
        return True
    return keyword_score(title) >= threshold


# ============================================================================
# GPT CLASSIFICATION
# ============================================================================

ECHO_SYSTEM_PROMPT = """You are Echo, a gold trading news classifier. You analyze headlines and classify their impact on XAU/USD gold price.

For each headline, return a JSON object with:
- "classification": "CRITICAL" | "IMPORTANT" | "ROUTINE"
- "relevance_score": 0-100 (how relevant to gold trading)
- "gold_impact": "BULLISH" | "BEARISH" | "NEUTRAL"
- "summary": 1 sentence explaining the gold impact

Classification criteria:
CRITICAL (triggers immediate trading review):
- Central bank emergency actions (unscheduled rate cuts, emergency QE)
- Military escalation (attacks, invasions, nuclear threats)
- Major market crashes (circuit breakers triggered, flash crashes)
- Gold-specific shocks (massive central bank buying/selling, ETF liquidation)
- Sanctions/trade war escalation with major economies

IMPORTANT (stored for next trading review):
- Scheduled economic data surprises (NFP miss, CPI shock, GDP revision)
- DXY significant moves (>0.5% intraday)
- Oil price spikes/crashes (>5%)
- Geopolitical tensions (not immediate military action)
- Fed/ECB/BOJ policy signals or minutes

ROUTINE (ignore):
- Market commentary and opinions
- Stock-specific news with no gold correlation
- Crypto news (unless major crash affecting risk sentiment)
- Regional news with no macro impact
- Rehashed/recycled stories

Return ONLY a JSON object: {"results": [{"classification": ..., "relevance_score": ..., "gold_impact": ..., "summary": ...}]}
One result per headline, in order."""


def _classify_with_ai(headlines: List[Dict]) -> Optional[List[Dict]]:
    """
    Classify headlines using ECHO_MODEL via OpenAI SDK.
    Returns list of classification dicts or None on failure.
    """
    if OpenAI is None:
        log.warning("[ECHO] openai package not installed")
        return None

    api_key = getattr(config, "ECHO_API_KEY", "")
    if not api_key:
        log.warning("[ECHO] No ECHO_API_KEY configured")
        return None

    # Cost cap check
    cost_data = _load_daily_cost()
    cap = getattr(config, "ECHO_DAILY_COST_CAP", 1.00)
    if cost_data["total_usd"] >= cap:
        log.warning(f"[ECHO] Daily cost cap reached (${cost_data['total_usd']:.2f} >= ${cap:.2f})")
        return None

    model = getattr(config, "ECHO_MODEL", "gpt-4o-mini")

    # Build prompt
    headline_lines = []
    for i, h in enumerate(headlines):
        title = h.get("title", "")
        source = h.get("source", "")
        headline_lines.append(f"{i+1}. [{source}] {title}")

    user_prompt = (
        f"Classify these {len(headlines)} headlines for gold trading impact.\n\n"
        + "\n".join(headline_lines)
    )

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ECHO_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
            timeout=20,
        )

        raw = response.choices[0].message.content
        parsed = json.loads(raw)

        results = parsed.get("results", parsed.get("headlines", []))
        if isinstance(parsed, list):
            results = parsed

        # Track cost
        usage = response.usage
        if usage:
            est = _estimate_cost(usage.prompt_tokens, usage.completion_tokens)
            cost_data["total_usd"] = round(cost_data["total_usd"] + est, 4)
            cost_data["calls"] += 1
            _save_daily_cost(cost_data)

        return results

    except Exception as e:
        log.error(f"[ECHO] AI classification failed: {e}")
        return None


def _classify_keyword_fallback(headline: Dict) -> Dict:
    """Fallback classification using keyword scoring when AI is unavailable."""
    title = headline.get("title", "")
    score = keyword_score(title)

    if has_critical_keyword(title) or score >= 8:
        classification = "CRITICAL"
        relevance = min(95, 60 + score * 5)
    elif score >= 4:
        classification = "IMPORTANT"
        relevance = min(80, 40 + score * 5)
    else:
        classification = "ROUTINE"
        relevance = min(50, score * 10)

    # Determine impact direction from keyword weights
    title_lower = title.lower()
    bull = sum(w for k, w in BULLISH_KEYWORDS.items() if k in title_lower)
    bear = sum(abs(w) for k, w in BEARISH_KEYWORDS.items() if k in title_lower)
    if bull > bear:
        impact = "BULLISH"
    elif bear > bull:
        impact = "BEARISH"
    else:
        impact = "NEUTRAL"

    return {
        "classification": classification,
        "relevance_score": relevance,
        "gold_impact": impact,
        "summary": f"Keyword-scored ({score} pts): {title[:80]}",
    }


# ============================================================================
# ALERTS FILE (echo_alerts.json)
# ============================================================================

def _load_alerts() -> List[Dict]:
    try:
        if ALERTS_FILE.exists():
            data = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_alerts(alerts: List[Dict]) -> None:
    try:
        ALERTS_FILE.write_text(
            json.dumps(alerts, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        log.error(f"[ECHO] Failed to save alerts: {e}")


def store_alert(headline: ClassifiedHeadline) -> None:
    """Append an IMPORTANT or CRITICAL alert to echo_alerts.json."""
    alerts = _load_alerts()
    alerts.append({
        "timestamp": datetime.utcnow().isoformat(),
        "title": headline.title,
        "source": headline.source,
        "classification": headline.classification,
        "relevance_score": headline.relevance_score,
        "gold_impact": headline.gold_impact,
        "summary": headline.summary,
        "read": False,
    })
    # Keep last 200 alerts max
    if len(alerts) > 200:
        alerts = alerts[-200:]
    _save_alerts(alerts)


def get_unread_alerts() -> List[Dict]:
    """Get unread alerts for Floki to consume. Marks them as read."""
    alerts = _load_alerts()
    unread = [a for a in alerts if not a.get("read")]
    if unread:
        for a in alerts:
            a["read"] = True
        _save_alerts(alerts)
    return unread


# ============================================================================
# MAIN SCAN FUNCTION
# ============================================================================

def run_echo_scan(
    direct_headlines: Optional[List[Dict]] = None,
    google_headlines: Optional[List[Dict]] = None,
) -> EchoScanResult:
    """
    Run a full Echo scan cycle.

    Args:
        direct_headlines: Pre-fetched direct RSS headlines (or None to fetch).
        google_headlines: Pre-fetched Google News headlines (or None to skip).

    Returns:
        EchoScanResult with classified headlines and stats.
    """
    if not getattr(config, "ECHO_ENABLED", False):
        return EchoScanResult(
            scan_time=datetime.utcnow().isoformat(),
            headlines_scanned=0,
            passed_keyword_filter=0,
            ai_classified=0,
            error="Echo disabled",
        )

    # Check market hours — suppress IMPORTANT when market is closed
    market_open = True
    try:
        from safety_checks import is_market_open
        market_open, market_reason, _ = is_market_open()
        if not market_open:
            log.info(f"[ECHO] Market closed ({market_reason}) — IMPORTANT alerts suppressed, CRITICAL only")
    except Exception:
        pass  # If check fails, assume open (safer)

    scan_time = datetime.utcnow().isoformat()

    # 1. Collect headlines
    all_headlines = []

    if direct_headlines is None:
        try:
            from news_score_hybrid import get_direct_rss_headlines
            direct_headlines = get_direct_rss_headlines()
        except Exception as e:
            log.error(f"[ECHO] Failed to fetch direct feeds: {e}")
            direct_headlines = []

    if google_headlines is None:
        try:
            from news_score_hybrid import get_rss_headlines
            google_max_age = float(getattr(config, "ECHO_MAX_AGE_HOURS_GOOGLE", 12))
            google_headlines = get_rss_headlines(max_age_hours=google_max_age)
        except Exception as e:
            log.error(f"[ECHO] Failed to fetch Google News feeds: {e}")
            google_headlines = []

    all_headlines.extend(direct_headlines or [])
    all_headlines.extend(google_headlines or [])

    total_scanned = len(all_headlines)
    if total_scanned == 0:
        return EchoScanResult(
            scan_time=scan_time,
            headlines_scanned=0,
            passed_keyword_filter=0,
            ai_classified=0,
        )

    # 2. Dedup — skip already-seen headlines
    seen = _load_seen_hashes()
    seen = _prune_expired_hashes(seen)
    now_ts = time.time()

    fresh_headlines = []
    for h in all_headlines:
        fp = _fingerprint(h.get("title", ""))
        if fp not in seen:
            fresh_headlines.append(h)
            seen[fp] = now_ts

    _save_seen_hashes(seen)

    if not fresh_headlines:
        log.info(f"[ECHO] Scanned {total_scanned} headlines, all already seen")
        return EchoScanResult(
            scan_time=scan_time,
            headlines_scanned=total_scanned,
            passed_keyword_filter=0,
            ai_classified=0,
        )

    # 3. Keyword pre-filter
    candidates = [h for h in fresh_headlines if passes_keyword_filter(h.get("title", ""))]
    filtered_out = len(fresh_headlines) - len(candidates)

    log.info(
        f"[ECHO] {total_scanned} scanned, {len(fresh_headlines)} fresh, "
        f"{len(candidates)} pass keyword filter ({filtered_out} filtered)"
    )

    if not candidates:
        return EchoScanResult(
            scan_time=scan_time,
            headlines_scanned=total_scanned,
            passed_keyword_filter=0,
            ai_classified=0,
            routine_count=filtered_out,
        )

    # 4. AI classification (or keyword fallback)
    ai_results = _classify_with_ai(candidates)
    use_fallback = ai_results is None

    critical_alerts = []
    important_alerts = []
    routine_count = filtered_out  # already filtered by keyword
    ai_classified = 0

    for i, headline in enumerate(candidates):
        if use_fallback:
            result = _classify_keyword_fallback(headline)
            method = "keyword_fallback"
        else:
            result = ai_results[i] if i < len(ai_results) else _classify_keyword_fallback(headline)
            method = "ai" if i < len(ai_results) else "keyword_fallback"
            ai_classified += 1

        classified = ClassifiedHeadline(
            title=headline.get("title", ""),
            source=headline.get("source", ""),
            classification=result.get("classification", "ROUTINE"),
            relevance_score=result.get("relevance_score", 0),
            gold_impact=result.get("gold_impact", "NEUTRAL"),
            summary=result.get("summary", ""),
            category=headline.get("category", ""),
            timestamp=headline.get("timestamp", ""),
            method=method,
        )

        if classified.classification == "CRITICAL":
            critical_alerts.append(classified)
            store_alert(classified)
        elif classified.classification == "IMPORTANT":
            if market_open:
                important_alerts.append(classified)
                store_alert(classified)
            else:
                routine_count += 1  # Suppress IMPORTANT when market closed
        else:
            routine_count += 1

    # 5. Log results
    if critical_alerts:
        for c in critical_alerts:
            log.warning(f"[ECHO] CRITICAL: {c.title} | {c.gold_impact} | {c.summary}")
    if important_alerts:
        for imp in important_alerts:
            log.info(f"[ECHO] IMPORTANT: {imp.title} | {imp.gold_impact}")

    # 6. Record agent events for Trade Room feed
    try:
        from db_writer import record_agent_event

        for c in critical_alerts:
            record_agent_event(
                event_type="ECHO_CRITICAL",
                content=f"CRITICAL: {c.title}. {c.gold_impact}. {c.summary}",
                payload=asdict(c),
                author="ECHO",
            )
        for imp in important_alerts:
            record_agent_event(
                event_type="ECHO_IMPORTANT",
                content=f"IMPORTANT: {imp.title}. {imp.gold_impact}. {imp.summary}",
                payload=asdict(imp),
                author="ECHO",
            )
    except Exception as e:
        log.error(f"[ECHO] Failed to record agent events: {e}")

    result = EchoScanResult(
        scan_time=scan_time,
        headlines_scanned=total_scanned,
        passed_keyword_filter=len(candidates),
        ai_classified=ai_classified,
        critical_alerts=critical_alerts,
        important_alerts=important_alerts,
        routine_count=routine_count,
    )

    # Write status on every scan (even when no new alerts)
    try:
        STATUS_FILE.write_text(json.dumps({
            "last_scan_at": scan_time,
            "headlines_scanned": total_scanned,
            "fresh": len(fresh_headlines) if 'fresh_headlines' in dir() else 0,
            "passed_filter": len(candidates),
            "critical": len(critical_alerts),
            "important": len(important_alerts),
            "routine": routine_count,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass

    return result
