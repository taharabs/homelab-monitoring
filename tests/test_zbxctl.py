from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest
import yaml

from tests.fake_zabbix import FakeZabbix
from zbxctl.apply import apply_config
from zbxctl.cli import main
from zbxctl.client import ZabbixAPI, ZabbixAPIError
from zbxctl.config import ConfigError, load_config, parse_config

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = yaml.safe_load((ROOT / "monitoring.yml").read_text())


@pytest.fixture
def fake():
    return FakeZabbix()


@pytest.fixture
def api(fake):
    client = ZabbixAPI("http://zabbix.test", http=httpx.Client(transport=fake.transport()))
    client.login("Admin", "zabbix")
    return client


def sample(**changes):
    raw = copy.deepcopy(SAMPLE)
    raw.update(changes)
    return raw


# -- client -----------------------------------------------------------------------------


def test_version_is_asked_without_a_token(api):
    assert api.version() == "7.0.5"


def test_login_failure_raises_a_readable_error(fake):
    client = ZabbixAPI("http://zabbix.test/", http=httpx.Client(transport=fake.transport()))
    with pytest.raises(ZabbixAPIError, match="Incorrect user name"):
        client.login("Admin", "wrong")


def test_calls_without_login_are_refused(fake):
    client = ZabbixAPI("http://zabbix.test", http=httpx.Client(transport=fake.transport()))
    with pytest.raises(ZabbixAPIError, match="Not authorized"):
        client.call("host.get", {"filter": {"host": []}})


def test_endpoint_is_built_from_the_base_url():
    assert ZabbixAPI("http://z:8080/").endpoint == "http://z:8080/api_jsonrpc.php"
    assert ZabbixAPI("http://z/api_jsonrpc.php").endpoint == "http://z/api_jsonrpc.php"


def test_closing_logs_out(api, fake):
    api.close()
    assert fake.logged_out


def test_item_last_value(api):
    assert api.item_last_value("Zabbix server", "agent.ping") == "1"
    assert api.item_last_value("shop-web", "system.uptime") is None


def test_web_scenario_items_are_requested_explicitly(api, fake):
    # Zabbix leaves web scenario items out of item.get unless webitems is set.
    fake.m_item_get = lambda params: (
        [{"lastvalue": "0", "lastclock": "1763049600"}] if params.get("webitems") else []
    )
    assert api.item_last_value("shop-web", "web.test.fail[Shop home page]") == "0"


# -- config -----------------------------------------------------------------------------


def test_sample_file_is_valid():
    config = load_config(ROOT / "monitoring.yml")
    assert [h.host for h in config.hosts] == ["Zabbix server", "shop-web"]
    check = config.hosts[1].web_checks[0]
    assert check.priority == 4
    assert check.trigger_expression("shop-web") == "last(/shop-web/web.test.fail[Shop home page])<>0"
    assert config.templates == ["Linux by Zabbix agent", "Zabbix server health"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["hosts"][0].update(groups=["Unknown group"]), "not listed in host_groups"),
        (lambda r: r["hosts"][1]["web_checks"][0].update(severity="urgent"), "severity must be one of"),
        (lambda r: r["hosts"][1]["web_checks"][0].update(interval="every minute"), "is not like"),
        (lambda r: r["hosts"][1]["web_checks"][0].update(url="shop-web"), "must start with http"),
        (lambda r: r["hosts"][1]["web_checks"][0].update(name="Shop, home"), "may only use"),
        (lambda r: r["hosts"][0].update(interface={"port": 10050}), "needs 'dns' or 'ip'"),
        (lambda r: r["hosts"].append(copy.deepcopy(r["hosts"][0])), "listed twice"),
        (lambda r: r["hosts"][0].pop("groups"), "'groups' is required"),
    ],
)
def test_invalid_files_are_rejected(mutate, message):
    raw = sample()
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


# -- apply ------------------------------------------------------------------------------


