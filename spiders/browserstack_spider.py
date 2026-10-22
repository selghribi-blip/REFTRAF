# spiders/browserstack_spider.py
# Multi-page slow click mode with full ad network coverage.
# ==========================================================================
import os
import re
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
    # Tunable behavior
    # ============================================================
    USE_MOBILE = False
    MAX_PAGES_PER_SESSION = int(os.getenv('MAX_PAGES_PER_SESSION', '3'))
    READ_TIME_MIN = 30
    READ_TIME_MAX = 60

    MAX_AD_CLICKS_PER_SESSION = int(os.getenv('MAX_ADS_PER_SESSION', '2'))

    # 100% click attempt — human-ness comes from timing/randomness,
    # not from skipping the sequence entirely.
    CLICK_PROBABILITY = 1.0

    TIME_BEFORE_FIRST_CLICK_MIN = 75
    TIME_BEFORE_FIRST_CLICK_MAX = 135
    TIME_BETWEEN_CLICKS_MIN = 10
    TIME_BETWEEN_CLICKS_MAX = 20
    TIME_AFTER_CLICK_MIN = 15
    TIME_AFTER_CLICK_MAX = 30

    DWELL_TIME_MIN = int(os.getenv('DWELL_TIME_MIN', '180'))
    DWELL_TIME_MAX = int(os.getenv('DWELL_TIME_MAX', '300'))

    INTERNAL_READ_MIN = 20
    INTERNAL_READ_MAX = 40
    INTERNAL_DWELL_MIN = 60
    INTERNAL_DWELL_MAX = 120
    INTERNAL_MAX_CLICKS = 1

    ADS_WAIT_MAX = 40

    # FIX: AdSense is not approved on the site yet — 30s per page is wasted.
    # When AdSense gets approved, bump this back to 25-30.
    ADSENSE_IFRAME_WAIT = 5

    # ============================================================
    # Skip boring paths (privacy, about, contact…)
    # ============================================================
    BORING_PATHS = {
        '/privacy', '/privacy-policy', '/privacy-notice',
        '/about', '/about-us',
        '/contact', '/contact-us',
        '/terms', '/terms-of-service', '/terms-and-conditions',
        '/disclaimer', '/legal', '/cookie-policy', '/cookies', '/tos',
        '/sitemap', '/sitemap.xml',
        '/p/privacy', '/p/privacy-policy', '/p/privacy-policy.html',
        '/p/about', '/p/about-us', '/p/about-us.html',
        '/p/contact', '/p/contact-us', '/p/contact-us.html',
        '/p/terms', '/p/terms-of-service', '/p/terms-of-service.html',
        '/p/disclaimer', '/p/disclaimer.html',
        '/p/sitemap', '/p/sitemap.html',
    }

    # ============================================================
    # Ad network domains — never treat as internal links
    # (FIX: previous code matched these because they contained
    #  "forjo.tech" in the refer= parameter)
    # ============================================================
    AD_NETWORKS = (
        'monetag', 'propellerads', 'adsterra', 'onclickalgo',
        'onclickmega', 'onclckds', 'effectivecpmnetwork',
        'highrevenueformat', 'highperformanceformat',
        'profitableratecpmnetwork', 'quge5',
        'quarrelsomebitter', 'difficultblock',
        'bicea.org', 'afders.org', 'spendsdetachment',
        'uplcm.com', 'omg10.com', 'asiafilm.org',
        'nthefeetofthea', 'viiukuhe', 'ad-maven',
        'popads', 'popcash', 'mgid', 'taboola',
        'outbrain', 'revcontent',
        'doubleclick', 'googlesyndication', 'googleadservices',
    )

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
        self.reading_time = 0
        self.click_attempts = 0

        self.captcha_detected = False
        self.ads_detected_in_dom = False
        self.ads_visible_counts = {}
        self.clicks_by_strategy = Counter()
        self.click_verifications = []
        self.proxy_bypassed = False
        self.form_payload = None

        self._visited_urls = set()
        self._tried_adsense = set()
        self._tried_adsterra = set()
        self._tried_containers = set()
        self._tried_iframes = set()
        self._tried_ezoic = set()
        self._proxy_provider = None

    # ============================================================
    # Path filter
    # ============================================================
    def _is_boring_path(self, url):
        try:
            path = urlparse(url).path.lower()
            path = path.rstrip('/')
            if path.endswith('.html'):
                path = path[:-5]
            path = path.rstrip('/')
            return path in self.BORING_PATHS
        except Exception:
            return False

    # ============================================================
    # Entry point
    # ============================================================
    def start_requests(self):
        if not self.target_url.startswith(('http://', 'https://')):
            self.target_url = 'https://' + self.target_url

        self.logger.info(f"Starting MULTI-PAGE bot for: {self.target_url}")
        self.logger.info(f"   Mode: {'BrowserStack' if self.use_browserstack else 'Local'}")
        self.logger.info(f"   Max pages/session: {self.MAX_PAGES_PER_SESSION}")
        self.logger.info(f"   Max clicks (main): {self.MAX_AD_CLICKS_PER_SESSION}")
        self.logger.info(f"   Max clicks (internal): {self.INTERNAL_MAX_CLICKS}")
        self.logger.info(f"   Dwell (main): {self.DWELL_TIME_MIN}-{self.DWELL_TIME_MAX}s")
        self.logger.info(f"   Dwell (internal): {self.INTERNAL_DWELL_MIN}-{self.INTERNAL_DWELL_MAX}s")
        self.logger.info(f"   Click probability: {self.CLICK_PROBABILITY}")
        self.logger.info(f"   AdSense iframe wait: {self.ADSENSE_IFRAME_WAIT}s")
        self.logger.info(f"   Strategies: adsense → adsterra → container → iframe → ezoic")

        if self.use_browserstack:
            user = os.environ.get('BROWSERSTACK_USERNAME', '').strip()
            key = os.environ.get('BROWSERSTACK_ACCESS_KEY', '').strip()
            if not (user and key):
                self.logger.warning("BrowserStack creds missing - falling back to LOCAL")
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
        pages_stats = []

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

            self._visited_urls.add(self.target_url)

            self.logger.info("[MAIN] Page 1 — target")
            pages_stats.append(self._run_page_phases(1, is_first=True))

            for page_num in range(2, self.MAX_PAGES_PER_SESSION + 1):
                next_url = self._pick_internal_link_url()
                if not next_url:
                    self.logger.info(f"[NAV] No more internal links (stopped at {page_num-1})")
                    break

                self.logger.info(f"[NAV] Page {page_num}: {next_url[:80]}")
                try:
                    self.client.driver.get(next_url)
                    self.client.wait_for_page_load(timeout=20)
                    time.sleep(random.uniform(2, 4))
                    pages_stats.append(self._run_page_phases(page_num, is_first=False))
                except Exception as e:
                    self.logger.warning(f"[NAV] Page {page_num} failed: {e}")
                    break

            try:
                fname = f"bs_{int(datetime.now(timezone.utc).timestamp())}.png"
                self.client.take_screenshot(fname)
            except Exception:
                pass

            status = 'success'
            self.logger.info(
                f"SESSION COMPLETE | pages={len(pages_stats)} "
                f"ads={self.ads_clicked} strategies={dict(self.clicks_by_strategy)} "
                f"verifications={len(self.click_verifications)} "
                f"read={self.reading_time}s dwell={self.dwell_time}s"
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
            'pages_visited': len(pages_stats),
            'pages': pages_stats,
            'ads_clicked': self.ads_clicked,
            'clicks_by_strategy': dict(self.clicks_by_strategy),
            'click_verifications': self.click_verifications,
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
    # Page phases
    # ============================================================
    def _run_page_phases(self, page_number, is_first=False):
        page_url = self.client.driver.current_url
        page_stats = {
            'page_number': page_number,
            'url': page_url,
            'ads_clicked': 0,
            'forms_filled': 0,
            'reading_time': 0,
            'dwell_time': 0,
            'scrolls': 0,
            'adsense_waited': False,
        }

        self.logger.info(f"[PAGE {page_number}] URL: {page_url[:80]}")

        time.sleep(random.uniform(3, 6))

        if is_first:
            read_min, read_max = self.READ_TIME_MIN, self.READ_TIME_MAX
        else:
            read_min, read_max = self.INTERNAL_READ_MIN, self.INTERNAL_READ_MAX
        read_time = random.uniform(read_min, read_max)
        self.logger.info(f"[PAGE {page_number}] Reading for {read_time:.0f}s")
        start = time.time()
        while time.time() - start < read_time:
            try:
                pixels = random.randint(150, 400)
                self.client.driver.execute_script(
                    f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}});"
                )
                page_stats['scrolls'] += 1
                self.scroll_count += 1
                time.sleep(random.uniform(3.0, 7.0))
                if random.random() < 0.2:
                    self.client.driver.execute_script(
                        f"window.scrollBy({{top: -{random.randint(100, 250)}, behavior: 'smooth'}});"
                    )
                    time.sleep(random.uniform(1.5, 3.0))
            except Exception:
                pass
        page_stats['reading_time'] = int(time.time() - start)
        self.reading_time += page_stats['reading_time']

        self.ads_visible_counts = self._count_visible_ads()

        adsense_info = self.ads_visible_counts.get('adsense', {})
        if adsense_info.get('total', 0) > 0 and adsense_info.get('visible', 0) == 0:
            self.logger.info(
                f"[PAGE {page_number}] AdSense present but not visible — waiting up to {self.ADSENSE_IFRAME_WAIT}s"
            )
            waited = self._wait_for_adsense_iframe(timeout=self.ADSENSE_IFRAME_WAIT)
            page_stats['adsense_waited'] = waited
            if waited:
                self.ads_visible_counts = self._count_visible_ads()

        self.ads_detected_in_dom = self._wait_for_ads()

        if is_first and self.ads_detected_in_dom and \
           not self.ads_visible_counts.get('adsense', {}).get('visible', 0):
            self._trigger_monetag_once()
            time.sleep(random.uniform(2, 4))

        if is_first:
            wait_time = random.uniform(
                self.TIME_BEFORE_FIRST_CLICK_MIN, self.TIME_BEFORE_FIRST_CLICK_MAX,
            )
        else:
            wait_time = random.uniform(15, 30)
        self.logger.info(f"[PAGE {page_number}] Waiting {wait_time:.0f}s before click")
        time.sleep(wait_time)

        pre_ads = self.ads_clicked
        if random.random() < self.CLICK_PROBABILITY:
            page_max_clicks = (
                self.MAX_AD_CLICKS_PER_SESSION if is_first
                else self.INTERNAL_MAX_CLICKS
            )
            self.logger.info(f"[PAGE {page_number}] Click sequence (max={page_max_clicks})")
            self._slow_click_sequence(max_clicks=page_max_clicks)
        else:
            self.logger.info(f"[PAGE {page_number}] Skipped (probability)")
        page_stats['ads_clicked'] = self.ads_clicked - pre_ads

        try:
            payload = self.client._generate_form_data()
            filled = self.client.fill_form(form_data=payload)
            page_stats['forms_filled'] = filled
            self.forms_filled += filled
            if filled > 0:
                self.form_payload = payload
                time.sleep(random.uniform(5, 10))
        except Exception as e:
            self.logger.debug(f"Form fill failed: {e}")

        if is_first:
            dwell_min, dwell_max = self.DWELL_TIME_MIN, self.DWELL_TIME_MAX
        else:
            dwell_min, dwell_max = self.INTERNAL_DWELL_MIN, self.INTERNAL_DWELL_MAX
        dwell = random.randint(dwell_min, dwell_max)
        self.logger.info(f"[PAGE {page_number}] Dwell for {dwell}s")
        start = time.time()
        last_scroll = time.time()
        while time.time() - start < dwell:
            if time.time() - last_scroll > random.uniform(10, 20):
                try:
                    pixels = random.randint(200, 500)
                    self.client.driver.execute_script(
                        f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}});"
                    )
                    page_stats['scrolls'] += 1
                    self.scroll_count += 1
                    if random.random() < 0.3:
                        time.sleep(random.uniform(1, 2))
                        self.client.driver.execute_script(
                            f"window.scrollBy({{top: -{random.randint(100, 300)}, behavior: 'smooth'}});"
                        )
                    last_scroll = time.time()
                except Exception:
                    pass
            time.sleep(random.uniform(2, 5))
        page_stats['dwell_time'] = int(time.time() - start)
        self.dwell_time += page_stats['dwell_time']

        self.logger.info(
            f"[PAGE {page_number}] DONE | ads={page_stats['ads_clicked']} "
            f"read={page_stats['reading_time']}s dwell={page_stats['dwell_time']}s"
        )
        return page_stats

    # ============================================================
    # Ad detection
    # ============================================================
    def _count_visible_ads(self):
        counts = {}
        specs = {
            'adsense': 'ins.adsbygoogle',
            'adsterra_native': 'div[id^="container-"]',
            'adsterra_banner_link': (
                'a[href*="adsterra"], '
                'a[href*="adsterra.net"], '
                'a[href*="adsterra.org"]'
            ),
            'adsterra_iframe': (
                'iframe[src*="adsterra"], '
                'iframe[src*="adsterra.net"], '
                'iframe[src*="adsterra.org"]'
            ),
            'adsterra_div': (
                'div[class*="adsterra"], '
                'div[id*="adsterra"]'
            ),
            'monetag_iframe': (
                'iframe[src*="highrevenueformat"], '
                'iframe[src*="effectivecpmnetwork"], '
                'iframe[src*="profitableratecpmnetwork"], '
                'iframe[src*="highperformanceformat"], '
                'iframe[src*="quge5"]'
            ),
            'generic_ad': (
                'div[class*="advertisement"], '
                'div[id^="ad-"]'
            ),
            'ezoic_placeholder': 'div[id^="ezoic-pub-ad-placeholder-"]',
            'ezoic_iframe': 'iframe[src*="ezoic"]',
            'mgid_widget': (
                'div[class*="mgid"], '
                'iframe[src*="mgid.com"]'
            ),
            'taboola_widget': (
                'div[id*="taboola"], '
                'iframe[id*="taboola"]'
            ),
            'outbrain_widget': (
                'div[class*="OUTBRAIN"], '
                'iframe[src*="outbrain"]'
            ),
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
            'iframe[src*="quge5"]',
            'div[id^="container-"]',
            'ins.adsbygoogle',
            'a[href*="adsterra"]',
            'iframe[src*="adsterra"]',
            'div[class*="adsterra"]',
            'div[id^="ezoic-pub-ad-placeholder-"]',
            'iframe[src*="ezoic"]',
            'iframe[src*="mgid.com"]',
            'div[id*="taboola"]',
            'div[class*="OUTBRAIN"]',
        ]
        self.logger.info(f"[ADS] Polling for ads (up to {self.ADS_WAIT_MAX}s)")
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

    def _wait_for_adsense_iframe(self, timeout=5):
        try:
            units = self.client.driver.find_elements(By.CSS_SELECTOR, 'ins.adsbygoogle')
            if not units:
                self.logger.info("[ADSENSE] No ins.adsbygoogle units found")
                return False
            start = time.time()
            last_log = 0
            while time.time() - start < timeout:
                for ins in units:
                    try:
                        if not self._safe_is_displayed(ins):
                            continue
                        try:
                            iframe = ins.find_element(By.TAG_NAME, 'iframe')
                        except NoSuchElementException:
                            continue
                        if iframe and iframe.is_displayed():
                            size = iframe.size
                            w, h = size.get('width', 0), size.get('height', 0)
                            if w > 100 and h > 50:
                                self.logger.info(
                                    f"[ADSENSE] iframe loaded ({w}x{h}) after "
                                    f"{int(time.time() - start)}s"
                                )
                                return True
                            elif w > 0 and h > 0:
                                if time.time() - last_log > 5:
                                    self.logger.debug(
                                        f"[ADSENSE] Small iframe: {w}x{h} (loading)"
                                    )
                                    last_log = time.time()
                    except Exception:
                        continue
                time.sleep(1.5)
            self.logger.info(f"[ADSENSE] No usable iframe after {timeout}s")
            return False
        except Exception as e:
            self.logger.debug(f"[ADSENSE] Wait error: {e}")
            return False

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
    # Click sequence
    # ============================================================
    def _slow_click_sequence(self, max_clicks=None):
        if max_clicks is None:
            max_clicks = self.MAX_AD_CLICKS_PER_SESSION

        self.logger.info(f"Starting SLOW click sequence (max={max_clicks})")
        clicked = 0

        for attempt in range(max_clicks):
            if clicked >= max_clicks:
                break
            self.click_attempts += 1
            self.logger.info(f"[SLOW] Attempt {attempt + 1}/{max_clicks}")

            success, verification, strategy = self._try_all_strategies()

            if success:
                clicked += 1
                self.ads_clicked += 1
                self.clicks_by_strategy[strategy] += 1
                if verification:
                    self.click_verifications.append(verification)
                self.logger.info(
                    f"[SLOW] ✅ {strategy} clicked (total: {clicked}) "
                    f"| verif={verification}"
                )

                post_wait = random.uniform(
                    self.TIME_AFTER_CLICK_MIN, self.TIME_AFTER_CLICK_MAX,
                )
                self.logger.info(f"[SLOW] Waiting {post_wait:.0f}s after click")
                time.sleep(post_wait)
                self._close_extra_tabs()

                if clicked >= max_clicks:
                    break

                between_wait = random.uniform(
                    self.TIME_BETWEEN_CLICKS_MIN, self.TIME_BETWEEN_CLICKS_MAX,
                )
                self.logger.info(f"[SLOW] Waiting {between_wait:.0f}s between clicks")
                time.sleep(between_wait)
            else:
                if attempt < max_clicks - 1:
                    retry_wait = random.uniform(8, 15)
                    self.logger.info(f"[SLOW] No click — waiting {retry_wait:.0f}s")
                    time.sleep(retry_wait)

        self.logger.info(
            f"[SLOW] Done: {clicked} clicks | {self.click_attempts} attempts | "
            f"strategies={dict(self.clicks_by_strategy)}"
        )

    def _try_all_strategies(self):
        for name, method in [
            ('adsense',   self._try_adsense),
            ('adsterra',  self._try_adsterra),
            ('container', self._try_container),
            ('iframe',    self._try_iframe),
            ('ezoic',     self._try_ezoic),
        ]:
            try:
                success, verification = method()
                if success:
                    return True, verification, name
            except Exception as e:
                self.logger.debug(f"[{name.upper()}] Unhandled: {e}")
        return False, None, None

    # ------------------------------------------------------------
    # Strategy 1: AdSense
    # ------------------------------------------------------------
    def _try_adsense(self):
        try:
            units = self.client.driver.find_elements(By.CSS_SELECTOR, 'ins.adsbygoogle')
            if not units:
                return False, None

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

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        ins,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                iframe = None
                try:
                    iframe = ins.find_element(By.TAG_NAME, 'iframe')
                except NoSuchElementException:
                    self.logger.debug("[ADSENSE] No iframe")
                    continue
                except Exception:
                    continue

                if not iframe or not iframe.is_displayed():
                    continue

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    self.client.driver.switch_to.frame(iframe)
                    time.sleep(random.uniform(1.2, 2.5))

                    try:
                        inner = self.client.driver.find_element(By.TAG_NAME, 'iframe')
                        self.client.driver.switch_to.frame(inner)
                        time.sleep(random.uniform(0.8, 1.5))
                    except NoSuchElementException:
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
                            return False, None
                finally:
                    try:
                        self.client.driver.switch_to.default_content()
                    except Exception:
                        pass

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'adsense'
                return True, verification
        except Exception as e:
            self.logger.debug(f"[ADSENSE] error: {e}")
        return False, None

    # ------------------------------------------------------------
    # Strategy 2: Adsterra
    # ------------------------------------------------------------
    def _try_adsterra(self):
        try:
            # 1) Direct banner links
            banner_links = self.client.driver.find_elements(
                By.CSS_SELECTOR,
                'a[href*="adsterra"], a[href*="adsterra.net"], a[href*="adsterra.org"]'
            )
            for link in banner_links:
                if not self._safe_is_displayed(link):
                    continue
                key = link.get_attribute('href') or str(id(link))
                if key in self._tried_adsterra:
                    continue
                self._tried_adsterra.add(key)

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        link,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(link) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(link) \
                        .perform()
                except Exception as e:
                    self.logger.debug(f"Adsterra link click failed: {e}")
                    continue

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'adsterra_link'
                return True, verification

            # 2) Adsterra iframes
            iframes = self.client.driver.find_elements(
                By.CSS_SELECTOR,
                'iframe[src*="adsterra"], iframe[src*="adsterra.net"], iframe[src*="adsterra.org"]'
            )
            for f in iframes:
                if not self._safe_is_displayed(f):
                    continue
                key = f.get_attribute('src') or str(id(f))
                if key in self._tried_adsterra:
                    continue
                self._tried_adsterra.add(key)

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        f,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    self.client.driver.switch_to.frame(f)
                    time.sleep(random.uniform(1.2, 2.5))
                    try:
                        body = self.client.driver.find_element(By.TAG_NAME, 'body')
                        ActionChains(self.client.driver) \
                            .move_to_element(body) \
                            .pause(random.uniform(0.4, 0.9)) \
                            .click(body) \
                            .perform()
                    except Exception:
                        try:
                            self.client.driver.find_element(By.TAG_NAME, 'body').click()
                        except Exception:
                            return False, None
                finally:
                    try:
                        self.client.driver.switch_to.default_content()
                    except Exception:
                        pass

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'adsterra_iframe'
                return True, verification

            # 3) Adsterra div containers
            divs = self.client.driver.find_elements(
                By.CSS_SELECTOR,
                'div[class*="adsterra"], div[id*="adsterra"]'
            )
            for d in divs:
                if not self._safe_is_displayed(d):
                    continue
                key = d.get_attribute('id') or d.get_attribute('class') or str(id(d))
                if key in self._tried_adsterra:
                    continue
                self._tried_adsterra.add(key)

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        d,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(d) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(d) \
                        .perform()
                except Exception as e:
                    self.logger.debug(f"Adsterra div click failed: {e}")
                    continue

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'adsterra_div'
                return True, verification

        except Exception as e:
            self.logger.debug(f"[ADSTERRA] error: {e}")
        return False, None

    # ------------------------------------------------------------
    # Strategy 3: Container (Adsterra Native Banner)
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

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(c) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(c) \
                        .perform()
                except Exception as e:
                    self.logger.debug(f"Container click failed: {e}")
                    continue

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'container'
                return True, verification
        except Exception as e:
            self.logger.debug(f"[CONTAINER] error: {e}")
        return False, None

    # ------------------------------------------------------------
    # Strategy 4: Monetag iframe
    # ------------------------------------------------------------
    def _try_iframe(self):
        try:
            iframes = self.client.driver.find_elements(
                By.CSS_SELECTOR,
                'iframe[src*="highrevenueformat"], '
                'iframe[src*="effectivecpmnetwork"], '
                'iframe[src*="highperformanceformat"], '
                'iframe[src*="profitableratecpmnetwork"], '
                'iframe[src*="quge5"]'
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

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                try:
                    ActionChains(self.client.driver) \
                        .move_to_element(f) \
                        .pause(random.uniform(0.3, 0.7)) \
                        .click(f) \
                        .perform()
                except Exception as e:
                    self.logger.debug(f"Iframe click failed: {e}")
                    continue

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'iframe'
                return True, verification
        except Exception as e:
            self.logger.debug(f"[IFRAME] error: {e}")
        return False, None

    # ------------------------------------------------------------
    # Strategy 5: Ezoic
    # ------------------------------------------------------------
    def _try_ezoic(self):
        try:
            placeholders = self.client.driver.find_elements(
                By.CSS_SELECTOR, 'div[id^="ezoic-pub-ad-placeholder-"]'
            )
            for ph in placeholders:
                if not self._safe_is_displayed(ph):
                    continue
                key = ph.get_attribute('id') or str(id(ph))
                if key in self._tried_ezoic:
                    continue
                self._tried_ezoic.add(key)

                iframe = None
                try:
                    iframe = ph.find_element(By.TAG_NAME, 'iframe')
                except NoSuchElementException:
                    iframe = None

                try:
                    self.client.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                        ph,
                    )
                except Exception:
                    continue
                time.sleep(random.uniform(2.0, 3.5))

                pre_handles = self.client.driver.window_handles
                pre_url = self.client.driver.current_url

                if iframe and iframe.is_displayed():
                    try:
                        self.client.driver.switch_to.frame(iframe)
                        time.sleep(random.uniform(1.2, 2.5))
                        try:
                            body = self.client.driver.find_element(By.TAG_NAME, 'body')
                            ActionChains(self.client.driver) \
                                .move_to_element(body) \
                                .pause(random.uniform(0.4, 0.9)) \
                                .click(body) \
                                .perform()
                        except Exception:
                            try:
                                self.client.driver.find_element(By.TAG_NAME, 'body').click()
                            except Exception:
                                return False, None
                    finally:
                        try:
                            self.client.driver.switch_to.default_content()
                        except Exception:
                            pass
                else:
                    try:
                        ActionChains(self.client.driver) \
                            .move_to_element(ph) \
                            .pause(random.uniform(0.3, 0.7)) \
                            .click(ph) \
                            .perform()
                    except Exception:
                        continue

                verification = self.client.verify_click_result(pre_handles, pre_url)
                verification['strategy'] = 'ezoic'
                return True, verification
        except Exception as e:
            self.logger.debug(f"[EZOIC] error: {e}")
        return False, None

    # ============================================================
    # Internal link discovery
    # FIX: match netloc exactly (previous version matched substring,
    # which let ad network URLs through because they contained
    # "forjo.tech" inside a refer= parameter)
    # ============================================================
    def _pick_internal_link_url(self):
        try:
            target_domain = urlparse(self.target_url).netloc

            skip_substrings = [
                '/tag/', '/label/', '/search', '/feed', '/archive',
                '/privacy', '/about', '/contact', '/terms', '/disclaimer',
                '/sitemap', '/p/about', '/p/privacy', '/p/contact',
                'facebook.com', 'twitter.com', 'instagram.com',
                'youtube.com', 'whatsapp', 't.me', 'telegram',
                'mailto:', 'javascript:',
                '.jpg', '.jpeg', '.png', '.gif', '.webp',
                '.pdf', '.zip', '.css', '.js',
            ]

            selectors = [
                '.post-title a',
                '.post h3 a',
                'h2.post-title a',
                'a.post-link',
                '.blog-pager a',
                '.blog-pager-older-link',
                '#BlogArchive1_ArchiveList a',
                '.BlogArchive a',
                'article h2 a',
                'article .post-title a',
                '.PopularPosts a',
                '.FeaturedPost a',
                '.widget-content a',
                'article a[href]:not([href^="#"])',
                'h3 a[href]:not([href^="#"])',
                'a[href]:not([href^="#"])',
            ]

            candidates = []

            for selector in selectors:
                try:
                    links = self.client.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for link in links[:15]:
                    try:
                        href = link.get_attribute('href') or ''
                        if not href:
                            continue

                        # ---- FIX: check actual netloc, not substring ----
                        parsed_href = urlparse(href)
                        if parsed_href.netloc != target_domain:
                            continue

                        if href in self._visited_urls:
                            continue

                        lower_href = href.lower()

                        # ---- FIX: exclude any URL mentioning an ad network ----
                        if any(ad in lower_href for ad in self.AD_NETWORKS):
                            continue

                        if any(s in lower_href for s in skip_substrings):
                            continue

                        if self._is_boring_path(href):
                            continue

                        path = parsed_href.path
                        if len(path) < 5 or path.strip('/') == '':
                            continue
                        candidates.append(href)
                    except Exception:
                        continue
                if candidates:
                    break

            if candidates:
                article_like = [
                    c for c in candidates
                    if '.html' in c or re.search(r'/\d{4}/', c)
                ]
                pool = article_like or candidates
                chosen = pool[0]
                self._visited_urls.add(chosen)
                self.logger.info(f"[NAV] Picked internal: {chosen[:100]}")
                return chosen

            self.logger.info("[NAV] No internal links matched any selector")
        except Exception as e:
            self.logger.debug(f"Link pick error: {e}")
        return None

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
                self.internal_clicks += len(handles) - 1
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
            f"verifications={len(self.click_verifications)} "
            f"attempts={self.click_attempts} "
            f"read={self.reading_time}s dwell={self.dwell_time}s "
            f"proxy_bypassed={self.proxy_bypassed}"
        )
