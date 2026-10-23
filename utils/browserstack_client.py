# utils/browserstack_client.py
# BrowserStack + Local Chrome with auto Chrome version detection,
# real proxy verification (MV3 auth), click verification, anti-detection,
# mobile emulation, and Monetag-first classification.
# ==========================================================================
import os
import re
import time
import json
import copy
import random
import logging
import tempfile
import zipfile
import subprocess
from urllib.parse import urlparse

import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    ElementClickInterceptedException, WebDriverException,
    StaleElementReferenceException, ElementNotInteractableException,
)

logger = logging.getLogger(__name__)


# ============================================================
# Constants — detection keywords for CI bypass
# ============================================================
AZURE_DATACENTER_KEYWORDS = (
    'azure', 'microsoft', 'cloudapp', 'github',
    'amazon', 'aws', 'google cloud', 'digitalocean',
)

# Revenue network keyword map — Monetag-first.
REVENUE_NETWORKS = {
    # ----- Monetag / PropellerAds (primary revenue) -----
    'monetag': [
        'nn125.com', 'nn68.com', 'nnmnn.com', 'link2nn',
        'omg10.com', 'uplcm.com',
        'monetag.com', 'quge5.com', 'quge5.net',
        'effectivecpmnetwork', 'highperformanceformat',
        'highrevenueformat', 'profitableratecpmnetwork',
        'propellerads.com', 'propeller-ads.com',
        'onclickalgo', 'onclickmega', 'onclckds',
        'mndly.com', 'mndl.net',
        'link-to-nn', 'smartlink', 'directlink',
    ],
    # ----- Affiliate destinations (Monetag popunder targets) -----
    'affiliate_offer': [
        'aliexpress.com', 'alibaba.com', 'temu.com', 'shein.com',
        'banggood.com', 'dhgate.com', 'wish.com', 'lightinthebox',
        'amazon.com', 'amazon.co.uk', 'amazon.de', 'amazon.fr',
        'amazon.it', 'amazon.es', 'ebay.com', 'allegro.pl',
        'gearbest.com', 'tomtop.com',
    ],
    # ----- Gambling / betting -----
    'gambling': [
        '1xbet', 'bet365', 'betway', 'melbet', 'pin-up',
        'mostbet', 'vulkan', 'betwinner', '22bet', 'parimatch',
    ],
    # ----- Dating / adult -----
    'dating': [
        'tinder', 'badoo', 'chaturbate', 'onlyfans',
        'adultfriendfinder', 'ashleymadison', 'flirt',
    ],
    # ----- Adsterra (fallback) -----
    'adsterra': [
        'adsterra.com', 'adsterra.net', 'adsterra.org',
        'www.adsterra.com', 'pixel.adsterra.com',
    ],
    # ----- Google AdSense -----
    'google_adsense': [
        'googlesyndication', 'doubleclick',
        'googleadservices', 'google.com/pagead',
    ],
    # ----- Native ads -----
    'mgid': ['mgid.com'],
    'taboola': ['taboola.com'],
    'outbrain': ['outbrain.com', 'outbrain.net'],
    'revcontent': ['revcontent.com'],
    # ----- Pop networks -----
    'popads': ['popads.net', 'popcash.net'],
    # ----- Other -----
    'media_net': ['media.net', 'media-net'],
    'ezoic': ['ezoic.com', 'ezojs.com', 'ezoic.net'],
    # ----- Known bot-detection dummy redirects (NOT revenue) -----
    'bot_detected': ['google.com/', 'about:blank', 'google.co/'],
}


# ============================================================
# Android Device Profiles
# ============================================================
ANDROID_DEVICES = [
    {
        'name': 'Pixel 8',
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
        'viewport': (412, 915),
        'device_scale_factor': 2.625,
        'platform': 'Linux armv8l',
        'webgl_vendor': 'Google Inc. (ARM)',
        'webgl_renderer': 'ANGLE (ARM, Mali-G715, OpenGL ES 3.2)',
        'hardware_concurrency': 8,
        'device_memory': 8,
        'max_touch_points': 5,
    },
    {
        'name': 'Samsung Galaxy S23',
        'user_agent': 'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
        'viewport': (360, 800),
        'device_scale_factor': 3.0,
        'platform': 'Linux armv8l',
        'webgl_vendor': 'Google Inc. (Qualcomm)',
        'webgl_renderer': 'ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)',
        'hardware_concurrency': 8,
        'device_memory': 8,
        'max_touch_points': 5,
    },
    {
        'name': 'Xiaomi Redmi Note 12',
        'user_agent': 'Mozilla/5.0 (Linux; Android 13; 22111317G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36',
        'viewport': (393, 873),
        'device_scale_factor': 2.75,
        'platform': 'Linux armv8l',
        'webgl_vendor': 'Google Inc. (Qualcomm)',
        'webgl_renderer': 'ANGLE (Qualcomm, Adreno (TM) 610, OpenGL ES 3.2)',
        'hardware_concurrency': 8,
        'device_memory': 6,
        'max_touch_points': 5,
    },
    {
        'name': 'OnePlus 11',
        'user_agent': 'Mozilla/5.0 (Linux; Android 13; CPH2449) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
        'viewport': (412, 919),
        'device_scale_factor': 3.0,
        'platform': 'Linux armv8l',
        'webgl_vendor': 'Google Inc. (Qualcomm)',
        'webgl_renderer': 'ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)',
        'hardware_concurrency': 8,
        'device_memory': 12,
        'max_touch_points': 5,
    },
]


