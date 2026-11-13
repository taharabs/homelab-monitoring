"""A small client for the Zabbix JSON-RPC API (tested against Zabbix 7.0 LTS)."""

from __future__ import annotations

import time
from typing import Any

import httpx

# Methods that Zabbix refuses when an authentication token is sent.
_ANONYMOUS = {"apiinfo.version", "user.login"}


class ZabbixAPIError(RuntimeError):
    """Zabbix answered with an error object."""

    def __init__(self, method: str, code: int, message: str, data: str) -> None:
        super().__init__(f"{method}: {message} {data}".strip())
        self.method = method
        self.code = code
        self.data = data


class ZabbixAPI:
    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        url = url.rstrip("/")
        self.endpoint = url if url.endswith("api_jsonrpc.php") else f"{url}/api_jsonrpc.php"
        self._token = token
        self._session = False  # True when the token comes from user.login and must be released
        self._http = http or httpx.Client(timeout=timeout)
        self._next_id = 1

    def call(self, method: str, params: Any = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {} if params is None else params,
            "id": self._next_id,
        }
        self._next_id += 1
        headers = {}
        if self._token and method not in _ANONYMOUS:
            headers["Authorization"] = f"Bearer {self._token}"
        response = self._http.post(self.endpoint, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            error = body["error"]
            raise ZabbixAPIError(
                method, error.get("code", 0), error.get("message", ""), error.get("data", "")
            )
        return body["result"]

    # -- session -----------------------------------------------------------------------

    def version(self) -> str:
        return self.call("apiinfo.version", [])

    def login(self, username: str, password: str) -> None:
        self._token = self.call("user.login", {"username": username, "password": password})
        self._session = True

    def logout(self) -> None:
        if self._session:
            try:
                self.call("user.logout", [])
            finally:
                self._token = None
                self._session = False

    def close(self) -> None:
        self.logout()
        self._http.close()

    def __enter__(self) -> ZabbixAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers used by the commands ----------------------------------------------------

    def wait_until_ready(self, timeout: float = 300, interval: float = 5) -> str:
        """Poll until the API answers (the server creates its database on first start)."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.version()
            except (httpx.HTTPError, ZabbixAPIError, ValueError) as exc:
                last_error = exc
                time.sleep(interval)
        raise TimeoutError(f"Zabbix API not ready after {timeout:.0f}s: {last_error}")

    def item_last_value(self, host: str, key: str) -> str | None:
        """Latest value of an item, or None if it never received data."""
        items = self.call(
            "item.get",
            {
                "output": ["lastvalue", "lastclock"],
                "host": host,
                "filter": {"key_": key},
                "webitems": True,  # without this, web scenario items are left out
            },
        )
        if not items or items[0]["lastclock"] in ("0", 0):
            return None
        return items[0]["lastvalue"]

    def active_problems(self) -> list[dict]:
        """Triggers currently in PROBLEM state on monitored hosts, most severe first."""
        return self.call(
            "trigger.get",
            {
                "output": ["triggerid", "description", "priority", "lastchange"],
                "filter": {"value": 1},
                "monitored": True,
                "active": True,
                "skipDependent": True,
                "selectHosts": ["host"],
                "sortfield": "priority",
                "sortorder": "DESC",
            },
        )
