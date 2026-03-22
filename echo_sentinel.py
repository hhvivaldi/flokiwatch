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
import os
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
    age_hours: float = 0.0


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
    """Remove hashes older than 24 hours (dedup window for seen headlines)."""
    cutoff = time.time() - 86400  # 24 hours
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
    """Estimate cost for MiMo-V2-Flash ($0.10/M input, $0.30/M output)."""
    return (input_tokens * 0.10 + output_tokens * 0.30) / 1_000_000


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

    api_key = getattr(config, "ECHO_API_KEY", "") or os.environ.get("LUNA_API_KEY", "")
    if not api_key:
        log.warning("[ECHO] No ECHO_API_KEY / LUNA_API_KEY configured")
        return None

    base_url = getattr(config, "ECHO_API_BASE", "https://api.xiaomimimo.com/v1")

    # Cost cap check
    cost_data = _load_daily_cost()
    cap = getattr(config, "ECHO_DAILY_COST_CAP", 1.00)
    if cost_data["total_usd"] >= cap:
        log.warning(f"[ECHO] Daily cost cap reached (${cost_data['total_usd']:.2f} >= ${cap:.2f})")
        return None

    model = getattr(config, "ECHO_MODEL", "mimo-v2-flash")

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
        client = OpenAI(api_key=api_key, base_url=base_url)
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


# FLO-73: Story clustering
STOPWORDS = frozenset(
    "the a an is are in on at to for of and or but with from by as it its "
    "that this was were has have had will be been not no all any more most "
    "than they them their there these those who what when where which how "
    "could would should may might can do does did than also just after before "
    "about over into new says said year years so very even still some other".split()
)


def _extract_keywords(title: str) -> set:
    """Extract significant keywords from a headline for clustering."""
    words = set()
    for w in str(title).lower().split():
        # Strip punctuation
        w = w.strip(".,;:!?\"'()-[]{}#@&*")
        if len(w) >= 4 and w not in STOPWORDS:
            words.add(w)
    return words


def _keyword_overlap(kw_a: set, kw_b: set) -> float:
    """Calculate keyword overlap ratio. 0.0 = no match, 1.0 = identical."""
    if not kw_a or not kw_b:
        return 0.0
    shared = len(kw_a & kw_b)
    return shared / min(len(kw_a), len(kw_b))


_CLASSIFICATION_RANK = {"CRITICAL": 3, "IMPORTANT": 2, "ROUTINE": 1}


def _find_matching_cluster(new_keywords: set, alerts: list, max_age_hours: float = 4.0) -> Optional[int]:
    """Find an existing alert that matches the new headline's keywords (>50% overlap)."""
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=max_age_hours)

    for i in range(len(alerts) - 1, -1, -1):  # Search newest first
        a = alerts[i]
        try:
            ts = datetime.fromisoformat(str(a.get("first_seen") or a.get("timestamp", "")).replace("Z", "+00:00"))
            if ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            if ts < cutoff:
                continue
        except Exception:
            continue

        existing_kw = _extract_keywords(a.get("representative_headline") or a.get("title", ""))
        if _keyword_overlap(new_keywords, existing_kw) >= 0.4:
            return i

    return None