# ============================================================
# Chrome Version Detection
# ============================================================
def detect_chrome_version():
    candidates = [
        os.environ.get('CHROME_BIN', ''),
        '/opt/hostedtoolcache/setup-chrome/chrome/stable/x64/chrome',
        '/usr/local/bin/google-chrome',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/opt/google/chrome/chrome',
    ]
    for chrome_bin in candidates:
        if not chrome_bin or not os.path.exists(chrome_bin):
            continue
        try:
            result = subprocess.run(
                [chrome_bin, '--version'],
                capture_output=True, text=True, timeout=5,
            )
            match = re.search(r'(\d+)\.', result.stdout)
            if match:
                version = int(match.group(1))
                logger.info(f"Detected Chrome version: {version} ({chrome_bin})")
                return version
        except Exception as e:
            logger.debug(f"Failed to detect Chrome at {chrome_bin}: {e}")
    logger.warning("Could not detect Chrome version")
    return None


# ============================================================
# Proxy Auth Extension (Manifest V3)
# ============================================================
def parse_proxy_url(proxy_url):
    if not proxy_url:
        return None
    try:
        scheme = 'http'
        url = proxy_url.strip()
        if '://' in url:
            scheme, url = url.split('://', 1)
        user = pass_ = None
        if '@' in url:
            creds, url = url.rsplit('@', 1)
            if ':' in creds:
                user, pass_ = creds.split(':', 1)
        if ':' in url:
            host, port = url.rsplit(':', 1)
        else:
            return None
        return {'scheme': scheme, 'host': host, 'port': port,
                'user': user, 'pass': pass_}
    except Exception as e:
        logger.error(f"Failed to parse proxy '{proxy_url}': {e}")
        return None


def create_proxy_auth_extension(proxy_host, proxy_port, proxy_user, proxy_pass, scheme='http'):
    manifest_json = """
    {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Chrome Proxy Auth",
        "permissions": ["proxy", "storage", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "120.0.0"
    }
    """
    background_js = """
    const config = {
        mode: "fixed_servers",
        rules: {
            singleProxy: {scheme: "%s", host: "%s", port: parseInt(%s)},
            bypassList: ["localhost", "127.0.0.1"]
        }
    };
    chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});
    chrome.webRequest.onAuthRequired.addListener(
        function(details, callback) {
            callback({authCredentials: {username: "%s", password: "%s"}});
        },
        {urls: ["<all_urls>"]},
        ['asyncBlocking']
    );
    """ % (scheme, proxy_host, proxy_port, proxy_user, proxy_pass)

    ext_dir = tempfile.mkdtemp(prefix='proxy_auth_ext_')
    manifest_path = os.path.join(ext_dir, 'manifest.json')
    background_path = os.path.join(ext_dir, 'background.js')
    with open(manifest_path, 'w') as f:
        f.write(manifest_json)
    with open(background_path, 'w') as f:
        f.write(background_js)
    logger.info(f"Created proxy auth extension (MV3) at: {ext_dir}")
    return ext_dir


# ============================================================
# Bezier Mouse
# ============================================================
class BezierMouse:
    @staticmethod
    def bezier_points(x1, y1, x2, y2, steps=25):
        cx1 = x1 + (x2 - x1) * random.uniform(0.2, 0.4) + random.randint(-50, 50)
        cy1 = y1 + (y2 - y1) * random.uniform(0.2, 0.4) + random.randint(-50, 50)
        cx2 = x1 + (x2 - x1) * random.uniform(0.6, 0.8) + random.randint(-50, 50)
        cy2 = y1 + (y2 - y1) * random.uniform(0.6, 0.8) + random.randint(-50, 50)
        points = []
        for i in range(steps + 1):
            t = i / steps
            x = ((1 - t) ** 3 * x1 + 3 * (1 - t) ** 2 * t * cx1
                 + 3 * (1 - t) * t ** 2 * cx2 + t ** 3 * x2)
            y = ((1 - t) ** 3 * y1 + 3 * (1 - t) ** 2 * t * cy1
                 + 3 * (1 - t) * t ** 2 * cy2 + t ** 3 * y2)
            points.append((int(x), int(y)))
        return points


