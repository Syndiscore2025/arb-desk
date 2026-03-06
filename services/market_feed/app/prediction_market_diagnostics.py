"""Helpers for lightweight prediction-market diagnostics."""

from __future__ import annotations

import re
from typing import Dict, List


KEYWORD_FALSE_POSITIVE_PHRASES: Dict[str, List[str]] = {
    "hail": ["hail mary"],
}


TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "politics": [
        "trump", "president", "election", "senate", "congress", "governor", "biden",
        "republican", "democrat", "vote", "impeach", "cabinet", "veto", "pardon",
    ],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "dogecoin"],
    "sports": [
        "nba", "nfl", "mlb", "nhl", "ncaa", "soccer", "football", "basketball",
        "baseball", "hockey", "tennis", "ufc", "mma", "boxing", "esports",
    ],
    "economics": [
        "tariff", "inflation", "gdp", "recession", "fed ", "interest rate",
        "unemployment", "jobs", "revenue", "deficit", "debt",
    ],
    "entertainment": [
        "oscar", "emmy", "grammy", "album", "movie", "gta", "game",
        "rihanna", "taylor", "drake", "kanye", "film",
    ],
    "world_events": ["ukraine", "russia", "china", "taiwan", "war", "ceasefire", "nato", "iran", "israel", "gaza"],
    "weather": [
        "temperature", "hurricane", "tornado", "earthquake", "weather", "climate",
        "wildfire", "flood", "rain", "rainfall", "snow", "snowfall", "blizzard",
        "precipitation", "hail", "heatwave", "tropical storm", "winter storm",
        "storm surge", "wind speed",
    ],
    "science": ["spacex", "nasa", "mars", "moon", "fda", "vaccine", "covid", "ai ", "artificial intelligence"],
}


def _keyword_matches_title(title_lower: str, keyword: str) -> bool:
    keyword_lower = keyword.lower()
    if " " in keyword_lower:
        return keyword_lower in title_lower
    if re.search(rf"(^|[^a-z0-9]){re.escape(keyword_lower)}([^a-z0-9]|$)", title_lower) is None:
        return False
    return not any(
        phrase in title_lower
        for phrase in KEYWORD_FALSE_POSITIVE_PHRASES.get(keyword_lower, [])
    )


def categorize_prediction_market_titles(titles: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for title in titles:
        title_lower = title.lower()
        matched_topic = False
        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(_keyword_matches_title(title_lower, keyword) for keyword in keywords):
                counts[topic] = counts.get(topic, 0) + 1
                matched_topic = True
        if not matched_topic:
            counts["other"] = counts.get("other", 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))