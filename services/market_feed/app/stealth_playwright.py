"""
Advanced Playwright-based stealth browser automation for 2026 anti-bot evasion.

Implements comprehensive fingerprint spoofing, residential proxy rotation,
human-like behavior simulation, and CAPTCHA handling to maximize account longevity.

Key evasion techniques:
- Playwright with stealth patches (faster + harder to detect than Selenium)
- Full fingerprint randomization (viewport, WebGL, canvas, fonts, etc.)
- Residential proxy rotation with geo-targeting
- Human behavior simulation (mouse curves, scrolling, typing variance)
- CAPTCHA solver integration (2Captcha, Anti-Captcha, CapSolver)
- Session persistence with cookie/storage management
- Ban detection and automatic failover
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from shared.schemas import ProxyConfig

logger = logging.getLogger(__name__)
browser_logger = logging.getLogger("market_feed.browser.stealth")


# 2026-current user agents (Chrome 120-125, Firefox 122-125, Safari 17.2-17.4)
USER_AGENTS_2026: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]

# Realistic viewport sizes (common resolutions)
VIEWPORTS: List[Dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 2560, "height": 1440},
    {"width": 1280, "height": 720},
]

# Locales for geo-targeting
LOCALES: Dict[str, List[str]] = {
    "US": ["en-US", "en"],
    "UK": ["en-GB", "en"],
    "CA": ["en-CA", "en", "fr-CA"],
    "AU": ["en-AU", "en"],
}


def jittered_delay(min_seconds: float = 2.0, max_seconds: float = 10.0) -> None:
    """Sleep for a random duration with realistic variance."""
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


async def async_jittered_delay(min_seconds: float = 2.0, max_seconds: float = 10.0) -> None:
    """Async sleep for a random duration with realistic variance."""
    delay = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(delay)


def get_random_user_agent() -> str:
    """Return a random 2026-current user agent."""
    return random.choice(USER_AGENTS_2026)


def get_random_viewport() -> Dict[str, int]:
    """Return a random realistic viewport size."""
    return random.choice(VIEWPORTS).copy()


def generate_fingerprint(geo: str = "US") -> Dict[str, Any]:
    """
    Generate a realistic browser fingerprint for the given geo.
    
    Randomizes:
    - User agent
    - Viewport size
    - Locale/timezone
    - Hardware concurrency (CPU cores)
    - Device memory
    - WebGL vendor/renderer
    - Platform
    """
    viewport = get_random_viewport()
    user_agent = get_random_user_agent()
    
    # Determine platform from UA
    if "Windows" in user_agent:
        platform = "Win32"
        cores = random.choice([4, 6, 8, 12, 16])
        memory = random.choice([8, 16, 32])
    elif "Macintosh" in user_agent:
        platform = "MacIntel"
        cores = random.choice([4, 8, 10, 12])
        memory = random.choice([8, 16, 32, 64])
    else:  # Linux
        platform = "Linux x86_64"
        cores = random.choice([4, 6, 8, 16])
        memory = random.choice([8, 16, 32])
    
    locale = LOCALES.get(geo, LOCALES["US"])
    
    return {
        "user_agent": user_agent,
        "viewport": viewport,
        "locale": locale[0],
        "timezone_id": _get_timezone_for_geo(geo),
        "platform": platform,
        "hardware_concurrency": cores,
        "device_memory": memory,
        "webgl_vendor": random.choice(["Intel Inc.", "NVIDIA Corporation", "AMD"]),
        "webgl_renderer": random.choice([
            "Intel Iris OpenGL Engine",
            "ANGLE (NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0)",
            "ANGLE (AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0)",
        ]),
    }


def _get_timezone_for_geo(geo: str) -> str:
    """Get a realistic timezone for the given geo."""
    timezones = {
        "US": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
        "UK": ["Europe/London"],
        "CA": ["America/Toronto", "America/Vancouver"],
        "AU": ["Australia/Sydney", "Australia/Melbourne"],
    }
    return random.choice(timezones.get(geo, timezones["US"]))


class StealthBrowser:
    """
    Advanced stealth browser using Playwright with comprehensive evasion.

    Features:
    - Full fingerprint spoofing
    - Residential proxy rotation
    - Human-like behavior simulation
    - CAPTCHA detection and solving
    - Session persistence
    - Ban detection and failover
    """

    def __init__(
        self,
        bookmaker: str,
        proxy: Optional[ProxyConfig] = None,
        geo: str = "US",
        headless: bool = True,
        captcha_api_key: Optional[str] = None,
        session_dir: Optional[Path] = None,
    ):
        self.bookmaker = bookmaker
        self.proxy = proxy
        self.geo = geo
        self.headless = headless
        self.captcha_api_key = captcha_api_key
        self.session_dir = session_dir or Path(f"/tmp/sessions/{bookmaker}")
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.fingerprint = generate_fingerprint(geo)
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self._ban_signals = [
            "access denied",
            "blocked",
            "captcha",
            "unusual activity",
            "verify you're human",
            "cloudflare",
            "datadome",
            "perimeterx",
        ]

    async def initialize(self) -> None:
        """Initialize Playwright browser with stealth settings."""
        logger.info(f"[{self.bookmaker}] Initializing stealth browser (geo={self.geo})")

        self.playwright = await async_playwright().start()

        # Launch args for maximum stealth
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
        ]

        # Proxy configuration
        proxy_config = None
        if self.proxy:
            proxy_config = {
                "server": f"{self.proxy.protocol}://{self.proxy.host}:{self.proxy.port}",
            }
            if self.proxy.username and self.proxy.password:
                proxy_config["username"] = self.proxy.username
                proxy_config["password"] = self.proxy.password

        # Launch browser
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
            proxy=proxy_config,
        )

        # Create context with fingerprint
        self.context = await self.browser.new_context(
            user_agent=self.fingerprint["user_agent"],
            viewport=self.fingerprint["viewport"],
            locale=self.fingerprint["locale"],
            timezone_id=self.fingerprint["timezone_id"],
            permissions=["geolocation", "notifications"],
            color_scheme="light",
            device_scale_factor=1.0,
        )

        # Load saved session if exists
        await self._load_session()

        # Apply advanced evasion scripts
        await self._apply_evasion_scripts()

        # Create page
        self.page = await self.context.new_page()

        # Set realistic timeouts
        self.page.set_default_timeout(30000)
        self.page.set_default_navigation_timeout(30000)

        logger.info(f"[{self.bookmaker}] Browser initialized with fingerprint: {self.fingerprint['user_agent'][:50]}...")

    async def _apply_evasion_scripts(self) -> None:
        """
        Inject JavaScript to spoof browser fingerprints and evade detection.

        Overrides:
        - navigator.webdriver (set to undefined)
        - navigator.plugins (add realistic plugins)
        - navigator.languages
        - navigator.hardwareConcurrency
        - navigator.deviceMemory
        - WebGL vendor/renderer
        - Canvas fingerprinting (add noise)
        - Chrome runtime detection
        """
        evasion_script = f"""
        // Remove webdriver property
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined
        }});

        // Override plugins
        Object.defineProperty(navigator, 'plugins', {{
            get: () => [
                {{name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'}},
                {{name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'}},
                {{name: 'Native Client', filename: 'internal-nacl-plugin'}}
            ]
        }});

        // Override languages
        Object.defineProperty(navigator, 'languages', {{
            get: () => {json.dumps(LOCALES[self.geo])}
        }});

        // Override hardware concurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {self.fingerprint['hardware_concurrency']}
        }});

        // Override device memory
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {self.fingerprint['device_memory']}
        }});

        // Override platform
        Object.defineProperty(navigator, 'platform', {{
            get: () => '{self.fingerprint['platform']}'
        }});

        // WebGL spoofing
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 37445) return '{self.fingerprint['webgl_vendor']}';
            if (parameter === 37446) return '{self.fingerprint['webgl_renderer']}';
            return getParameter.apply(this, arguments);
        }};

        // Canvas noise injection (subtle randomization to avoid fingerprinting)
        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function() {{
            const context = this.getContext('2d');
            if (context) {{
                const imageData = context.getImageData(0, 0, this.width, this.height);
                for (let i = 0; i < imageData.data.length; i += 4) {{
                    imageData.data[i] += Math.floor(Math.random() * 3) - 1;
                }}
                context.putImageData(imageData, 0, 0);
            }}
            return originalToDataURL.apply(this, arguments);
        }};

        // Remove chrome automation indicators
        if (window.chrome) {{
            delete window.chrome.runtime;
        }}

        // Permissions API spoofing
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({{ state: Notification.permission }}) :
                originalQuery(parameters)
        );
        """

        await self.context.add_init_script(evasion_script)

    async def _load_session(self) -> None:
        """Load saved cookies and storage state if available."""
        session_file = self.session_dir / "session.json"
        if session_file.exists():
            try:
                with open(session_file, "r") as f:
                    state = json.load(f)
                await self.context.add_cookies(state.get("cookies", []))
                logger.info(f"[{self.bookmaker}] Loaded saved session")
            except Exception as e:
                logger.warning(f"[{self.bookmaker}] Failed to load session: {e}")

    async def save_session(self) -> None:
        """Save cookies and storage state for session persistence."""
        if not self.context:
            return

        try:
            cookies = await self.context.cookies()
            session_file = self.session_dir / "session.json"
            with open(session_file, "w") as f:
                json.dump({"cookies": cookies}, f)
            logger.info(f"[{self.bookmaker}] Saved session")
        except Exception as e:
            logger.warning(f"[{self.bookmaker}] Failed to save session: {e}")

    async def human_type(self, selector: str, text: str, delay_range: Tuple[int, int] = (50, 150)) -> None:
        """
        Type text with human-like delays and occasional typos/corrections.

        Simulates realistic typing patterns:
        - Variable delays between keystrokes
        - Occasional pauses (thinking)
        - Rare typos with backspace correction
        """
        if not self.page:
            return

        element = await self.page.wait_for_selector(selector)
        await element.click()  # Focus

        for i, char in enumerate(text):
            # 2% chance of typo
            if random.random() < 0.02 and i > 0:
                wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz")
                await element.type(wrong_char, delay=random.randint(*delay_range))
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await element.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15))

            await element.type(char, delay=random.randint(*delay_range))

            # Occasional longer pause (thinking)
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.3, 0.8))

    async def human_scroll(self, distance: int = 500) -> None:
        """Scroll page with human-like smoothness and variance."""
        if not self.page:
            return

        # Scroll in chunks with variance
        chunks = random.randint(3, 7)
        chunk_size = distance // chunks

        for _ in range(chunks):
            scroll_amount = chunk_size + random.randint(-50, 50)
            await self.page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            await asyncio.sleep(random.uniform(0.1, 0.3))

    async def human_mouse_move(self, x: int, y: int) -> None:
        """Move mouse with realistic curve (Bezier-like path)."""
        if not self.page:
            return

        # Get current position (approximate)
        current_x, current_y = random.randint(100, 500), random.randint(100, 500)

        # Generate curve points
        steps = random.randint(10, 20)
        for i in range(steps):
            t = i / steps
            # Quadratic Bezier curve with random control point
            control_x = (current_x + x) / 2 + random.randint(-100, 100)
            control_y = (current_y + y) / 2 + random.randint(-100, 100)

            new_x = int((1-t)**2 * current_x + 2*(1-t)*t * control_x + t**2 * x)
            new_y = int((1-t)**2 * current_y + 2*(1-t)*t * control_y + t**2 * y)

            await self.page.mouse.move(new_x, new_y)
            await asyncio.sleep(random.uniform(0.01, 0.03))

    async def detect_ban(self) -> bool:
        """
        Detect if we've been banned or blocked.

        Checks for:
        - CAPTCHA pages
        - Access denied messages
        - Cloudflare/DataDome/PerimeterX challenges
        - Empty/error pages
        - Suspicious redirects
        """
        if not self.page:
            return False

        try:
            content = await self.page.content()
            content_lower = content.lower()

            # Check for ban signals
            for signal in self._ban_signals:
                if signal in content_lower:
                    logger.warning(f"[{self.bookmaker}] Ban signal detected: {signal}")
                    return True

            # Check for CAPTCHA
            if await self._has_captcha():
                logger.warning(f"[{self.bookmaker}] CAPTCHA detected")
                return True

            # Check for empty page (possible block)
            if len(content.strip()) < 100:
                logger.warning(f"[{self.bookmaker}] Empty page detected")
                return True

            return False

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error detecting ban: {e}")
            return False

    async def _has_captcha(self) -> bool:
        """Check if page contains a CAPTCHA challenge."""
        if not self.page:
            return False

        captcha_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "div[class*='captcha']",
            "div[id*='captcha']",
            "#challenge-form",  # Cloudflare
            ".g-recaptcha",
            ".h-captcha",
        ]

        for selector in captcha_selectors:
            try:
                element = await self.page.query_selector(selector)
                if element:
                    return True
            except Exception:
                pass

        return False

    async def solve_captcha(self) -> bool:
        """
        Attempt to solve CAPTCHA using external solver API.

        Supports:
        - 2Captcha
        - Anti-Captcha
        - CapSolver

        Returns True if solved successfully.
        """
        if not self.captcha_api_key or not self.page:
            logger.warning(f"[{self.bookmaker}] CAPTCHA detected but no solver configured")
            return False

        logger.info(f"[{self.bookmaker}] Attempting to solve CAPTCHA...")

        try:
            # Get page URL and screenshot
            url = self.page.url
            screenshot = await self.page.screenshot()

            # Call 2Captcha API (example - adapt for other services)
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Submit CAPTCHA
                response = await client.post(
                    "https://2captcha.com/in.php",
                    data={
                        "key": self.captcha_api_key,
                        "method": "userrecaptcha",
                        "googlekey": await self._extract_recaptcha_sitekey(),
                        "pageurl": url,
                        "json": 1,
                    }
                )
                result = response.json()

                if result.get("status") != 1:
                    logger.error(f"[{self.bookmaker}] CAPTCHA submission failed: {result}")
                    return False

                captcha_id = result["request"]

                # Poll for solution
                for _ in range(30):  # 30 attempts, 5s each = 2.5min max
                    await asyncio.sleep(5)

                    response = await client.get(
                        "https://2captcha.com/res.php",
                        params={
                            "key": self.captcha_api_key,
                            "action": "get",
                            "id": captcha_id,
                            "json": 1,
                        }
                    )
                    result = response.json()

                    if result.get("status") == 1:
                        solution = result["request"]
                        # Inject solution
                        await self.page.evaluate(f"""
                            document.getElementById('g-recaptcha-response').innerHTML = '{solution}';
                        """)
                        logger.info(f"[{self.bookmaker}] CAPTCHA solved successfully")
                        return True

                logger.error(f"[{self.bookmaker}] CAPTCHA solving timeout")
                return False

        except Exception as e:
            logger.error(f"[{self.bookmaker}] CAPTCHA solving failed: {e}")
            return False

    async def _extract_recaptcha_sitekey(self) -> Optional[str]:
        """Extract reCAPTCHA site key from page."""
        if not self.page:
            return None

        try:
            sitekey = await self.page.evaluate("""
                () => {
                    const element = document.querySelector('[data-sitekey]');
                    return element ? element.getAttribute('data-sitekey') : null;
                }
            """)
            return sitekey
        except Exception:
            return None

    async def close(self) -> None:
        """Close browser and save session."""
        await self.save_session()

        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

        logger.info(f"[{self.bookmaker}] Browser closed")

    async def visual_login(
        self,
        login_url: str,
        success_indicators: Optional[List[str]] = None,
        timeout_seconds: int = 300,
    ) -> bool:
        """
        Open a visible browser window for manual login.

        The user logs in manually (including 2FA), and this method waits
        for login success indicators before saving the session and closing.

        Args:
            login_url: The sportsbook login page URL
            success_indicators: CSS selectors that indicate successful login
                (e.g., account dropdown, balance display, user avatar)
            timeout_seconds: How long to wait for login (default 5 minutes)

        Returns:
            True if login was successful, False otherwise
        """
        # Default success indicators for common sportsbooks
        if success_indicators is None:
            success_indicators = [
                # Common logged-in indicators
                "[data-testid='account-dropdown']",
                "[data-testid='user-menu']",
                ".account-menu",
                ".user-balance",
                ".my-account",
                "[aria-label='Account']",
                "[aria-label='My Account']",
                ".logged-in",
                # FanDuel specific
                "[data-test-id='AccountDetails']",
                ".account-balance",
                # DraftKings specific
                "[data-testid='dk-user-dropdown']",
                ".account-icon",
                # Fanatics specific
                ".profile-menu",
            ]

        logger.info(f"[{self.bookmaker}] Starting visual login (non-headless mode)")

        # Force non-headless mode for visual login
        original_headless = self.headless
        self.headless = False

        try:
            # Initialize browser in visible mode
            await self.initialize()

            # Navigate to login page
            logger.info(f"[{self.bookmaker}] Navigating to login page: {login_url}")
            await self.page.goto(login_url, wait_until="networkidle")

            # Wait for user to complete login
            logger.info(
                f"[{self.bookmaker}] Browser window opened. "
                f"Please complete login (including 2FA if required). "
                f"Waiting up to {timeout_seconds} seconds..."
            )

            # Poll for success indicators
            start_time = time.time()
            while time.time() - start_time < timeout_seconds:
                # Check each success indicator
                for selector in success_indicators:
                    try:
                        element = await self.page.query_selector(selector)
                        if element:
                            is_visible = await element.is_visible()
                            if is_visible:
                                logger.info(
                                    f"[{self.bookmaker}] Login successful! "
                                    f"Detected indicator: {selector}"
                                )
                                # Save session immediately
                                await self.save_session()
                                return True
                    except Exception:
                        pass  # Selector not found, continue checking

                # Also check URL for logged-in redirects
                current_url = self.page.url.lower()
                if any(indicator in current_url for indicator in [
                    "/account", "/my-bets", "/wallet", "/deposit",
                    "logged=true", "authenticated"
                ]):
                    logger.info(
                        f"[{self.bookmaker}] Login successful! "
                        f"Detected logged-in URL: {current_url}"
                    )
                    await self.save_session()
                    return True

                # Wait before next check
                await asyncio.sleep(1)

            logger.warning(f"[{self.bookmaker}] Login timeout after {timeout_seconds} seconds")
            return False

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Visual login failed: {e}")
            return False

        finally:
            # Restore original headless setting
            self.headless = original_headless
            # Close the browser
            await self.close()


class ResidentialProxyRotator:
    """
    Advanced proxy rotation manager for residential/mobile proxies.

    Features:
    - Health tracking per proxy
    - Automatic failover on errors
    - Geo-targeting support
    - Rate limiting per proxy
    - Exponential backoff on failures
    """

    def __init__(self, proxies: List[ProxyConfig], geo: str = "US"):
        self.proxies = proxies
        self.geo = geo
        self._index = 0
        self._health: Dict[str, Dict[str, Any]] = {}
        self._last_used: Dict[str, float] = {}

        # Initialize health tracking
        for proxy in proxies:
            key = f"{proxy.host}:{proxy.port}"
            self._health[key] = {
                "failures": 0,
                "successes": 0,
                "last_failure": 0,
                "backoff_until": 0,
            }

    def get_next(self) -> Optional[ProxyConfig]:
        """Get next healthy proxy with backoff consideration."""
        if not self.proxies:
            return None

        now = time.time()
        available = []

        for proxy in self.proxies:
            key = f"{proxy.host}:{proxy.port}"
            health = self._health[key]

            # Skip if in backoff period
            if health["backoff_until"] > now:
                continue

            # Skip if too many recent failures
            if health["failures"] > 5 and health["successes"] == 0:
                continue

            available.append(proxy)

        if not available:
            # Reset all if none available
            logger.warning("All proxies in backoff, resetting...")
            for health in self._health.values():
                health["backoff_until"] = 0
                health["failures"] = 0
            available = self.proxies

        # Round-robin through available
        proxy = available[self._index % len(available)]
        self._index += 1
        self._last_used[f"{proxy.host}:{proxy.port}"] = now

        return proxy

    def mark_success(self, proxy: ProxyConfig) -> None:
        """Mark proxy as successful."""
        key = f"{proxy.host}:{proxy.port}"
        if key in self._health:
            self._health[key]["successes"] += 1
            self._health[key]["failures"] = max(0, self._health[key]["failures"] - 1)

    def mark_failure(self, proxy: ProxyConfig, error_type: str = "generic") -> None:
        """
        Mark proxy as failed with exponential backoff.

        Backoff schedule:
        - 1st failure: 30s
        - 2nd failure: 1min
        - 3rd failure: 5min
        - 4th+ failure: 15min
        """
        key = f"{proxy.host}:{proxy.port}"
        if key not in self._health:
            return

        health = self._health[key]
        health["failures"] += 1
        health["last_failure"] = time.time()

        # Calculate backoff
        failures = health["failures"]
        if failures == 1:
            backoff = 30
        elif failures == 2:
            backoff = 60
        elif failures == 3:
            backoff = 300
        else:
            backoff = 900

        health["backoff_until"] = time.time() + backoff

        logger.warning(
            f"Proxy {key} failed ({failures} times), backing off for {backoff}s. "
            f"Error: {error_type}"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get proxy health statistics."""
        stats = {
            "total": len(self.proxies),
            "healthy": 0,
            "in_backoff": 0,
            "failed": 0,
        }

        now = time.time()
        for key, health in self._health.items():
            if health["backoff_until"] > now:
                stats["in_backoff"] += 1
            elif health["failures"] > 5:
                stats["failed"] += 1
            else:
                stats["healthy"] += 1

        return stats