def store_alert(headline: ClassifiedHeadline) -> None:
    """Append an IMPORTANT or CRITICAL alert to echo_alerts.json, with story clustering (FLO-73)."""
    alerts = _load_alerts()

    new_kw = _extract_keywords(headline.title)
    match_idx = _find_matching_cluster(new_kw, alerts)

    if match_idx is not None:
        # Update existing cluster
        cluster = alerts[match_idx]
        cluster["headline_count"] = cluster.get("headline_count", 1) + 1
        cluster["latest"] = datetime.utcnow().isoformat()

        # Add source if new
        sources = cluster.get("sources", [])
        if headline.source and headline.source not in sources:
            sources.append(headline.source)
        cluster["sources"] = sources[-10:]  # Cap at 10

        # Upgrade classification if higher
        new_rank = _CLASSIFICATION_RANK.get(headline.classification, 0)
        old_rank = _CLASSIFICATION_RANK.get(cluster.get("classification", "ROUTINE"), 0)
        if new_rank > old_rank:
            cluster["classification"] = headline.classification

        # Keep gold_impact from highest-ranked headline
        if new_rank >= old_rank:
            cluster["gold_impact"] = headline.gold_impact
            cluster["summary"] = headline.summary

        cluster["read"] = False  # Mark unread again

        log.info(
            f"[ECHO] Clustered into existing story: {cluster.get('representative_headline', '')[:60]} "
            f"({cluster['headline_count']} headlines, {len(sources)} sources)"
        )
    else:
        # Create new cluster entry
        alerts.append({
            "timestamp": datetime.utcnow().isoformat(),
            "first_seen": datetime.utcnow().isoformat(),
            "latest": datetime.utcnow().isoformat(),
            "title": headline.title,
            "representative_headline": headline.title,
            "headline_count": 1,
            "sources": [headline.source] if headline.source else [],
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


AGGREGATE_FILE = DATA_DIR / "echo_aggregate.json"


def _calculate_sentiment_aggregate() -> Dict[str, Any]:
    """
    Calculate aggregate sentiment from echo_alerts.json across 1h and 4h windows.
    Returns aggregate dict. Never raises.
    """
    try:
        alerts = _load_alerts()
        now = datetime.utcnow()

        def _count_window(max_age_hours: float) -> Dict[str, Any]:
            cutoff = now - timedelta(hours=max_age_hours)
            bullish = 0
            bearish = 0
            neutral = 0
            for a in alerts:
                try:
                    ts = datetime.fromisoformat(str(a.get("timestamp", "")).replace("Z", "+00:00"))
                    if ts.tzinfo:
                        ts = ts.replace(tzinfo=None)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                impact = str(a.get("gold_impact", "")).upper()
                if impact == "BULLISH":
                    bullish += 1
                elif impact == "BEARISH":
                    bearish += 1
                else:
                    neutral += 1

            total = bullish + bearish + neutral
            if total == 0:
                return {"total": 0, "bullish": 0, "bearish": 0, "neutral": 0, "dominant": "NEUTRAL"}

            bull_pct = bullish / total
            bear_pct = bearish / total
            if bull_pct > 0.6:
                dominant = "BULLISH"
            elif bear_pct > 0.6:
                dominant = "BEARISH"
            elif total <= 2:
                dominant = "NEUTRAL"
            else:
                dominant = "MIXED"

            return {
                "total": total,
                "bullish": bullish,
                "bearish": bearish,
                "neutral": neutral,
                "dominant": dominant,
            }

        aggregate = {
            "1h": _count_window(1.0),
            "4h": _count_window(4.0),
            "updated": now.isoformat(),
        }

        try:
            AGGREGATE_FILE.write_text(
                json.dumps(aggregate, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

        return aggregate
    except Exception as e:
        log.warning(f"[ECHO] Sentiment aggregate failed: {e}")
        return {}


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
            age_hours=float(headline.get("age_hours", 0)),
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

    # 6. Record agent events for Trade Room feed (with dedup against recent events)
    try:
        from db_writer import record_agent_event
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path

        # Load recent Echo event titles from DB to prevent duplicates
        _recent_titles = set()
        try:
            _db_path = _Path(__file__).parent / "data" / "history.db"
            if _db_path.exists():
                _conn = _sqlite3.connect(str(_db_path))
                _rows = _conn.execute(
                    "SELECT content FROM agent_events WHERE author='ECHO' AND timestamp > datetime('now', '-24 hours')"
                ).fetchall()
                _conn.close()
                for _r in _rows:
                    # Extract title from content like "CRITICAL: Title. IMPACT. Summary"
                    _txt = (_r[0] or "")
                    _colon = _txt.find(": ")
                    if _colon >= 0:
                        _title_part = _txt[_colon + 2:].split(".")[0].strip().lower()[:50]
                    else:
                        _title_part = _txt.lower()[:50]
                    _recent_titles.add(_title_part)
        except Exception:
            pass

        # FLO-73: Read back cluster counts for event payloads
        _alerts_for_count = _load_alerts()
        def _cluster_count_for(title: str) -> int:
            kw = _extract_keywords(title)
            for a in reversed(_alerts_for_count):
                a_kw = _extract_keywords(a.get("representative_headline") or a.get("title", ""))
                if _keyword_overlap(kw, a_kw) >= 0.4:
                    return a.get("headline_count", 1)
            return 1

        for c in critical_alerts:
            if c.title.lower()[:50] not in _recent_titles:
                pl = asdict(c)
                hc = _cluster_count_for(c.title)
                pl["headline_count"] = hc
                suffix = f" ({hc} sources)" if hc > 1 else ""
                record_agent_event(
                    event_type="ECHO_CRITICAL",
                    content=f"CRITICAL: {c.title}{suffix}. {c.gold_impact}. {c.summary}",
                    payload=pl,
                    author="ECHO",
                )
                _recent_titles.add(c.title.lower()[:50])
            else:
                log.info(f"[ECHO] Skipped duplicate event: {c.title[:50]}")
        for imp in important_alerts:
            if imp.title.lower()[:50] not in _recent_titles:
                pl = asdict(imp)
                hc = _cluster_count_for(imp.title)
                pl["headline_count"] = hc
                suffix = f" ({hc} sources)" if hc > 1 else ""
                record_agent_event(
                    event_type="ECHO_IMPORTANT",
                    content=f"IMPORTANT: {imp.title}{suffix}. {imp.gold_impact}. {imp.summary}",
                    payload=pl,
                    author="ECHO",
                )
                _recent_titles.add(imp.title.lower()[:50])
            else:
                log.info(f"[ECHO] Skipped duplicate event: {imp.title[:50]}")
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

    # FLO-72: Calculate sentiment aggregate after every scan
    try:
        _calculate_sentiment_aggregate()
    except Exception:
        pass

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