def test_first_apply_creates_everything(api, fake):
    actions = apply_config(api, parse_config(sample()))

    assert [a.kind for a in actions] == [
        "create_group",
        "update_interface",
        "create_host",
        "create_web_check",
        "create_trigger",
    ]
    assert actions[1].detail == "127.0.0.1:10050 -> zabbix-agent:10050"
    (interface,) = fake.hosts["Zabbix server"]["interfaces"]
    assert (interface["useip"], interface["dns"], interface["ip"]) == (0, "zabbix-agent", "")

    shop = fake.hosts["shop-web"]
    assert shop["interfaces"] == []
    assert shop["groups"] == [fake.groups["Homelab/Web services"]]
    assert {"tag": "role", "value": "web"} in shop["tags"]

    (scenario,) = fake.httptests.values()
    assert scenario["delay"] == "30s"
    assert scenario["steps"][0]["url"] == "http://shop-web/"
    assert scenario["steps"][0]["required"] == "Demo shop"
    (trigger,) = fake.triggers.values()
    assert trigger["description"] == "Shop home page is down"
    assert trigger["priority"] == 4


def test_new_host_gets_its_agent_interface(api, fake):
    raw = sample()
    raw["hosts"].append(
        {
            "host": "nas-01",
            "groups": ["Zabbix servers"],
            "templates": ["Linux by Zabbix agent"],
            "interface": {"ip": "192.0.2.10"},
        }
    )
    apply_config(api, parse_config(raw))
    (interface,) = fake.hosts["nas-01"]["interfaces"]
    assert (interface["useip"], interface["ip"], interface["port"]) == (1, "192.0.2.10", "10050")


def test_missing_agent_interface_is_added(api, fake):
    fake.hosts["Zabbix server"]["interfaces"] = []
    actions = apply_config(api, parse_config(sample()))
    assert ("add_interface", "zabbix-agent:10050") in [(a.kind, a.detail) for a in actions]
    assert fake.hosts["Zabbix server"]["interfaces"][0]["dns"] == "zabbix-agent"


def test_second_apply_changes_nothing(api, fake):
    config = parse_config(sample())
    apply_config(api, config)
    calls_before = len(fake.calls)

    assert apply_config(api, config) == []
    writes = [c for c in fake.calls[calls_before:] if not c.endswith(".get")]
    assert writes == []


def test_dry_run_reports_but_does_not_write(api, fake):
    actions = apply_config(api, parse_config(sample()), dry_run=True)
    assert len(actions) == 5
    assert set(fake.hosts) == {"Zabbix server"} and fake.httptests == {} and fake.triggers == {}
    assert fake.hosts["Zabbix server"]["interfaces"][0]["ip"] == "127.0.0.1"
    assert all(c.endswith(".get") or c.startswith("user.") for c in fake.calls)


def test_drift_is_corrected_without_deleting_anything(api, fake):
    apply_config(api, parse_config(sample()))
    # Someone edits Zabbix by hand: renames the host, unlinks a template, adds an extra group,
    # and points the agent somewhere else.
    server = fake.hosts["Zabbix server"]
    server["name"] = "renamed by hand"
    server["templates"] = [fake.templates["Zabbix server health"]]
    fake.groups["Manual group"] = "99"
    server["groups"].append("99")
    server["interfaces"][0].update(dns="old-agent", port="10051")

    raw = sample()
    raw["hosts"][1]["web_checks"][0].update(url="http://shop-web/health", severity="disaster")
    actions = apply_config(api, parse_config(raw))

    assert [a.kind for a in actions] == [
        "rename_host",
        "update_interface",
        "link_templates",
        "update_web_check",
        "update_trigger",
    ]
    assert server["name"] == "Zabbix server"
    assert fake.templates["Linux by Zabbix agent"] in server["templates"]
    assert "99" in server["groups"]  # the manual group is kept
    assert (server["interfaces"][0]["dns"], server["interfaces"][0]["port"]) == ("zabbix-agent", "10050")
    (scenario,) = fake.httptests.values()
    assert scenario["steps"][0]["url"] == "http://shop-web/health"
    (trigger,) = fake.triggers.values()
    assert trigger["priority"] == 5


def test_new_group_is_added_to_an_existing_host(api, fake):
    apply_config(api, parse_config(sample()))
    raw = sample(host_groups=SAMPLE["host_groups"] + ["Homelab/Production"])
    raw["hosts"][0]["groups"] = ["Zabbix servers", "Homelab/Production"]

    actions = apply_config(api, parse_config(raw))
    assert [(a.kind, a.detail) for a in actions] == [
        ("create_group", ""),
        ("add_groups", "Homelab/Production"),
    ]


def test_missing_template_stops_before_any_change(api, fake):
    raw = sample()
    raw["hosts"][0]["templates"] = ["Windows by Zabbix agent"]
    with pytest.raises(ConfigError, match="Windows by Zabbix agent"):
        apply_config(api, parse_config(raw))
    assert set(fake.hosts) == {"Zabbix server"} and "hostgroup.create" not in fake.calls


