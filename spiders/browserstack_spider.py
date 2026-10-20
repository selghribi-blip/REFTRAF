# spiders/browserstack_spider.py
# Slow Click Mode — Human-like behavior with proper timing and multi-strategy
# ad clicking (AdSense / Monetag container / Monetag iframe).
# ==========================================================================
import os
import random
import time
import scrapy
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import NoSuchElementException

from utils.browserstack_client import BrowserStackClient, LocalBrowserClient
from utils.proxy_for_browser import BrowserProxyProvider


class BrowserStackSpider(scrapy.Spider):
    name = 'browserstack_bot'

    custom_settings = {
        'DOWNLOAD_DELAY': 3,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
        'CONCURRENT_REQUESTS': 1,
        'RETRY_TIMES': 1,
        'DOWNLOAD_TIMEOUT': 90,
    }

    BROWSERSTACK_BROWSERS = [
        ('chrome', 'Windows', '11'),
        ('chrome', 'Windows', '10'),
        ('chrome', 'OS X', 'Ventura'),
        ('edge', 'Windows', '11'),
    ]

    # ============================================================
    # SLOW CLICK MODE — tunable via environment variables
    # ============================================================
    USE_MOBILE = True

    # Reading phase (before any click)
    READ_TIME_MIN = 30
    READ_TIME_MAX = 60

    # Click behavior (conservative)
    MAX_AD_CLICKS_PER_SESSION = int(os.getenv('MAX_ADS_PER_SESSION', '2'))
    CLICK_PROBABILITY = 0.6
    TIME_BEFORE_FIRST_CLICK_MIN = 45
    TIME_BEFORE_FIRST_CLICK_MAX = 90
    TIME_BETWEEN_CLICKS_MIN = 10
    TIME_BETWEEN_CLICKS_MAX = 20
    TIME_AFTER_CLICK_MIN = 15
    TIME_AFTER_CLICK_MAX = 30

    # Dwell (total time on page)
    DWELL_TIME_MIN = int(os.getenv('DWELL_TIME_MIN', '180'))
    DWELL_TIME_MAX = int(os.getenv('DWELL_TIME_MAX', '300'))

    # Scrolling
    SCROLL_MIN = 4
    SCROLL_MAX = 8
    SCROLL_DELAY_MIN = 3.0
    SCROLL_DELAY_MAX = 7.0

    # Internal links
    INTERNAL_LINKS_MIN = 1
    INTERNAL_LINKS_MAX = 2

    ADS_WAIT_MAX = 40

    def __init__(self, target_url=None, use_browserstack='true', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_url = (target_url or '').strip() or 'https://www.forjo.tech/'
        self.use_browserstack = str(use_browserstack).lower() == 'true'
        self.client = None
        self.ads_clicked = 0
        self.forms_filled = 0
        self.proxy_used = None
        self.dwell_time = 0
        self.scroll_count = 0
        self.internal_clicks = 0
        self.captcha_detected = False
        self.ads_detected_in_dom = False
        self.ads_visible_counts = {}
        self.click_attempts = 0
        self.reading_time = 0
        self._proxy_provider = None
        self._tried_adsense = set()
        self._tried_containers = set()
        self._tried_iframes = set()
        self.clicks_by_strategy = Counter()
        self.proxy_bypassed = False
        self.form_payload = None

    def start_requests(self):
        if not self.target_url.startswith(('http://', 'https://')):
            self.target_url = 'https://' + self.target_url

        self.logger.info(f"Starting SLOW CLICK bot for: {self.target_url}")
        self.logger.info(f"   Mode: {'BrowserStack' if self.use_browserstack else 'Local'}")
        self.logger.info(f"   Mobile: {self.USE_MOBILE}")
        self.logger.info(f"   Max clicks: {self.MAX_AD_CLICKS_PER_SESSION}")
        self.logger.info(f"   Dwell: {self.DWELL_TIME_MIN}-{self.DWELL_TIME_MAX}s")

        if self.use_browserstack:
            user = os.environ.get('BROWSERSTACK_USERNAME', '').strip()
            key = os.environ.get('BROWSERSTACK_ACCESS_KEY', '').strip()
            if not (user and key):
                self.logger.warning("BrowserStack credentials missing - falling back to LOCAL")
                self.use_browserstack = False

        yield scrapy.Request(
            url=self.target_url,
            callback=self.parse_with_browser,
            errback=self.handle_error,
            dont_filter=True,
            meta={'handle_httpstatus_all': True},
        )

    # ============================================================
    # Main flow
    # ============================================================
    def parse_with_browser(self, response):
        status = 'unknown'
        error_msg = None

        try:
            proxy = self._pick_proxy()
            self.proxy_used = proxy

            if self.use_browserstack:
                driver = self._start_browserstack(proxy)
            else:
                driver = self._start_local(proxy)

            if driver is None:
                raise RuntimeError("Session creation returned None")

            self.client.wait_for_page_load(timeout=30)

            if self.client.is_captcha_page():
                self.captcha_detected = True
                raise RuntimeError("Google CAPTCHA detected")

            # ============================================================
            # PHASE 1: Page loaded — settle
            # ============================================================
            self.logger.info("[PHASE 1] Page loaded - settling...")
            time.sleep(random.uniform(3, 6))

            # ============================================================
            # PHASE 2: Human-like reading (no clicks)
            # ============================================================
            self._human_reading_phase()

            # ============================================================
            # PHASE 3: Count visible ads + wait for them to render
            # ============================================================
            self.ads_visible_counts = self._count_visible_ads()
            self.ads_detected_in_dom = self._wait_for_ads()

            # ============================================================
            # PHASE 4: Monetag warm-up (synthetic click at center)
            # ============================================================
            if self.ads_detected_in_dom and not self.ads_visible_counts.get('adsense', 0):
                self._trigger_monetag_once()
                time.sleep(random.uniform(2, 4))

            # ============================================================
            # PHASE 5: Wait before first click
            # ============================================================
            wait_time = random.uniform(
                self.TIME_BEFORE_FIRST_CLICK_MIN,
                self.TIME_BEFORE_FIRST_CLICK_MAX,
            )
            self.logger.info(f"[PHASE 5] Waiting {wait_time:.0f}s before first click")
            time.sleep(wait_time)

            # ============================================================
            # PHASE 6: Slow click (probability-gated)
            # ============================================================
            if random.random() < self.CLICK_PROBABILITY:
                self.logger.info("[PHASE 6] Starting slow click sequence")
                self._slow_click_sequence()
            else:
                self.logger.info("[PHASE 6] Skipped clicking (probability)")

            # ============================================================
            # PHASE 7: Fill forms (if any)
            # ============================================================
            try:
                payload = self.client._generate_form_data()
                filled = self.client.fill_form(form_data=payload)
                self.forms_filled = filled
                if filled > 0:
                    self.form_payload = payload
                    time.sleep(random.uniform(5, 10))
            except Exception as e:
                self.logger.debug(f"Form fill failed: {e}")

            # ============================================================
            # PHASE 8: Maybe visit one internal link
            # ============================================================
            self._maybe_visit_internal_link()

            # ============================================================
            # PHASE 9: Final dwell
            # ============================================================
            self._final_dwell()

            try:
                fname = f"bs_{int(datetime.now(timezone.utc).timestamp())}.png"
                self.client.take_screenshot(fname)
            except Exception:
                pass

            status = 'success'
            self.logger.info(
                f"SLOW CLICK COMPLETE | ads={self.ads_clicked} "
                f"strategies={dict(self.clicks_by_strategy)} "
                f"attempts={self.click_attempts} read={self.reading_time}s "
                f"dwell={self.dwell_time}s mobile={getattr(self.client, 'is_mobile', False)}"
            )

        except RuntimeError as e:
            msg = str(e)
            if 'Proxy not effective' in msg:
                status = 'proxy_bypassed'
                self.proxy_bypassed = True
                self.logger.error(f"Session aborted (proxy bypass): {msg}")
            elif 'CAPTCHA' in msg:
                status = 'captcha'
                self.logger.error(f"Session aborted (captcha): {msg}")
            else:
                status = 'error: RuntimeError'
                self.logger.error(f"Runtime error: {msg}")
            error_msg = msg[:200]

        except Exception as e:
            status = f'error: {e.__class__.__name__}'
            error_msg = str(e)[:200]
            self.logger.error(f"Browser failed: {e}")

        finally:
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass

        yield {
            'type': 'browserstack_visit',
            'url': self.target_url,
            'status': status,
            'error': error_msg,
            'ads_clicked': self.ads_clicked,
            'clicks_by_strategy': dict(self.clicks_by_strategy),
            'click_attempts': self.click_attempts,
            'ads_detected': self.ads_detected_in_dom,
            'ads_visible': self.ads_visible_counts,
            'forms_filled': self.forms_filled,
            'form_payload': self.form_payload,
            'reading_time': self.reading_time,
            'dwell_time': self.dwell_time,
            'scroll_count': self.scroll_count,
            'internal_clicks': self.internal_clicks,
            'captcha': self.captcha_detected,
            'proxy_bypassed': self.proxy_bypassed,
            'mode': 'browserstack' if self.use_browserstack else 'local',
            'mobile': getattr(self.client, 'is_mobile', False),
            'proxy_used': self.proxy_used,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    # ============================================================
    # PHASE 2: Human Reading Phase
    # ============================================================
    def _human_reading_phase(self):
        read_time = random.uniform(self.READ_TIME_MIN, self.READ_TIME_MAX)
        self.logger.info(f"[READ] Reading page for {read_time:.0f}s...")
        start = time.time()
        while time.time() - start < read_time:
            try:
                pixels = random.randint(150, 400)
                self.client.driver.execute_script(
                    f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}});"
                )
                self.scroll_count += 1
                time.sleep(random.uniform(3.0, 7.0))
                if random.random() < 0.2:
                    self.client.driver.execute_script(
                        f"window.scrollBy({{top: -{random.randint(100, 250)}, behavior: 'smooth'}});"
                    )
                    time.sleep(random.uniform(1.5, 3.0))
            except Exception:
                pass
        self.reading_time = int(time.time() - start)
        self.logger.info(f"[READ] Done reading ({self.reading_time}s)")

    # ============================================================
    # Ad detection & diagnostics
    # ============================================================
    def _count_visible_ads(self):
        """Count each ad type individually — great for diagnosing 0-click runs."""
        counts = {}
        specs = {
            'adsense': 'ins.adsbygoogle',
            'monetag_container': 'div[id^="container-"]',
            'monetag_iframe': (
                'iframe[src*="highrevenueformat"], '
                'iframe[src*="effectivecpmnetwork"], '
                'iframe[src*="profitableratecpmnetwork"], '
                'iframe[src*="highperformanceformat"]'
            ),
            'adsterra_banner': 'div[class*="advertisement"], div[id^="ad-"]',
        }
        for name, selector in specs.items():
            try:
                elems = self.client.driver.find_elements(By.CSS_SELECTOR, selector)
                visible = sum(1 for e in elems if self._safe_is_displayed(e))
                counts[name] = {'total': len(elems), 'visible': visible}
            except Exception:
                counts[name] = {'total': 0, 'visible': 0}
        self.logger.info(f"[ADS-COUNT] {counts}")
        return counts

    def _safe_is_displayed(self, element):
        try:
            return element.is_displayed()
        except Exception:
            return False

    def _wait_for_ads(self):
        selectors = [
            'iframe[src*="highrevenueformat"]',
            'iframe[src*="effectivecpmnetwork"]',
            'iframe[src*="profitableratecpmnetwork"]',
            'iframe[src*="highperformanceformat"]',
            'div[id^="container-"]',
            'ins.adsbygoogle',
        ]
        self.logger.info(f"[ADS] Polling for ads (up to {self.ADS_WAIT_MAX}s)...")
        start = time.time()
        while time.time() - start < self.ADS_WAIT_MAX:
            for selector in selectors:
                try:
                    elements = self.client.driver.find_elements(By.CSS_SELECTOR, selector)
                    if any(self._safe_is_displayed(e) for e in elements):
                        self.logger.info(f"[ADS] Found visible: {selector}")
                        return True
                except Exception:
                    continue
            time.sleep(2.0)
        self.logger.warning(f"[ADS] No ads found after {self.ADS_WAIT_MAX}s")
        return False

    # ============================================================
    # PHASE 4: Monetag warm-up
    # ============================================================
    def _trigger_monetag_once(self):
        try:
            self.client.driver.execute_script("""
                (() => {
                    const x = window.innerWidth / 2;
                    const y = window.innerHeight / 2;
                    const el = document.elementFromPoint(x, y) || document.body;
                    el.dispatchEvent(new MouseEvent('click', {
                        bubbles: true, cancelable: true, view: window,
                        clientX: x, clientY: y, button: 0
                    }));
                })();
            """)
            self.logger.info("[MONETAG] Warm-up trigger fired")
        except Exception as e:
            self.logger.debug(f"Monetag trigger failed: {e}")

    # ============================================================
    # PHASE 6: Slow click — try AdSense first, then containers, then iframes
    # ============================================================
    def _slow_click_sequence(self):
        self.logger.info("Starting SLOW click sequence...")
        clicked = 0

        for attempt in range(self.MAX_AD_CLICKS_PER_SESSION):
            if clicked >= self.MAX_AD_CLICKS_PER_SESSION:
                break
            self.click_attempts += 1
            self.logger.info(
                f"[SLOW] Attempt {attempt + 1}/{self.MAX_AD_CLICKS_PER_SESSION}"
            )

            success = False

            # --- Strategy 1: AdSense ---
            if self._try_adsense():
                clicked += 1
                self.ads_clicked += 1
                self.clicks_by_strategy['adsense'] += 1
                success = True
                self.logger.info(f"[SLOW] ✅ AdSense clicked (total: {clicked})")

            # --- Strategy 2: Monetag container ---
            elif self._try_container():
                clicked += 1
                self.ads_clicked += 1
                self.clicks_by_strategy['container'] += 1
                success = True
                self.logger.info(f"[SLOW] ✅ Container clicked (total: {clicked})")

            # --- Strategy 3: Monetag iframe ---
            elif self._try_monetag_iframe():
                clicked += 1
                self.ads_clicked += 1
                self.clicks_by_strategy['iframe'] += 1
                success = True
                self.logger.info(f"[SLOW] ✅ Iframe clicked (total: {clicked})")

            if success:
                post_wait = random.uniform(
                    self.TIME_AFTER_CLICK_MIN, self.TIME_AFTER_CLICK_MAX,
                )
                self.logger.info(f"[SLOW] Waiting {post_wait:.0f}s after click")
                time.sleep(post_wait)
                self._close_extra_tabs()

                if clicked >= self.MAX_AD_CLICKS_PER_SESSION:
                    break

                between_wait = random.uniform(
                    self.TIME_BETWEEN_CLICKS_MIN, self.TIME_BETWEEN_CLICKS_MAX,
                )
                self.logger.info(f"[SLOW] Waiting {between_wait:.0f}s between clicks")
                time.sleep(between_wait)
            else:
                if attempt < self.MAX_AD_CLICKS_PER_SESSION - 1:
                    retry_wait = random.uniform(8, 15)
                    self.logger.info(f"[SLOW] No click this attempt - waiting {retry_wait:.0f}s")
                    time.sleep(retry_wait)

        self.logger.info(
            f"[SLOW] Sequence done: {clicked} clicks | "
            f"{self.click_attempts} attempts | strategies={dict(self.clicks_by_strategy)}"
        )

    # ------------------------------------------------------------
    # Strategy 1: AdSense (handles double iframe)
    # ------------------------------------------------------------
    def _try_adsense(self):
        try:
            units = self.client.driver.find_elements(By.CSS_SELECTOR, 'ins.adsbygoogle')
            if not units:
                return False

            for ins in units:
                if not self._safe_is_displayed(ins):
                    continue

                ins_key = (
                    ins.get_attribute('data-ad-slot')
                    or ins.get_attribute('data-ad-client')
                    or str(id(ins))
                )
                if ins_key in self._tried_adsense:
                    continue
                self._tried_adsense.add(ins_key)

                # Scroll into view
                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        ins,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                # Find inner iframe
                try:
                    iframe = ins.find_element(By.TAG_NAME, 'iframe')
                except NoSuchElementException:
                    self.logger.debug("[ADSENSE] No iframe inside ins.adsbygoogle")
                    continue
                except Exception:
                    continue

                # Enter iframe(s)
                try:
                    self.client.driver.switch_to.frame(iframe)
                    time.sleep(random.uniform(1.2, 2.5))

                    # Google sometimes double-nests
                    try:
                        inner = self.client.driver.find_element(By.TAG_NAME, 'iframe')
                        self.client.driver.switch_to.frame(inner)
                        time.sleep(random.uniform(0.8, 1.5))
                    except NoSuchElementException:
                        pass
                    except Exception:
                        pass

                    try:
                        body = self.client.driver.find_element(By.TAG_NAME, 'body')
                        ActionChains(self.client.driver) \
                            .move_to_element(body) \
                            .pause(random.uniform(0.4, 0.9)) \
                            .click(body) \
                            .perform()
                    except Exception:
                        try:
                            self.client.driver.find_element(
                                By.TAG_NAME, 'body'
                            ).click()
                        except Exception:
                            return False
                    return True
                finally:
                    try:
                        self.client.driver.switch_to.default_content()
                    except Exception:
                        pass
        except Exception as e:
            self.logger.debug(f"[ADSENSE] Strategy error: {e}")
        return False

    # ------------------------------------------------------------
    # Strategy 2: Monetag container (div[id^="container-"])
    # ------------------------------------------------------------
    def _try_container(self):
        try:
            containers = self.client.driver.find_elements(
                By.CSS_SELECTOR, 'div[id^="container-"]'
            )
            for c in containers:
                if not self._safe_is_displayed(c):
                    continue
                cid = c.get_attribute('id') or str(id(c))
                if cid in self._tried_containers:
                    continue
                self._tried_containers.add(cid)

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        c,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(c) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(c) \
                        .perform()
                    return True
                except Exception as e:
                    self.logger.debug(f"Container click failed: {e}")
        except Exception as e:
            self.logger.debug(f"[CONTAINER] Strategy error: {e}")
        return False

    # ------------------------------------------------------------
    # Strategy 3: Monetag iframe
    # ------------------------------------------------------------
    def _try_monetag_iframe(self):
        try:
            iframes = self.client.driver.find_elements(
                By.CSS_SELECTOR,
                'iframe[src*="highrevenueformat"], '
                'iframe[src*="effectivecpmnetwork"], '
                'iframe[src*="highperformanceformat"], '
                'iframe[src*="profitableratecpmnetwork"]'
            )
            for f in iframes:
                if not self._safe_is_displayed(f):
                    continue
                fid = f.get_attribute('id') or f.get_attribute('src') or str(id(f))
                if fid in self._tried_iframes:
                    continue
                self._tried_iframes.add(fid)

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        f,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(f) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(f) \
                        .perform()
                    return True
                except Exception as e:
                    self.logger.debug(f"Iframe click failed: {e}")
        except Exception as e:
            self.logger.debug(f"[IFRAME] Strategy error: {e}")
        return False

    # ============================================================
    # PHASE 8: Maybe visit internal link (50%)
    # ============================================================
    def _maybe_visit_internal_link(self):
        if random.random() > 0.5:
            self.logger.info("[LINK] Skipping internal link this session")
            return

        try:
            target_domain = urlparse(self.target_url).netloc
            selectors = [
                'article a:not([href^="#"])',
                'h2 a',
                '.post-title a',
            ]
            for selector in selectors:
                links = self.client.driver.find_elements(By.CSS_SELECTOR, selector)
                if not links:
                    continue

                link = random.choice(links[:3])
                href = link.get_attribute('href') or ''
                if not href or target_domain not in href:
                    continue

                self.logger.info(f"[LINK] Visiting: {href[:60]}")
                link.click()
                time.sleep(random.uniform(5, 10))

                for _ in range(random.randint(2, 4)):
                    self.client.driver.execute_script(
                        f"window.scrollBy({{top: {random.randint(200, 500)}, behavior: 'smooth'}});"
                    )
                    time.sleep(random.uniform(3, 6))

                self.client.driver.back()
                time.sleep(random.uniform(3, 5))
                self.internal_clicks = 1
                break
        except Exception as e:
            self.logger.debug(f"Internal link error: {e}")

    # ============================================================
    # PHASE 9: Final dwell
    # ============================================================
    def _final_dwell(self):
        try:
            duration = random.randint(self.DWELL_TIME_MIN, self.DWELL_TIME_MAX)
            self.logger.info(f"[DWELL] Final dwell for {duration}s...")
            start = time.time()
            last_scroll = time.time()

            while time.time() - start < duration:
                if time.time() - last_scroll > random.uniform(10, 20):
                    try:
                        pixels = random.randint(200, 500)
                        self.client.driver.execute_script(
                            f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}});"
                        )
                        if random.random() < 0.3:
                            time.sleep(random.uniform(1, 2))
                            self.client.driver.execute_script(
                                f"window.scrollBy({{top: -{random.randint(100, 300)}, behavior: 'smooth'}});"
                            )
                        last_scroll = time.time()
                    except Exception:
                        pass
                time.sleep(random.uniform(2, 5))

            self.dwell_time = int(time.time() - start)
            self.logger.info(f"[DWELL] Complete ({self.dwell_time}s)")
        except Exception as e:
            self.logger.debug(f"Dwell error: {e}")
            self.dwell_time = self.DWELL_TIME_MIN

    # ============================================================
    # Tab cleanup
    # ============================================================
    def _close_extra_tabs(self):
        try:
            handles = self.client.driver.window_handles
            if len(handles) > 1:
                main = handles[0]
                for handle in handles[1:]:
                    try:
                        self.client.driver.switch_to.window(handle)
                        time.sleep(random.uniform(2.0, 4.0))
                        self.client.driver.close()
                    except Exception:
                        continue
                try:
                    self.client.driver.switch_to.window(main)
                except Exception:
                    pass
                self.logger.info(f"[TABS] Closed {len(handles) - 1} extra tabs")
        except Exception as e:
            self.logger.debug(f"Tab close error: {e}")

    # ============================================================
    # Proxy
    # ============================================================
    def _pick_proxy(self):
        if os.environ.get('DISABLE_PROXIES', 'false').lower() == 'true':
            return None
        if os.environ.get('USE_PROXY_WITH_BROWSER', 'false').lower() != 'true':
            return None
        if self._proxy_provider is None:
            self._proxy_provider = BrowserProxyProvider()
        return self._proxy_provider.pick()

    # ============================================================
    # Session starters
    # ============================================================
    def _start_browserstack(self, proxy=None):
        username = os.environ.get('BROWSERSTACK_USERNAME', '').strip()
        access_key = os.environ.get('BROWSERSTACK_ACCESS_KEY', '').strip()
        self.client = BrowserStackClient(username, access_key)
        browser, os_name, os_version = random.choice(self.BROWSERSTACK_BROWSERS)
        self.logger.info(f"BrowserStack: {browser} on {os_name} {os_version}")
        return self.client.start_session(
            url=self.target_url,
            browser=browser,
            os_name=os_name,
            os_version=os_version,
            proxy=proxy,
            use_local=False,
            mobile=self.USE_MOBILE,
        )

    def _start_local(self, proxy=None):
        self.client = LocalBrowserClient()
        self.logger.info(f"Local Chrome (Mobile: {self.USE_MOBILE})")
        return self.client.start_session(
            url=self.target_url,
            headless=True,
            proxy=proxy,
            mobile=self.USE_MOBILE,
        )

    # ============================================================
    # Error handler
    # ============================================================
    def handle_error(self, failure):
        self.logger.error(f"{failure.request.url} | {failure.value}")

    def closed(self, reason):
        self.logger.info(
            f"Closed ({reason}) | ads={self.ads_clicked} "
            f"strategies={dict(self.clicks_by_strategy)} "
            f"attempts={self.click_attempts} "
            f"read={self.reading_time}s dwell={self.dwell_time}s "
            f"proxy_bypassed={self.proxy_bypassed}"
        )