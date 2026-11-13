"""The monitoring file: what should exist in Zabbix."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = {"info": 1, "warning": 2, "average": 3, "high": 4, "disaster": 5}
_INTERVAL = re.compile(r"^\d+[smhd]?$")
# Characters that are safe inside a Zabbix item key parameter without quoting.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
_HOST = re.compile(r"^[A-Za-z0-9._ -]+$")


class ConfigError(ValueError):
    """The monitoring file is invalid."""


@dataclass(frozen=True)
class WebCheck:
    name: str
    url: str
    expect_status: int = 200
    expect_text: str = ""
    interval: str = "1m"
    timeout: str = "15s"
    severity: str = "high"

    @property
    def priority(self) -> int:
        return SEVERITIES[self.severity]

    @property
    def trigger_name(self) -> str:
        return f"{self.name} is down"

    def trigger_expression(self, host: str) -> str:
        return f"last(/{host}/web.test.fail[{self.name}])<>0"


@dataclass(frozen=True)
class Interface:
    dns: str = ""
    ip: str = ""
    port: int = 10050


@dataclass(frozen=True)
class Host:
    host: str
    name: str
    groups: tuple[str, ...]
    templates: tuple[str, ...] = ()
    interface: Interface | None = None
    tags: dict[str, str] = field(default_factory=dict)
    web_checks: tuple[WebCheck, ...] = ()


@dataclass(frozen=True)
class Config:
    host_groups: tuple[str, ...]
    hosts: tuple[Host, ...]

    @property
    def templates(self) -> list[str]:
        return sorted({t for h in self.hosts for t in h.templates})


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where}: '{key}' is required")
    return mapping[key]


def _web_check(raw: dict, where: str) -> WebCheck:
    name = str(_require(raw, "name", where))
    if not _SAFE_NAME.match(name):
        raise ConfigError(f"{where}: name {name!r} may only use letters, digits, space, _ . -")
    url = str(_require(raw, "url", where))
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: url must start with http:// or https://")
    check = WebCheck(
        name=name,
        url=url,
        expect_status=int(raw.get("expect_status", 200)),
        expect_text=str(raw.get("expect_text", "")),
        interval=str(raw.get("interval", "1m")),
        timeout=str(raw.get("timeout", "15s")),
        severity=str(raw.get("severity", "high")),
    )
    for label, value in (("interval", check.interval), ("timeout", check.timeout)):
        if not _INTERVAL.match(value):
            raise ConfigError(f"{where}: {label} {value!r} is not like 30s, 5m or 1h")
    if check.severity not in SEVERITIES:
        raise ConfigError(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
    return check


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the file must contain a mapping with 'host_groups' and 'hosts'")
    groups = tuple(str(g) for g in raw.get("host_groups") or ())
    hosts: list[Host] = []
    for index, item in enumerate(raw.get("hosts") or ()):
        where = f"hosts[{index}]"
        technical = str(_require(item, "host", where))
        if not _HOST.match(technical):
            raise ConfigError(f"{where}: host {technical!r} has characters Zabbix does not allow")
        where = f"host {technical!r}"
        host_groups = tuple(str(g) for g in _require(item, "groups", where))
        unknown = [g for g in host_groups if g not in groups]
        if unknown:
            raise ConfigError(f"{where}: groups {unknown} are not listed in host_groups")

        interface = None
        if item.get("interface"):
            spec = item["interface"]
            interface = Interface(
                dns=str(spec.get("dns", "")),
                ip=str(spec.get("ip", "")),
                port=int(spec.get("port", 10050)),
            )
            if not interface.dns and not interface.ip:
                raise ConfigError(f"{where}: interface needs 'dns' or 'ip'")

        checks = tuple(
            _web_check(check, f"{where} web_checks[{i}]")
            for i, check in enumerate(item.get("web_checks") or ())
        )
        names = [c.name for c in checks]
        if len(set(names)) != len(names):
            raise ConfigError(f"{where}: two web checks have the same name")

        hosts.append(
            Host(
                host=technical,
                name=str(item.get("name", technical)),
                groups=host_groups,
                templates=tuple(str(t) for t in item.get("templates") or ()),
                interface=interface,
                tags={str(k): str(v) for k, v in (item.get("tags") or {}).items()},
                web_checks=checks,
            )
        )

    technical_names = [h.host for h in hosts]
    if len(set(technical_names)) != len(technical_names):
        raise ConfigError("a host is listed twice")
    return Config(host_groups=groups, hosts=tuple(hosts))


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh))
