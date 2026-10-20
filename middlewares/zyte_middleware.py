# middlewares/zyte_middleware.py
import os
import logging
from scrapy.exceptions import NotConfigured

logger = logging.getLogger(__name__)


class ZyteMiddleware:
    """
    Sets `request.meta['zyte_api']` for requests flagged with `use_zyte=True`.
    Requires `scrapy-zyte-api` to be installed AND enabled in ADDONS for the
    meta to actually be processed. Without the plugin, this middleware is a
    harmless no-op.
    """

    @classmethod
    def from_crawler(cls, crawler):
        key = crawler.settings.get('ZYTE_API_KEY') or os.getenv('ZYTE_API_KEY', '')
        if not key:
            raise NotConfigured("ZYTE_API_KEY missing — ZyteMiddleware disabled")
        return cls(key)

    def __init__(self, api_key):
        self.api_key = api_key
        logger.info(f"ZyteMiddleware enabled (key length: {len(api_key)})")

    def process_request(self, request, spider):
        if request.meta.get('use_zyte'):
            request.meta['zyte_api'] = {
                'httpResponseBody': True,
                'browserHtml': True,
                'javascript': True,
                'actions': [
                    {'action': 'wait', 'waitTimeout': 3},
                    {'action': 'scroll', 'direction': 'down'},
                ],
            }
        return None