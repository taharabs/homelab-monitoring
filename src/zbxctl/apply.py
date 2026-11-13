"""Make Zabbix match the monitoring file.

The apply is additive and safe to repeat:

* missing host groups, hosts, web checks and triggers are created;
* missing groups and templates are added to existing hosts;
* web checks and triggers that drifted from the file are updated;
* nothing is ever deleted, so objects created by hand in the web interface are left alone.

Running it twice in a row makes no change the second time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import ZabbixAPI
from .config import Config, ConfigError, Host, WebCheck


@dataclass(frozen=True)
class Action:
    kind: str  # create_group, create_host, add_groups, link_templates, rename_host, ...
    target: str
    detail: str = ""

    def __str__(self) -> str:
        text = f"{self.kind:<18} {self.target}"
        return f"{text}  ({self.detail})" if self.detail else text


def _interfaces(host: Host) -> list[dict]:
    if host.interface is None:
        return []
    use_ip = 1 if host.interface.ip and not host.interface.dns else 0
    return [
        {
            "type": 1,  # Zabbix agent
            "main": 1,
            "useip": use_ip,
            "ip": host.interface.ip,
            "dns": host.interface.dns,
            "port": str(host.interface.port),
        }
    ]


def _step(check: WebCheck) -> dict:
    return {
        "no": 1,
        "name": "Open page",
        "url": check.url,
        "status_codes": str(check.expect_status),
        "required": check.expect_text,
        "timeout": check.timeout,
        "follow_redirects": 1,
    }


def _step_differs(existing: dict, wanted: dict) -> bool:
    return any(str(existing.get(k, "")) != str(wanted[k]) for k in ("url", "status_codes", "required"))


class Applier:
    def __init__(self, api: ZabbixAPI, config: Config, *, dry_run: bool = False) -> None:
        self.api = api
        self.config = config
        self.dry_run = dry_run
        self.actions: list[Action] = []

    def _do(self, action: Action, method: str, params: dict) -> dict | None:
        self.actions.append(action)
        if self.dry_run:
            return None
        return self.api.call(method, params)

    # -- groups and templates ----------------------------------------------------------

    def _groups(self) -> dict[str, str | None]:
        found = self.api.call(
            "hostgroup.get",
            {"output": ["groupid", "name"], "filter": {"name": list(self.config.host_groups)}},
        )
        ids: dict[str, str | None] = {g["name"]: g["groupid"] for g in found}
        for name in self.config.host_groups:
            if name not in ids:
                result = self._do(Action("create_group", name), "hostgroup.create", {"name": name})
                ids[name] = result["groupids"][0] if result else None
        return ids

    def _templates(self) -> dict[str, str]:
        wanted = self.config.templates
        if not wanted:
            return {}
        found = self.api.call("template.get", {"output": ["templateid", "host"], "filter": {"host": wanted}})
        ids = {t["host"]: t["templateid"] for t in found}
        missing = [t for t in wanted if t not in ids]
        if missing:
            raise ConfigError(f"templates not found in Zabbix: {', '.join(missing)}")
        return ids

    # -- hosts -------------------------------------------------------------------------

    def _hosts(self, groups: dict[str, str | None], templates: dict[str, str]) -> dict:
        found = self.api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"host": [h.host for h in self.config.hosts]},
                "selectHostGroups": ["name"],
                "selectParentTemplates": ["host"],
            },
        )
        existing = {h["host"]: h for h in found}
        host_ids: dict[str, str | None] = {}

        for host in self.config.hosts:
            current = existing.get(host.host)
            if current is None:
                params = {
                    "host": host.host,
                    "name": host.name,
                    "groups": [{"groupid": groups[g]} for g in host.groups],
                    "templates": [{"templateid": templates[t]} for t in host.templates],
                    "interfaces": _interfaces(host),
                    "tags": [{"tag": k, "value": v} for k, v in host.tags.items()],
                }
                detail = ", ".join(host.templates) or "no template"
                result = self._do(Action("create_host", host.host, detail), "host.create", params)
                host_ids[host.host] = result["hostids"][0] if result else None
                continue

            host_id = current["hostid"]
            host_ids[host.host] = host_id
            if current["name"] != host.name:
                self._do(
                    Action("rename_host", host.host, f"{current['name']!r} -> {host.name!r}"),
                    "host.update",
                    {"hostid": host_id, "name": host.name},
                )
            have_groups = {g["name"] for g in current.get("hostgroups", [])}
            add_groups = [g for g in host.groups if g not in have_groups]
            if add_groups:
                self._do(
                    Action("add_groups", host.host, ", ".join(add_groups)),
                    "host.massadd",
                    {"hosts": [{"hostid": host_id}], "groups": [{"groupid": groups[g]} for g in add_groups]},
                )
            have_templates = {t["host"] for t in current.get("parentTemplates", [])}
            link = [t for t in host.templates if t not in have_templates]
            if link:
                self._do(
                    Action("link_templates", host.host, ", ".join(link)),
                    "host.massadd",
                    {
                        "hosts": [{"hostid": host_id}],
                        "templates": [{"templateid": templates[t]} for t in link],
                    },
                )
        return host_ids

    # -- web checks and their triggers -------------------------------------------------

    def _web_checks(self, host: Host, host_id: str | None) -> None:
        if not host.web_checks:
            return
        scenarios: dict[str, dict] = {}
        triggers: dict[str, dict] = {}
        if host_id is not None:
            scenarios = {
                s["name"]: s
                for s in self.api.call(
                    "httptest.get",
                    {
                        "output": ["httptestid", "name", "delay"],
                        "hostids": [host_id],
                        "selectSteps": ["url", "status_codes", "required"],
                    },
                )
            }
            triggers = {
                t["description"]: t
                for t in self.api.call(
                    "trigger.get",
                    {
                        "output": ["triggerid", "description", "priority"],
                        "hostids": [host_id],
                        "filter": {"description": [c.trigger_name for c in host.web_checks]},
                    },
                )
            }

        for check in host.web_checks:
            label = f"{host.host}: {check.name}"
            step = _step(check)
            scenario = scenarios.get(check.name)
            if scenario is None:
                self._do(
                    Action("create_web_check", label, f"{check.url} every {check.interval}"),
                    "httptest.create",
                    {"name": check.name, "hostid": host_id, "delay": check.interval, "steps": [step]},
                )
            else:
                steps = scenario.get("steps") or [{}]
                if scenario["delay"] != check.interval or _step_differs(steps[0], step):
                    self._do(
                        Action("update_web_check", label, f"{check.url} every {check.interval}"),
                        "httptest.update",
                        {"httptestid": scenario["httptestid"], "delay": check.interval, "steps": [step]},
                    )

            trigger = triggers.get(check.trigger_name)
            if trigger is None:
                self._do(
                    Action("create_trigger", label, f"{check.trigger_name}, {check.severity}"),
                    "trigger.create",
                    {
                        "description": check.trigger_name,
                        "expression": check.trigger_expression(host.host),
                        "priority": check.priority,
                        "tags": [{"tag": "service", "value": host.host}],
                    },
                )
            elif int(trigger["priority"]) != check.priority:
                self._do(
                    Action("update_trigger", label, f"severity {check.severity}"),
                    "trigger.update",
                    {"triggerid": trigger["triggerid"], "priority": check.priority},
                )

    def run(self) -> list[Action]:
        templates = self._templates()  # fail before changing anything if a template is missing
        groups = self._groups()
        host_ids = self._hosts(groups, templates)
        for host in self.config.hosts:
            self._web_checks(host, host_ids.get(host.host))
        return self.actions


def apply_config(api: ZabbixAPI, config: Config, *, dry_run: bool = False) -> list[Action]:
    return Applier(api, config, dry_run=dry_run).run()
