"""
Send eCourts traffic (and only eCourts traffic) through ECOURTS_PROXY_URL.

eCourts' firewall answers many data-centre IP addresses with "405 Security
Page", so a server hosted in one can't search or poll cases directly. With
ECOURTS_PROXY_URL set (e.g. an Indian residential/ISP proxy, or a small relay
on an office connection), every bharat-courts request goes through it.
Telegram, email, Google etc. keep using the server's own connection.

install() is called once per process at import of the scraper/search code.
"""
import logging

import httpx
from bharat_courts import http as bc_http

from config.settings import settings

logger = logging.getLogger("courtpilot.proxy")

_installed = False


def install() -> None:
    global _installed
    if _installed or not settings.ECOURTS_PROXY_URL:
        return
    proxy = settings.ECOURTS_PROXY_URL

    # Same client as bharat_courts.http.RateLimitedClient._ensure_client (0.5.0), plus the proxy
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._config.timeout,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "en-US,en;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                },
                follow_redirects=True,
                verify=self._ssl_context if self._ssl_context else False,
                proxy=proxy,
            )
        return self._client

    bc_http.RateLimitedClient._ensure_client = _ensure_client
    _installed = True
    logger.info("eCourts requests go through the configured proxy")


def proxy_for_httpx() -> dict:
    """Extra httpx.AsyncClient kwargs for our own direct eCourts requests."""
    return {"proxy": settings.ECOURTS_PROXY_URL} if settings.ECOURTS_PROXY_URL else {}
