"""Small fail-closed GitHub REST client with complete pagination checks."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

API_BASE = "https://api.github.com"
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


class GitHubApiError(RuntimeError):
    """An API call failed or returned an incomplete/invalid response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubApi:
    """Call the GitHub REST API without depending on local `gh` authentication."""

    def __init__(
        self,
        token: str | Callable[[], str],
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        api_base: str = API_BASE,
    ) -> None:
        if isinstance(token, str) and (not token or any(character.isspace() for character in token)):
            raise ValueError("A non-empty GitHub token is required.")
        self._token = token
        self._opener = opener
        self._api_base = api_base.rstrip("/")

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, int, Any]:
        """Return parsed JSON and status; errors never become empty successful results."""
        url = path if path.startswith("https://") else f"{self._api_base}/{path.lstrip('/')}"
        url_parts = urllib.parse.urlsplit(url)
        base_parts = urllib.parse.urlsplit(self._api_base)
        if url_parts.scheme != "https" or url_parts.netloc != base_parts.netloc:
            raise GitHubApiError("Refusing to send the credential outside the configured API host.")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        token = self._token() if callable(self._token) else self._token
        if not token or any(character.isspace() for character in token):
            raise GitHubApiError("Token provider returned no usable GitHub credential.")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2026-03-10",
                "Content-Type": "application/json",
                "User-Agent": "vexel-ci-actions",
            },
        )
        try:
            response = self._opener(request, timeout=20)
            with response:
                status = response.status
                raw = response.read()
                headers = response.headers
        except urllib.error.HTTPError as error:
            raw = error.read(512)
            if error.code == 404:
                raise GitHubApiError(f"GitHub API returned 404 for {method} {url}.", status=404) from error
            detail = raw.decode("utf-8", errors="replace").replace("\n", " ")[:300]
            raise GitHubApiError(
                f"GitHub API returned {error.code} for {method} {url}: {detail}", status=error.code
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubApiError(f"GitHub API request failed for {method} {url}: {error}") from error

        if status < 200 or status >= 300:
            raise GitHubApiError(f"GitHub API returned unexpected status {status} for {method} {url}.")
        if not raw:
            return None, status, headers
        try:
            return json.loads(raw), status, headers
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError(f"GitHub API returned invalid JSON for {method} {url}.") from error

    def get(self, path: str) -> Any:
        """Read one API resource."""
        result, _, _ = self.request("GET", path)
        return result

    def post(self, path: str, body: dict[str, Any]) -> Any:
        """Create or update one API resource using a caller-supplied scoped route."""
        result, _, _ = self.request("POST", path, body)
        return result

    def delete(self, path: str, *, allow_missing: bool = False) -> Any:
        """Delete one API resource; an optional 404 is safe only for idempotent label removal."""
        try:
            result, _, _ = self.request("DELETE", path)
            return result
        except GitHubApiError as error:
            if allow_missing and error.status == 404:
                return None
            raise

    def paginated(
        self, path: str, collection_key: str, *, identity_key: str = "id"
    ) -> list[dict[str, Any]]:
        """Read every page and reject truncation, duplicate identities, or foreign URLs."""
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "per_page"]
        query.append(("per_page", "100"))
        first_url = urllib.parse.urlunsplit(("", "", parsed.path, urllib.parse.urlencode(query), ""))
        next_url: str | None = first_url
        items: list[dict[str, Any]] = []
        expected_count: int | None = None
        seen_urls: set[str] = set()

        while next_url is not None:
            absolute_url = next_url if next_url.startswith("https://") else f"{self._api_base}/{next_url.lstrip('/')}"
            if absolute_url in seen_urls:
                raise GitHubApiError("GitHub pagination repeated a page URL.")
            seen_urls.add(absolute_url)
            value, _, headers = self.request("GET", absolute_url)
            if not isinstance(value, dict) or not isinstance(value.get(collection_key), list):
                raise GitHubApiError(f"GitHub response omitted the '{collection_key}' array.")
            if not isinstance(value.get("total_count"), int):
                raise GitHubApiError("GitHub paginated response omitted integer total_count.")
            if expected_count is None:
                expected_count = value["total_count"]
            elif value["total_count"] != expected_count:
                raise GitHubApiError("GitHub total_count changed during pagination.")

            page_items = value[collection_key]
            if not all(isinstance(item, dict) for item in page_items):
                raise GitHubApiError("GitHub page contains a non-object item.")
            items.extend(page_items)
            next_url = self._next_link(headers.get("Link"), absolute_url)

        if expected_count is None or len(items) != expected_count:
            raise GitHubApiError(
                f"GitHub pagination was incomplete: expected {expected_count}, received {len(items)}."
            )
        identities = [item.get(identity_key) for item in items]
        if any(identity is None for identity in identities) or len(identities) != len(set(identities)):
            raise GitHubApiError(
                f"GitHub pagination returned a missing or duplicate '{identity_key}' identity."
            )
        return items

    def _next_link(self, link_header: str | None, current_url: str) -> str | None:
        candidate = None
        if link_header:
            for url, relation in _LINK_RE.findall(link_header):
                if relation == "next":
                    candidate = urllib.parse.urljoin(current_url, url)
                    break
        if candidate is None:
            return None
        candidate_parts = urllib.parse.urlsplit(candidate)
        base_parts = urllib.parse.urlsplit(self._api_base)
        if candidate_parts.scheme != "https" or candidate_parts.netloc != base_parts.netloc:
            raise GitHubApiError("GitHub pagination attempted to leave the API host.")
        return candidate
