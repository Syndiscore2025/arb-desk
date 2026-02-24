"""market_feed adapters package.

Important: many adapters depend on optional heavy browser stacks (Selenium,
Playwright, stealth drivers). Unit tests and lightweight environments should
still be able to import the prediction-market adapters without having those
dependencies installed.

So: we eagerly export the lightweight adapters and *guard* imports of
browser-based adapters.
"""

from __future__ import annotations

from typing import List

# Always-available, API-only adapters
from .prediction_markets import (  # noqa: F401
    KalshiAdapter,
    PolymarketAdapter,
    PredictionMarketArbFinder,
    PredictionMarketEventUnifier,
)

# Config-only module (no browser deps)
from .ct_sportsbooks import (  # noqa: F401
    CT_SPORTSBOOK_CONFIGS,
    DRAFTKINGS_CT_CONFIG,
    FANDUEL_CT_CONFIG,
    FANATICS_CT_CONFIG,
    get_all_ct_configs,
    get_ct_config,
)

__all__: List[str] = [
    # Prediction markets
    "PolymarketAdapter",
    "KalshiAdapter",
    "PredictionMarketArbFinder",
    "PredictionMarketEventUnifier",
    # CT configs
    "CT_SPORTSBOOK_CONFIGS",
    "FANDUEL_CT_CONFIG",
    "DRAFTKINGS_CT_CONFIG",
    "FANATICS_CT_CONFIG",
    "get_ct_config",
    "get_all_ct_configs",
]


def _try_export(name: str, fn) -> None:
    """Best-effort optional import/export.

    We swallow ImportError so unit tests can run without browser dependencies.
    """

    try:
        obj = fn()
    except ImportError:
        return
    except Exception:
        # Defensive: don't break package import on unexpected environment issues.
        return
    globals()[name] = obj
    __all__.append(name)


# Optional adapters (browser stacks and extra deps)
_try_export("BaseFeedAdapter", lambda: __import__(__name__ + ".base", fromlist=["BaseFeedAdapter"]).BaseFeedAdapter)
_try_export(
    "GenericSportsbookAdapter",
    lambda: __import__(__name__ + ".generic", fromlist=["GenericSportsbookAdapter"]).GenericSportsbookAdapter,
)
_try_export(
    "PlaywrightFeedAdapter",
    lambda: __import__(__name__ + ".playwright_adapter", fromlist=["PlaywrightFeedAdapter"]).PlaywrightFeedAdapter,
)
_try_export(
    "PlaywrightGenericAdapter",
    lambda: __import__(__name__ + ".playwright_generic", fromlist=["PlaywrightGenericAdapter"]).PlaywrightGenericAdapter,
)
_try_export(
    "PinnacleAdapter",
    lambda: __import__(__name__ + ".pinnacle_adapter", fromlist=["PinnacleAdapter"]).PinnacleAdapter,
)
_try_export(
    "CLVCalculator",
    lambda: __import__(__name__ + ".pinnacle_adapter", fromlist=["CLVCalculator"]).CLVCalculator,
)
_try_export(
    "OddsAPIAdapter",
    lambda: __import__(__name__ + ".odds_api_adapter", fromlist=["OddsAPIAdapter"]).OddsAPIAdapter,
)
_try_export(
    "InterceptingAdapter",
    lambda: __import__(__name__ + ".intercepting_adapter", fromlist=["InterceptingAdapter"]).InterceptingAdapter,
)
_try_export(
    "DraftKingsPublicAPIAdapter",
    lambda: __import__(__name__ + ".draftkings_public_api", fromlist=["DraftKingsPublicAPIAdapter"]).DraftKingsPublicAPIAdapter,
)

