"""An in-memory imitation of the parts of the Zabbix 7.0 API that zbxctl uses.

It checks the same things the real server checks for these calls (authentication,
required fields, duplicate names), so the unit tests exercise real request shapes.
"""

from __future__ import annotations

import itertools
import json

import httpx

TOKEN = "fake-session-token"


class FakeZabbix:
    def __init__(self, templates=("Linux by Zabbix agent", "Zabbix server health")) -> None:
        self._ids = itertools.count(10001)
        self.templates = {name: str(next(self._ids)) for name in templates}
        self.groups: dict[str, str] = {"Zabbix servers": str(next(self._ids))}
        self.hosts: dict[str, dict] = {}
        # Like a fresh Zabbix install: a "Zabbix server" host whose agent is expected on 127.0.0.1.
        self.hosts["Zabbix server"] = {
            "hostid": str(next(self._ids)),
            "host": "Zabbix server",
            "name": "Zabbix server",
            "groups": [self.groups["Zabbix servers"]],
            "templates": [self.templates["Linux by Zabbix agent"], self.templates["Zabbix server health"]],
            "interfaces": [
                {
                    "interfaceid": str(next(self._ids)),
                    "type": 1,
                    "main": 1,
                    "useip": 1,
                    "ip": "127.0.0.1",
                    "dns": "",
                    "port": "10050",
                }
            ],
            "tags": [],
        }
        self.httptests: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.calls: list[str] = []
        self.logged_out = False
        self.item_values: dict[tuple[str, str], str] = {}  # (host, key) -> last value

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _new_id(self) -> str:
        return str(next(self._ids))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        authorized = request.headers.get("Authorization") == f"Bearer {TOKEN}"

        if method in ("apiinfo.version", "user.login"):
            if "Authorization" in request.headers:
                return self._error(
                    body, -32602, f'The "{method}" method must be called without authorization.'
                )
        elif not authorized:
            return self._error(body, -32602, "Not authorized.")

        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            return self._error(body, -32601, f"Method not found: {method}")
        try:
            result = handler(params)
        except ValueError as exc:
            return self._error(body, -32500, "Application error.", str(exc))
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})

    @staticmethod
    def _error(body, code, message, data=""):
        error = {"code": code, "message": message, "data": data}
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": error, "id": body["id"]})

    # -- session ---------------------------------------------------------------------

    def m_apiinfo_version(self, params):
        return "7.0.5"

    def m_user_login(self, params):
        if params != {"username": "Admin", "password": "zabbix"}:
            raise ValueError("Incorrect user name or password or account is temporarily blocked.")
        return TOKEN

    def m_user_logout(self, params):
        self.logged_out = True
        return True

    # -- groups and templates --------------------------------------------------------

    def m_hostgroup_get(self, params):
        names = params["filter"]["name"]
        return [{"groupid": gid, "name": n} for n, gid in self.groups.items() if n in names]

    def m_hostgroup_create(self, params):
        if params["name"] in self.groups:
            raise ValueError(f'Host group "{params["name"]}" already exists.')
        self.groups[params["name"]] = self._new_id()
        return {"groupids": [self.groups[params["name"]]]}

    def m_template_get(self, params):
        names = params["filter"]["host"]
        return [{"templateid": tid, "host": n} for n, tid in self.templates.items() if n in names]

    # -- hosts -----------------------------------------------------------------------

    def _group_name(self, groupid):
        return next(n for n, gid in self.groups.items() if gid == groupid)

    def _template_name(self, templateid):
        return next(n for n, tid in self.templates.items() if tid == templateid)

    def m_host_get(self, params):
        wanted = params["filter"]["host"]
        result = []
        for host in self.hosts.values():
            if host["host"] not in wanted:
                continue
            item = {"hostid": host["hostid"], "host": host["host"], "name": host["name"]}
            if "selectHostGroups" in params:
                item["hostgroups"] = [{"name": self._group_name(g)} for g in host["groups"]]
            if "selectParentTemplates" in params:
                item["parentTemplates"] = [{"host": self._template_name(t)} for t in host["templates"]]
            if "selectInterfaces" in params:
                item["interfaces"] = [
                    {"available": "1", **{k: str(v) for k, v in i.items()}} for i in host["interfaces"]
                ]
            result.append(item)
        return result

    def m_host_create(self, params):
        if params["host"] in self.hosts:
            raise ValueError(f'Host with the same name "{params["host"]}" already exists.')
        if not params.get("groups"):
            raise ValueError('Invalid parameter "/1": the parameter "groups" is missing.')
        for group in params["groups"]:
            if group["groupid"] not in self.groups.values():
                raise ValueError("No permissions to referred object or it does not exist!")
        host_id = self._new_id()
        self.hosts[params["host"]] = {
            "hostid": host_id,
            "host": params["host"],
            "name": params.get("name", params["host"]),
            "groups": [g["groupid"] for g in params["groups"]],
            "templates": [t["templateid"] for t in params.get("templates", [])],
            "interfaces": [{"interfaceid": self._new_id(), **i} for i in params.get("interfaces", [])],
            "tags": params.get("tags", []),
        }
        return {"hostids": [host_id]}

    def _host_by_id(self, host_id):
        return next(h for h in self.hosts.values() if h["hostid"] == host_id)

    def m_host_update(self, params):
        host = self._host_by_id(params["hostid"])
        host["name"] = params.get("name", host["name"])
        return {"hostids": [host["hostid"]]}

    def m_host_massadd(self, params):
        for ref in params["hosts"]:
            host = self._host_by_id(ref["hostid"])
            for group in params.get("groups", []):
                if group["groupid"] not in host["groups"]:
                    host["groups"].append(group["groupid"])
            for template in params.get("templates", []):
                if template["templateid"] not in host["templates"]:
                    host["templates"].append(template["templateid"])
        return {"hostids": [h["hostid"] for h in params["hosts"]]}

    def m_hostinterface_update(self, params):
        for host in self.hosts.values():
            for interface in host["interfaces"]:
                if interface["interfaceid"] == params["interfaceid"]:
                    interface.update({k: v for k, v in params.items() if k != "interfaceid"})
                    return {"interfaceids": [params["interfaceid"]]}
        raise ValueError("No permissions to referred object or it does not exist!")

    def m_hostinterface_create(self, params):
        host = self._host_by_id(params["hostid"])
        interface = {"interfaceid": self._new_id(), **{k: v for k, v in params.items() if k != "hostid"}}
        host["interfaces"].append(interface)
        return {"interfaceids": [interface["interfaceid"]]}

    # -- web scenarios and triggers --------------------------------------------------

    def m_httptest_get(self, params):
        result = []
        for test in self.httptests.values():
            if test["hostid"] in params["hostids"]:
                item = {k: test[k] for k in ("httptestid", "name", "delay")}
                if "selectSteps" in params:
                    item["steps"] = [
                        {k: str(s.get(k, "")) for k in params["selectSteps"]} for s in test["steps"]
                    ]
                result.append(item)
        return result

    def m_httptest_create(self, params):
        if not params.get("hostid"):
            raise ValueError('Invalid parameter "/1/hostid": a number is expected.')
        key = f"{params['hostid']}:{params['name']}"
        if key in self.httptests:
            raise ValueError(f'Web scenario "{params["name"]}" already exists.')
        test_id = self._new_id()
        self.httptests[key] = {"httptestid": test_id, **params}
        return {"httptestids": [test_id]}

    def m_httptest_update(self, params):
        test = next(t for t in self.httptests.values() if t["httptestid"] == params["httptestid"])
        test.update({k: v for k, v in params.items() if k != "httptestid"})
        return {"httptestids": [test["httptestid"]]}

    def m_trigger_get(self, params):
        if "hostids" not in params:
            return [t for t in self.triggers.values() if t.get("value") == 1]
        names = params["filter"]["description"]
        result = []
        for trigger in self.triggers.values():
            host = trigger["expression"].split("/")[1]
            if self.hosts[host]["hostid"] in params["hostids"] and trigger["description"] in names:
                result.append({k: str(trigger[k]) for k in ("triggerid", "description", "priority")})
        return result

    def m_trigger_create(self, params):
        host, rest = params["expression"].split("/")[1], params["expression"].split("/", 2)[2]
        key = rest[: rest.index(")")]
        if not any(
            t["hostid"] == self.hosts[host]["hostid"] and key == f"web.test.fail[{t['name']}]"
            for t in self.httptests.values()
        ):
            raise ValueError(f'Incorrect item key "{key}" provided for trigger expression on "{host}".')
        trigger_id = self._new_id()
        self.triggers[trigger_id] = {"triggerid": trigger_id, "value": 0, **params}
        return {"triggerids": [trigger_id]}

    def m_trigger_update(self, params):
        self.triggers[params["triggerid"]].update(params)
        return {"triggerids": [params["triggerid"]]}

    def m_item_get(self, params):
        key = params["filter"]["key_"]
        if key.startswith("web.test.") and not params.get("webitems"):
            return []  # like Zabbix: web scenario items are only returned with webitems=true
        if params["host"] == "Zabbix server" and key == "agent.ping":
            return [{"lastvalue": "1", "lastclock": "1763049600"}]
        if (params["host"], key) in self.item_values:
            return [{"lastvalue": self.item_values[params["host"], key], "lastclock": "1763049600"}]
        return [{"lastvalue": "0", "lastclock": "0"}]