# -- command line -----------------------------------------------------------------------


@pytest.fixture
def cli_fake(fake, monkeypatch):
    original = httpx.Client.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = fake.transport()
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    monkeypatch.setenv("ZABBIX_URL", "http://zabbix.test")
    monkeypatch.delenv("ZABBIX_TOKEN", raising=False)
    return fake


def test_cli_apply_then_status(cli_fake, capsys):
    file = str(ROOT / "monitoring.yml")
    assert main(["apply", "-f", file, "--dry-run"]) == 0
    assert "Would apply 5 change(s)" in capsys.readouterr().out
    assert set(cli_fake.hosts) == {"Zabbix server"}

    assert main(["apply", "-f", file]) == 0
    assert "Applied 5 change(s)" in capsys.readouterr().out
    assert main(["apply", "-f", file]) == 0
    assert "No changes" in capsys.readouterr().out

    assert main(["status", "-f", file]) == 0
    out = capsys.readouterr().out
    assert "Zabbix server" in out and "available" in out and "Problems (0)" in out
    assert cli_fake.logged_out


def test_cli_status_can_fail_on_a_problem(cli_fake, capsys):
    file = str(ROOT / "monitoring.yml")
    main(["apply", "-f", file])
    (trigger,) = cli_fake.triggers.values()
    trigger.update(value=1, lastchange="1763049600", hosts=[{"host": "shop-web"}])

    cli_fake.item_values["shop-web", "web.test.fail[Shop home page]"] = "0"

    assert main(["status", "-f", file, "--fail-on", "high"]) == 2
    assert "Shop home page is down" in capsys.readouterr().out
    assert main(["status", "-f", file, "--fail-on", "disaster"]) == 0


def test_status_reports_each_web_check(cli_fake, capsys):
    file = str(ROOT / "projects.yml")
    assert main(["apply", "-f", file]) == 0
    capsys.readouterr()
    values = cli_fake.item_values
    for host, check in [
        ("depot-change-monitor", "API health"),
        ("depot-change-monitor", "Agent online"),
        ("depot-change-monitor", "Dashboard"),
        ("task-manager", "Home page"),
        ("wach-ntayab", "Home page"),
        ("wach-ntayab", "Service worker"),
        ("portfolio", "Home page"),
    ]:
        values[host, f"web.test.fail[{check}]"] = "0"

    # One check has not run yet: not a pass when a threshold is set.
    assert main(["status", "-f", file, "--fail-on", "high"]) == 2
    out = capsys.readouterr().out
    assert "Web checks" in out and "no data yet" in out and "U-Net write-up" in out

    values["portfolio", "web.test.fail[U-Net write-up]"] = "0"
    assert main(["status", "-f", file, "--fail-on", "high"]) == 0
    assert "no data yet" not in capsys.readouterr().out

    values["task-manager", "web.test.fail[Home page]"] = "1"
    values["task-manager", "web.test.error[Home page]"] = 'required pattern "Task Manager" was not found'
    assert main(["status", "-f", file]) == 0  # without --fail-on it only reports
    assert "failing" in capsys.readouterr().out
    assert main(["status", "-f", file, "--fail-on", "high"]) == 2


def test_status_can_wait_for_first_results(cli_fake, capsys, monkeypatch):
    file = str(ROOT / "monitoring.yml")
    main(["apply", "-f", file])
    naps = []

    def fake_sleep(seconds):  # the check reports while zbxctl waits
        naps.append(seconds)
        cli_fake.item_values["shop-web", "web.test.fail[Shop home page]"] = "0"

    monkeypatch.setattr("zbxctl.cli.time.sleep", fake_sleep)
    assert main(["status", "-f", file, "--wait", "60", "--fail-on", "high"]) == 0
    assert naps and "ok" in capsys.readouterr().out


def test_projects_file_is_valid():
    config = load_config(ROOT / "projects.yml")
    checks = [c for h in config.hosts for c in h.web_checks]
    assert len(checks) == 8
    assert all(c.url.startswith("https://") for c in checks)


def test_cli_reports_config_errors(tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("host_groups: [A]\nhosts:\n  - host: x\n    groups: [B]\n")
    assert main(["apply", "-f", str(bad)]) == 1
    assert "not listed in host_groups" in capsys.readouterr().err