# ============================================================
# CDP Spoofer
# ============================================================
class CDPSpoofer:
    @classmethod
    def apply(cls, driver, fp):
        try:
            if fp.get('timezone'):
                driver.execute_cdp_cmd('Emulation.setTimezoneOverride',
                                       {'timezoneId': fp['timezone']})
            if fp.get('locale'):
                driver.execute_cdp_cmd('Emulation.setLocaleOverride',
                                       {'locale': fp['locale']})
            if fp.get('user_agent'):
                driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                    'userAgent': fp['user_agent'],
                    'platform': fp.get('platform', 'Win32'),
                    'acceptLanguage': fp.get('locale', 'en-US'),
                })
            if fp.get('viewport'):
                vw, vh = fp['viewport']
                is_mobile = fp.get('is_mobile', False)
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                    'width': vw, 'height': vh,
                    'deviceScaleFactor': fp.get('device_scale_factor', 1),
                    'mobile': is_mobile,
                    'screenWidth': vw, 'screenHeight': vh,
                })
            if fp.get('is_mobile'):
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {
                    'enabled': True,
                    'maxTouchPoints': fp.get('max_touch_points', 5),
                })
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
                                   {'source': cls._script(fp)})
            logger.info(f"CDP spoofed: {fp.get('device_name', 'Unknown')} | "
                        f"{fp.get('timezone')} | {fp.get('locale')}")
        except Exception as e:
            logger.warning(f"CDP spoofing partial: {e}")

    @staticmethod
    def _script(fp):
        wv = fp.get('webgl_vendor', '')
        wr = fp.get('webgl_renderer', '')
        hw = fp.get('hardware_concurrency', 8)
        dm = fp.get('device_memory', 8)
        langs = fp.get('langs', ['en-US', 'en'])
        platform = fp.get('platform', 'Win32')
        vendor = fp.get('vendor', 'Google Inc.')
        is_mobile = fp.get('is_mobile', False)
        max_touch = fp.get('max_touch_points', 5) if is_mobile else 0

        return f"""
        (() => {{
            Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
            Object.defineProperty(navigator, 'platform', {{get: () => '{platform}'}});
            Object.defineProperty(navigator, 'vendor', {{get: () => '{vendor}'}});
            Object.defineProperty(navigator, 'language', {{get: () => '{langs[0]}'}});
            Object.defineProperty(navigator, 'languages', {{get: () => {json.dumps(langs)}}});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hw}}});
            Object.defineProperty(navigator, 'deviceMemory', {{get: () => {dm}}});
            Object.defineProperty(navigator, 'maxTouchPoints', {{get: () => {max_touch}}});
            Object.defineProperty(navigator, 'plugins', {{
                get: () => [
                    {{name: 'PDF Viewer', filename: 'internal-pdf-viewer'}},
                    {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'}},
                    {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'}},
                ],
            }});
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {{
                if (p === 37445) return '{wv}';
                if (p === 37446) return '{wr}';
                return getParameter.call(this, p);
            }};
            if (window.WebGL2RenderingContext) {{
                const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(p) {{
                    if (p === 37445) return '{wv}';
                    if (p === 37446) return '{wr}';
                    return getParameter2.call(this, p);
                }};
            }}
            const toDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function() {{
                const ctx = this.getContext('2d');
                if (ctx) {{
                    try {{
                        const imgData = ctx.getImageData(0, 0, this.width, this.height);
                        for (let i = 0; i < imgData.data.length; i += 100) {{
                            imgData.data[i] = imgData.data[i] ^ 1;
                        }}
                        ctx.putImageData(imgData, 0, 0);
                    }} catch(e) {{}}
                }}
                return toDataURL.apply(this, arguments);
            }};
            if (window.AudioContext) {{
                const origGetFloatFrequencyData = AnalyserNode.prototype.getFloatFrequencyData;
                AnalyserNode.prototype.getFloatFrequencyData = function(array) {{
                    origGetFloatFrequencyData.call(this, array);
                    for (let i = 0; i < array.length; i++) {{
                        array[i] += Math.random() * 0.0001;
                    }}
                }};
            }}
            window.chrome = {{runtime: {{}}, loadTimes: function() {{}}, csi: function() {{}}, app: {{}}}};
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications'
                    ? Promise.resolve({{state: Notification.permission}})
                    : origQuery(p)
            );
            if (window.RTCPeerConnection) {{
                const origRTC = window.RTCPeerConnection;
                window.RTCPeerConnection = function(...args) {{
                    const pc = new origRTC(...args);
                    const origCreateDataChannel = pc.createDataChannel;
                    pc.createDataChannel = function(...args) {{
                        return origCreateDataChannel.apply(pc, args);
                    }};
                    return pc;
                }};
                window.RTCPeerConnection.prototype = origRTC.prototype;
            }}
        }})();
        """


# ============================================================
# Resource Blocker
# ============================================================
class ResourceBlocker:
    BLOCK_ALWAYS = [
        'google-analytics.com', 'googletagmanager.com',
        'facebook.net', 'hotjar.com', 'clarity.ms',
    ]

    @classmethod
    def setup(cls, driver, mode='balanced'):
        if mode == 'off':
            return
        try:
            driver.execute_cdp_cmd('Network.enable', {})
            blocked = [f'*{d}*' for d in cls.BLOCK_ALWAYS]
            if mode == 'aggressive':
                blocked += [
                    '*.jpg*', '*.jpeg*', '*.png*', '*.gif*', '*.webp*',
                    '*.svg*', '*.ico*', '*.mp4*', '*.webm*', '*.mp3*',
                    '*.woff*', '*.woff2*', '*.ttf*', '*.otf*', '*.css*',
                ]
            driver.execute_cdp_cmd('Network.setBlockedURLs', {'urls': blocked})
            logger.info(f"Resource blocking: {mode}")
        except Exception as e:
            logger.debug(f"Resource blocking failed: {e}")


