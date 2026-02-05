# Advanced market scrapers
from .props_scraper import PlayerPropsScraper
from .alt_lines_scraper import AltLinesScraper
from .futures_scraper import FuturesScraper
from .boost_detector import BoostDetector
from .parlay_analyzer import ParlayCorrelationAnalyzer

__all__ = [
    "PlayerPropsScraper",
    "AltLinesScraper",
    "FuturesScraper",
    "BoostDetector",
    "ParlayCorrelationAnalyzer",
]

