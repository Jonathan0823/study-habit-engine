"""Bounded search over explicitly trusted hosts; never fetch page contents."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class TavilySourceSearcher:
    """Search the trusted host catalog and return link metadata only.

    The injected HTTP client must have a finite request timeout and must not
    follow arbitrary redirects. `trusted_hosts` is a reviewed exact-host set.
    """

    def __init__(self, *, api_key: str, trusted_hosts: set[str], http_client):
        normalized = {
            host.casefold().rstrip(".")
            for host in trusted_hosts
            if isinstance(host, str) and host.strip()
        }
        if not api_key.strip() or not normalized or len(normalized) > 300:
            raise ValueError("Tavily requires an API key and 1-300 trusted hosts")
        self._api_key = api_key
        self._trusted_hosts = normalized
        self._http_client = http_client

    async def search(self, query: str, *, max_results: int = 5) -> list[dict]:
        """Return only HTTPS results on the configured hosts, without snippets."""
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("search query must contain 1-512 characters")
        if type(max_results) is not int or not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        response = await self._http_client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query.strip(),
                "search_depth": "basic",
                "max_results": max_results,
                "include_domains": sorted(self._trusted_hosts),
                "include_domains_mode": "restrict",
                "include_answer": False,
                "include_raw_content": False,
                "include_published_date": True,
                "include_favicon": False,
                "auto_parameters": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ValueError("Tavily returned an invalid result list")

        safe_results = []
        for result in results:
            if not isinstance(result, dict):
                continue
            url = result.get("url")
            host = trusted_https_host(url, self._trusted_hosts)
            title = result.get("title")
            if host is None or not isinstance(title, str) or not title.strip():
                continue
            publisher = result.get("site_name")
            safe_results.append(
                {
                    "title": title.strip()[:500],
                    "publisher": publisher.strip()[:500]
                    if isinstance(publisher, str) and publisher.strip()
                    else host,
                    "url": url,
                    "published_at": result.get("published_date"),
                }
            )
        return safe_results


def trusted_https_host(url: object, trusted_hosts: set[str]) -> str | None:
    """Return the normalized exact host for a safe trusted HTTPS URL."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not host
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    allowed = {item.casefold().rstrip(".") for item in trusted_hosts if isinstance(item, str)}
    return host if host in allowed else None