# ============================================================
# BrowserStack Client (cloud) + Click verification
# ============================================================
class BrowserStackClient:
    AD_SELECTORS = [
        'ins.adsbygoogle',
        'iframe[src*="doubleclick"]',
        'iframe[src*="googlesyndication"]',
        'a[href*="adsterra"]',
        'a[href*="monetag"]',
        'a[href*="propellerads"]',
        'a[href*="effectivecpmnetwork"]',
        'a[href*="highperformanceformat"]',
        'a[href*="highrevenueformat"]',
        'a[href*="profitableratecpmnetwork"]',
        'a[href*="nn125"]',
        'a[href*="omg10"]',
        'a[href*="uplcm"]',
        'div[id^="container-"]',
        'div[id^="ad-"]',
        'div[class*="advertisement"]',
    ]

    FORM_SELECTORS = [
        'form[action*="contact"]', 'form[action*="subscribe"]',
        'form[action*="newsletter"]', 'form[id*="contact"]',
        'form[id*="subscribe"]', 'form[class*="contact"]',
        'form[class*="subscribe"]', 'form[class*="newsletter"]',
        '.contact-form', '.subscribe-form', '.newsletter-form',
        '#contact-form', '#subscribe-form',
    ]

    CAPTCHA_SIGNALS = [
        'google.com/sorry', 'consent.google.com', 'captcha',
        'unusual traffic', 'detected unusual', 'our systems have detected',
        'please show you', '/sorry/index', 'are you a robot',
    ]

    DESKTOP_VIEWPORTS = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1600, 900)]
    LOCALES = ['en-US', 'en-GB', 'fr-FR', 'de-DE', 'es-ES']
    TIMEZONES = {
        'en-US': 'America/New_York',
        'en-GB': 'Europe/London',
        'fr-FR': 'Europe/Paris',
        'de-DE': 'Europe/Berlin',
        'es-ES': 'Europe/Madrid',
    }

    def __init__(self, username=None, access_key=None):
        self.username = username or os.environ.get('BROWSERSTACK_USERNAME', '').strip()
        self.access_key = access_key or os.environ.get('BROWSERSTACK_ACCESS_KEY', '').strip()
        self.driver = None
        self.current_locale = 'en-US'
        self.is_mobile = False

    def _random_fp(self, mobile=False):
        locale = random.choice(self.LOCALES)
        if mobile:
            device = random.choice(ANDROID_DEVICES)
            return {
                'locale': locale,
                'timezone': self.TIMEZONES[locale],
                'platform': device['platform'],
                'vendor': 'Google Inc.',
                'user_agent': device['user_agent'],
                'viewport': device['viewport'],
                'device_scale_factor': device['device_scale_factor'],
                'webgl_vendor': device['webgl_vendor'],
                'webgl_renderer': device['webgl_renderer'],
                'hardware_concurrency': device['hardware_concurrency'],
                'device_memory': device['device_memory'],
                'langs': [locale, locale.split('-')[0]],
                'is_mobile': True,
                'device_name': device['name'],
                'max_touch_points': device['max_touch_points'],
            }
        return {
            'locale': locale,
            'timezone': self.TIMEZONES[locale],
            'platform': 'Win32',
            'vendor': 'Google Inc.',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'viewport': random.choice(self.DESKTOP_VIEWPORTS),
            'device_scale_factor': 1,
            'webgl_vendor': 'Google Inc. (NVIDIA)',
            'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)',
            'hardware_concurrency': random.choice([4, 8, 12, 16]),
            'device_memory': random.choice([4, 8, 16]),
            'langs': [locale, locale.split('-')[0]],
            'is_mobile': False,
            'device_name': 'Desktop',
            'max_touch_points': 0,
        }

    @staticmethod
    def _identify_network(domain):
        """Map a destination domain to an ad network name."""
        if not domain:
            return None
        d = domain.lower()
        if d == 'www.google.com' or d == 'google.com':
            return 'bot_detected'
        for network, keywords in REVENUE_NETWORKS.items():
            if any(kw in d for kw in keywords):
                return network
        return 'unknown'

    def verify_click_result(self, pre_handles, pre_url, timeout=8):
        """After a click, check: new tab? destination? network? HTTP status?"""
        result = {
            'new_tab': False,
            'destination_url': None,
            'destination_domain': None,
            'http_status': None,
            'revenue_network': None,
            'is_bot_dummy': False,
        }

        start = time.time()
        new_handles = []
        while time.time() - start < timeout:
            try:
                post_handles = self.driver.window_handles
            except Exception:
                time.sleep(0.5)
                continue
            new_handles = [h for h in post_handles if h not in pre_handles]
            if new_handles:
                break
            time.sleep(0.5)

        original_handle = pre_handles[0] if pre_handles else None

        if new_handles:
            result['new_tab'] = True
            try:
                self.driver.switch_to.window(new_handles[0])
                time.sleep(random.uniform(1.5, 3.0))
                result['destination_url'] = self.driver.current_url
                parsed = urlparse(result['destination_url'])
                result['destination_domain'] = parsed.netloc
                result['revenue_network'] = self._identify_network(parsed.netloc)
                if result['revenue_network'] == 'bot_detected':
                    result['is_bot_dummy'] = True

                try:
                    status = self.driver.execute_script(
                        "return (window.performance && "
                        "window.performance.getEntriesByType('navigation')[0] "
                        "&& window.performance.getEntriesByType('navigation')[0].responseStatus) "
                        "|| null;"
                    )
                    result['http_status'] = status
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"[CLICK-VERIFY] read failed: {e}")
            finally:
                try:
                    if original_handle:
                        self.driver.switch_to.window(original_handle)
                except Exception:
                    pass
        else:
            try:
                current_url = self.driver.current_url
                if current_url and current_url != pre_url:
                    result['destination_url'] = current_url
                    parsed = urlparse(current_url)
                    result['destination_domain'] = parsed.netloc
                    result['revenue_network'] = self._identify_network(parsed.netloc)
            except Exception:
                pass

        return result

    # ---------- BrowserStack cloud session ----------
    def start_session(self, url, browser='chrome', os_name='Windows', os_version='11',
                      proxy=None, use_local=False, fp=None, record_video=False,
                      block_resources='balanced', mobile=False):
        if not (self.username and self.access_key):
            raise RuntimeError("BrowserStack credentials missing")

        fp = fp or self._random_fp(mobile=mobile)
        self.is_mobile = fp.get('is_mobile', False)

        options = Options()
        options.set_capability('browserName', browser)
        options.set_capability('browserVersion', 'latest')

        bstack_opts = {
            'os': os_name, 'osVersion': os_version, 'local': use_local,
            'projectName': 'Traffic Bot',
            'buildName': f'Traffic Bot - {os_name} {os_version}',
            'sessionName': f'Visit {url[:60]}',
            'debug': True, 'networkLogs': True, 'consoleLogs': 'info',
            'userName': self.username, 'accessKey': self.access_key,
        }
        if proxy:
            bstack_opts['proxy'] = proxy
        if record_video:
            bstack_opts['video'] = True

        options.set_capability('bstack:options', bstack_opts)
        logger.info(f"BrowserStack: {browser} on {os_name} {os_version} | "
                    f"{fp.get('device_name')}")

        self.driver = webdriver.Remote(
            command_executor='https://hub-cloud.browserstack.com/wd/hub',
            options=options,
        )
        self.driver.set_page_load_timeout(90)
        self.driver.set_script_timeout(30)

        CDPSpoofer.apply(self.driver, fp)
        ResourceBlocker.setup(self.driver, mode=block_resources)

        self.current_locale = fp.get('locale', 'en-US')
        if fp.get('viewport'):
            try:
                vw, vh = fp['viewport']
                self.driver.set_window_size(vw, vh)
            except Exception:
                pass

        self.driver.get(url)
        self.wait_for_page_load(timeout=30)
        return self.driver

    # ---------- Common actions ----------
    def fill_form(self, form_data=None, form_selectors=None):
        selectors = form_selectors or self.FORM_SELECTORS
        filled = 0
        for fs in selectors:
            try:
                forms = self.driver.find_elements(By.CSS_SELECTOR, fs)
                for form in forms[:2]:
                    if self._fill_single_form(form, form_data):
                        filled += 1
                        logger.info(f"Filled form: {fs}")
            except Exception:
                continue
        return filled

    def _fill_single_form(self, form, form_data=None):
        data = form_data or self._generate_form_data()
        filled_any = False
        for field_sel, value in data.items():
            try:
                field = form.find_element(By.CSS_SELECTOR, field_sel)
                if not (field.is_displayed() and field.is_enabled()):
                    continue
                field.clear()
                self.human_type(field, value)
                filled_any = True
            except (NoSuchElementException, ElementNotInteractableException):
                continue
        if not filled_any:
            return False
        try:
            submit = form.find_element(
                By.CSS_SELECTOR, 'button[type="submit"], input[type="submit"]'
            )
            if submit.is_displayed() and submit.is_enabled():
                submit.click()
                self.random_sleep(2.0, 4.0)
                return True
        except Exception:
            pass
        return filled_any

    def human_type(self, element, text, min_delay=0.04, max_delay=0.20):
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(min_delay, max_delay))
            if random.random() < 0.02:
                element.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.1, 0.3))
                element.send_keys(char)

    def bezier_mouse_move(self, target):
        try:
            loc = target.location
            sz = target.size
            tx = loc['x'] + sz['width'] // 2
            ty = loc['y'] + sz['height'] // 2
            points = BezierMouse.bezier_points(0, 0, tx, ty, steps=20)
            body = self.driver.find_element(By.TAG_NAME, 'body')
            actions = ActionChains(self.driver)
            for x, y in points:
                try:
                    actions.move_to_element_with_offset(body, x, y)
                    actions.pause(random.uniform(0.005, 0.02))
                except Exception:
                    continue
            actions.perform()
            return True
        except Exception:
            return False

    def human_scroll(self, count=5):
        for i in range(count):
            pixels = random.randint(250, 750)
            if i < 2 or i >= count - 1:
                self.driver.execute_script(
                    f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}});"
                )
                self.random_sleep(1.2, 2.5)
            else:
                self.driver.execute_script(
                    f"window.scrollBy({{top: {pixels * 2}, behavior: 'smooth'}});"
                )
                self.random_sleep(0.6, 1.2)

    def random_sleep(self, min_s, max_s):
        time.sleep(random.uniform(min_s, max_s))

    def _safe_click(self, element, max_retries=3):
        for _ in range(max_retries):
            try:
                if not element.is_displayed():
                    return False
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    element,
                )
                self.random_sleep(0.5, 1.2)
                try:
                    element.click()
                except ElementClickInterceptedException:
                    self.driver.execute_script("arguments[0].click();", element)
                return True
            except (StaleElementReferenceException, Exception):
                time.sleep(0.5)
        return False

    def _close_extra_tabs(self):
        try:
            if len(self.driver.window_handles) > 1:
                main = self.driver.window_handles[0]
                for handle in self.driver.window_handles[1:]:
                    self.driver.switch_to.window(handle)
                    self.random_sleep(1.5, 3.0)
                    self.driver.close()
                self.driver.switch_to.window(main)
        except Exception:
            pass

    def is_captcha_page(self):
        try:
            url = self.driver.current_url.lower()
            title = (self.driver.title or '').lower()
            try:
                source = self.driver.page_source.lower()[:5000]
            except Exception:
                source = ''
            for sig in self.CAPTCHA_SIGNALS:
                if sig in url or sig in title or sig in source:
                    return True
            return False
        except Exception:
            return False

    def wait_for_page_load(self, timeout=30):
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass

    def _generate_form_data(self):
        locales_data = {
            'en-US': (['John', 'Jane', 'Alex'], ['Smith', 'Brown'],
                      ['Great content!', 'Very helpful.'], ['Inquiry', 'Question']),
            'fr-FR': (['Pierre', 'Marie'], ['Dupont', 'Martin'],
                      ['Excellent!', 'Tres informatif.'], ['Demande', 'Question']),
            'de-DE': (['Hans', 'Anna'], ['Muller', 'Schmidt'],
                      ['Toller Inhalt!', 'Sehr informativ.'], ['Anfrage', 'Frage']),
        }
        locale = self.current_locale if self.current_locale in locales_data else 'en-US'
        fnames, lnames, msgs, subs = locales_data[locale]
        return {
            'input[type="email"]': self._fake_email(),
            'input[name*="name"]': f"{random.choice(fnames)} {random.choice(lnames)}",
            'textarea[name*="message"]': random.choice(msgs),
            'input[name*="subject"]': random.choice(subs),
            'input[type="tel"]': self._fake_phone(),
        }

    def _fake_email(self):
        domains = ['gmail.com', 'yahoo.com', 'outlook.com']
        names = ['john', 'jane', 'alex', 'sarah', 'mike']
        return f"{random.choice(names)}{random.randint(100, 9999)}@{random.choice(domains)}"

    def _fake_phone(self):
        countries = ['+212', '+33', '+44', '+1', '+49']
        return f"{random.choice(countries)}{random.randint(600000000, 999999999)}"

    def take_screenshot(self, filename=None):
        if not filename:
            filename = f"screenshot_{int(time.time())}.png"
        try:
            self.driver.save_screenshot(filename)
            return filename
        except Exception:
            return None

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None


