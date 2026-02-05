"""
Stealth browser utilities for undetectable web scraping.
Implements anti-detection measures: user agent rotation, proxy support, jittered delays.
"""
from __future__ import annotations

import random
import time
from typing import List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium_stealth import stealth
from webdriver_manager.chrome import ChromeDriverManager

from shared.schemas import ProxyConfig


# Common user agents for rotation
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]


def get_random_user_agent() -> str:
    """Return a random user agent string."""
    return random.choice(USER_AGENTS)


def jittered_delay(min_seconds: float = 2.0, max_seconds: float = 10.0) -> None:
    """Sleep for a random duration between min and max seconds."""
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


def create_stealth_driver(
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    user_agent: Optional[str] = None,
) -> webdriver.Chrome:
    """
    Create a Chrome WebDriver with stealth settings to avoid detection.
    
    Args:
        headless: Run browser in headless mode
        proxy: Optional proxy configuration
        user_agent: Optional specific user agent (random if not provided)
    
    Returns:
        Configured Chrome WebDriver instance
    """
    options = Options()
    
    # Use provided or random user agent
    ua = user_agent or get_random_user_agent()
    options.add_argument(f"--user-agent={ua}")
    
    # Headless mode
    if headless:
        options.add_argument("--headless=new")
    
    # Anti-detection arguments
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--lang=en-US,en")
    
    # Proxy configuration
    if proxy:
        proxy_str = _build_proxy_string(proxy)
        options.add_argument(f"--proxy-server={proxy_str}")
    
    # Exclude automation switches
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    # Create driver
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    
    # Apply selenium-stealth
    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )
    
    # Set timeouts
    driver.set_page_load_timeout(30)
    driver.implicitly_wait(10)
    
    return driver


def _build_proxy_string(proxy: ProxyConfig) -> str:
    """Build proxy connection string from config."""
    if proxy.username and proxy.password:
        return f"{proxy.protocol}://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
    return f"{proxy.protocol}://{proxy.host}:{proxy.port}"


class ProxyRotator:
    """Manages rotation through a list of proxies."""
    
    def __init__(self, proxies: Optional[List[ProxyConfig]] = None):
        self.proxies = proxies or []
        self._index = 0
        self._failed: set = set()
    
    def get_next(self) -> Optional[ProxyConfig]:
        """Get the next available proxy, skipping failed ones."""
        if not self.proxies:
            return None
        
        available = [p for i, p in enumerate(self.proxies) if i not in self._failed]
        if not available:
            # Reset failed list if all proxies have failed
            self._failed.clear()
            available = self.proxies
        
        proxy = available[self._index % len(available)]
        self._index += 1
        return proxy
    
    def mark_failed(self, proxy: ProxyConfig) -> None:
        """Mark a proxy as failed."""
        for i, p in enumerate(self.proxies):
            if p.host == proxy.host and p.port == proxy.port:
                self._failed.add(i)
                break
    
    def reset(self) -> None:
        """Reset the failed proxy list."""
        self._failed.clear()
        self._index = 0