# ============================================================
# Local Browser Client — anti-detection, proxy auth (MV3), mobile emulation
# ============================================================
class LocalBrowserClient(BrowserStackClient):
    """Local Chrome with anti-detection, proxy auth (MV3), and mobile emulation."""

    def __init__(self):
        super().__init__()
        self._profile_dir = None
        self._extension_path = None

    # ------------------------------------------------------------
    # Fast proxy pre-flight (HTTP check before browser launch)
    # ------------------------------------------------------------
    def _preflight_proxy_check(self, proxy_url, timeout=15):
        """
        Verify the proxy responds via plain HTTP (no browser).
        Fails in ≤15s instead of 50s of browser timeouts.
        """
        try:
            proxies = {'http': proxy_url, 'https': proxy_url}
            for test_url in (
                'https://api.ipify.org?format=json',
                'https://httpbin.org/ip',
            ):
                try:
                    r = requests.get(
                        test_url, proxies=proxies, timeout=timeout,
                    )
                    if r.status_code == 200:
                        body = r.text[:120]
                        logger.info(f"[PREFLIGHT] ✅ Proxy OK via {test_url}: {body}")
                        return True
                except Exception as e:
                    logger.debug(f"[PREFLIGHT] {test_url} failed: {type(e).__name__}")
                    continue
            logger.error("[PREFLIGHT] ❌ Proxy failed both health checks")
        except Exception as e:
            logger.error(f"[PREFLIGHT] Proxy check crashed: {e}")
        return False

    # ------------------------------------------------------------
    # Browser-based IP check (fallback, called after preflight passes)
    # ------------------------------------------------------------
    def _get_public_ip(self):
        """Return public IP via multiple services with short timeouts (max 15s)."""
        services = [
            ('https://api.ipify.org?format=json', 'ip'),
            ('https://ipinfo.io/json', 'ip'),
            ('https://ifconfig.me/ip', None),  # plain text
        ]
        original_timeout = None
        try:
            original_timeout = 25
            self.driver.set_page_load_timeout(5)
        except Exception:
            pass

        try:
            for url, key in services:
                try:
                    logger.info(f"[IP-CHECK] Trying {url}")
                    self.driver.get(url)
                    time.sleep(0.8)
                    body = self.driver.find_element(By.TAG_NAME, 'body').text.strip()
                    if not body:
                        continue

                    if key:
                        try:
                            data = json.loads(body)
                            ip = (data.get(key) or data.get('origin') or '').strip()
                            if ip and '.' in ip:
                                return ip.split(',')[0].strip()
                        except json.JSONDecodeError:
                            pass

                    # Plain text fallback
                    if body.count('.') == 3 or body.count(':') >= 2:
                        return body.split()[0].strip()
                except Exception as e:
                    logger.warning(f"[IP-CHECK] {url} failed: {type(e).__name__}")
                    continue
        finally:
            if original_timeout:
                try:
                    self.driver.set_page_load_timeout(original_timeout)
                except Exception:
                    pass
        return None

    def _verify_proxy_effective(self, proxy_url):
        if self.driver is None:
            logger.error("[PROXY-CHECK] Driver is None")
            return False

        public_ip = self._get_public_ip()
        logger.info(f"[PROXY-CHECK] Public IP via browser: {public_ip}")
        if not public_ip:
            logger.error("[PROXY-CHECK] Could not read public IP")
            return False

        try:
            r = requests.get(
                f'http://ip-api.com/json/{public_ip}'
                f'?fields=status,countryCode,isp,org,as,hosting,proxy',
                timeout=6,
            )
            info = r.json()
        except Exception as e:
            logger.warning(f"[PROXY-CHECK] ip-api lookup failed: {e}")
            info = {}

        is_hosting = info.get('hosting', False)
        is_proxy = info.get('proxy', False)
        isp = (info.get('isp') or '').lower()
        org = (info.get('org') or '').lower()
        country = info.get('countryCode', '??')

        logger.info(
            f"[PROXY-CHECK] IP={public_ip} | country={country} | "
            f"hosting={is_hosting} | proxy={is_proxy} | isp={isp[:40]}"
        )

        if any(k in isp for k in AZURE_DATACENTER_KEYWORDS) or \
           any(k in org for k in AZURE_DATACENTER_KEYWORDS):
            logger.error(
                f"❌ PROXY BYPASSED — traffic exits from GitHub runner "
                f"({isp[:40]}). Extension auth failed. Aborting."
            )
            return False

        if is_hosting and not is_proxy:
            logger.warning(
                "⚠️  PROXY is DATACENTER (hosting=true). "
                "Ad networks may block. Continuing but expect low yield."
            )

        logger.info(f"✅ Proxy effective: {country} via {isp[:40]}")
        return True

    def start_session(self, url, headless=True, proxy=None, fp=None,
                      block_resources='balanced', mobile=True):
        fp = fp or self._random_fp(mobile=mobile)
        self.is_mobile = fp.get('is_mobile', False)

        # ---- PRE-FLIGHT: fail fast if proxy is down (≤15s) ----
        if proxy:
            logger.info("[PREFLIGHT] Verifying proxy health before Chrome launch...")
            if not self._preflight_proxy_check(proxy, timeout=15):
                raise RuntimeError(
                    "Proxy failed pre-flight check — likely down or blocked. "
                    "Aborting before wasting Actions minutes."
                )
            logger.info("[PREFLIGHT] ✅ Proxy is reachable, proceeding to browser")

        options = Options()
        vw, vh = fp.get('viewport', (412, 915))
        locale = fp.get('locale', 'en-US')

        display_present = os.environ.get('DISPLAY') is not None
        if headless and not display_present:
            options.add_argument('--headless=new')
            logger.info("Using --headless=new (no DISPLAY detected)")
        else:
            logger.info(f"Using xvfb display (DISPLAY={os.environ.get('DISPLAY')})")

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-setuid-sandbox')
        options.add_argument(f'--window-size={vw},{vh}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-infobars')
        options.add_argument('--disable-notifications')
        options.add_argument(f'--lang={locale}')
        options.add_argument('--log-level=3')
        options.add_argument(f'--user-agent={fp.get("user_agent", "")}')

        # Speed-up flags
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-client-side-phishing-detection')
        options.add_argument('--disable-default-apps')
        options.add_argument('--disable-hang-monitor')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--disable-prompt-on-repost')
        options.add_argument('--disable-sync')
        options.add_argument('--metrics-recording-only')
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        options.add_argument('--safebrowsing-disable-auto-update')
        options.add_argument('--password-store=basic')
        options.add_argument('--use-mock-keychain')
        options.add_argument(
            '--disable-features='
            'DialMediaRouteProvider,Translate,OptimizationHints,'
            'MediaRouter,InterestFeedContentSuggestions,'
            'CalculateNativeWinOcclusion'
        )

        # Proxy (MV3)
        if proxy:
            parsed = parse_proxy_url(proxy)
            if parsed:
                host = parsed['host']
                port = parsed['port']
                user = parsed['user']
                pass_ = parsed['pass']
                scheme = parsed['scheme']

                if user and pass_:
                    try:
                        self._extension_path = create_proxy_auth_extension(
                            proxy_host=host, proxy_port=port,
                            proxy_user=user, proxy_pass=pass_, scheme=scheme,
                        )
                        options.add_argument(
                            f'--load-extension={self._extension_path}'
                        )
                        options.add_argument(
                            f'--disable-extensions-except={self._extension_path}'
                        )
                        options.add_argument(
                            '--disable-features=DisableLoadExtensionCommandLineSwitch'
                        )
                        logger.info(
                            f"Proxy auth extension (MV3) loaded: {scheme}://{host}:{port}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Extension failed: {e}. Falling back to --proxy-server"
                        )
                        options.add_argument(
                            f'--proxy-server={scheme}://{host}:{port}'
                        )
                else:
                    options.add_argument(
                        f'--proxy-server={scheme}://{host}:{port}'
                    )
                    logger.info(f"Proxy without auth: {scheme}://{host}:{port}")

        # ---- FORCE uc.Chrome to use the setup-chrome binary ----
        # Prevents the stale uc cache from loading an old Chrome 155
        chrome_bin = os.environ.get('CHROME_BIN', '')
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin
            logger.info(f"[UC] Forced binary_location: {chrome_bin}")

        # Driver creation
        driver = None
        chrome_version = detect_chrome_version()

        try:
            import undetected_chromedriver as uc
            logger.info(
                f"Using undetected-chromedriver (detected Chrome: {chrome_version})"
            )
            try:
                if chrome_version:
                    driver = uc.Chrome(
                        options=copy.deepcopy(options),
                        use_subprocess=True,
                        version_main=chrome_version,
                    )
                else:
                    driver = uc.Chrome(
                        options=copy.deepcopy(options), use_subprocess=True,
                    )
                logger.info("undetected-chromedriver started successfully")
            except Exception as e:
                logger.warning(
                    f"uc.Chrome(version_main={chrome_version}) failed: {e}"
                )
                try:
                    logger.info("Retrying uc.Chrome with fresh options")
                    driver = uc.Chrome(
                        options=copy.deepcopy(options), use_subprocess=True,
                    )
                    logger.info("undetected-chromedriver started (fallback)")
                except Exception as e2:
                    logger.warning(f"uc.Chrome fallback failed: {e2}")
                    driver = None
        except ImportError:
            logger.warning("undetected-chromedriver not installed")
            driver = None

        if driver is None:
            logger.info("Using plain Chrome with system ChromeDriver")
            chrome_bin = os.environ.get('CHROME_BIN', '/usr/local/bin/google-chrome')
            if os.path.exists(chrome_bin):
                options.binary_location = chrome_bin

            self._profile_dir = tempfile.mkdtemp(prefix='chrome_profile_')
            options.add_argument(f'--user-data-dir={self._profile_dir}')

            cd_path = os.environ.get('CHROMEDRIVER_PATH', '/usr/local/bin/chromedriver')
            if os.path.exists(cd_path):
                service = Service(executable_path=cd_path)
                driver = webdriver.Chrome(service=service, options=options)
            else:
                driver = webdriver.Chrome(options=options)

        self.driver = driver

        CDPSpoofer.apply(self.driver, fp)
        ResourceBlocker.setup(self.driver, mode=block_resources)

        try:
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                '''
            })
        except Exception:
            pass

        self.current_locale = locale
        self.driver.set_page_load_timeout(60)
        self.driver.set_script_timeout(30)

        if proxy:
            self.driver.set_page_load_timeout(25)
            try:
                ok = self._verify_proxy_effective(proxy)
            except Exception as e:
                logger.error(f"Proxy verification raised: {e}")
                ok = False
            finally:
                try:
                    self.driver.set_page_load_timeout(60)
                except Exception:
                    pass

            if not ok:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
                raise RuntimeError(
                    "Proxy not effective — aborted to save quota. "
                    "Check that Chrome extensions load in headless mode."
                )

        self.driver.get(url)
        self.wait_for_page_load(timeout=30)
        return self.driver
