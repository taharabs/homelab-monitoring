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
    assert api.item_last_value("zabbix-agent", "agent.ping") == "1"
    assert api.item_last_value("shop-web", "web.test.fail[Shop home page]") is None


# -- config -----------------------------------------------------------------------------


def test_sample_file_is_valid():
    config = load_config(ROOT / "monitoring.yml")
    assert [h.host for h in config.hosts] == ["zabbix-agent", "shop-web"]
    check = config.hosts[1].web_checks[0]
    assert check.priority == 4
    assert check.trigger_expression("shop-web") == "last(/shop-web/web.test.fail[Shop home page])<>0"
    assert config.templates == ["Linux by Zabbix agent"]


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
        "create_group",
        "create_host",
        "create_host",
        "create_web_check",
        "create_trigger",
    ]
    agent = fake.hosts["zabbix-agent"]
    assert agent["interfaces"] == [
        {"type": 1, "main": 1, "useip": 0, "ip": "", "dns": "zabbix-agent", "port": "10050"}
    ]
    assert agent["templates"] == [fake.templates["Linux by Zabbix agent"]]
    assert {"tag": "role", "value": "linux"} in agent["tags"]
    assert fake.hosts["shop-web"]["interfaces"] == []

    (scenario,) = fake.httptests.values()
    assert scenario["delay"] == "30s"
    assert scenario["steps"][0]["url"] == "http://shop-web/"
    assert scenario["steps"][0]["required"] == "Demo shop"
    (trigger,) = fake.triggers.values()
    assert trigger["description"] == "Shop home page is down"
    assert trigger["priority"] == 4


def test_second_apply_changes_nothing(api, fake):
    config = parse_config(sample())
    apply_config(api, config)
    calls_before = len(fake.calls)

    assert apply_config(api, config) == []
    writes = [c for c in fake.calls[calls_before:] if not c.endswith(".get")]
    assert writes == []


def test_dry_run_reports_but_does_not_write(api, fake):
    actions = apply_config(api, parse_config(sample()), dry_run=True)
    assert len(actions) == 6
    assert fake.hosts == {} and fake.httptests == {} and fake.triggers == {}
    assert all(c.endswith(".get") or c.startswith("user.") for c in fake.calls)


def test_drift_is_corrected_without_deleting_anything(api, fake):
    apply_config(api, parse_config(sample()))
    # Someone edits Zabbix by hand: renames the host, unlinks the template, adds an extra group.
    agent = fake.hosts["zabbix-agent"]
    agent["name"] = "renamed by hand"
    agent["templates"] = []
    fake.groups["Manual group"] = "99"
    agent["groups"].append("99")

    raw = sample()
    raw["hosts"][1]["web_checks"][0].update(url="http://shop-web/health", severity="disaster")
    actions = apply_config(api, parse_config(raw))

    assert [a.kind for a in actions] == [
        "rename_host",
        "link_templates",
        "update_web_check",
        "update_trigger",
    ]
    assert agent["name"] == "Linux server (agent 2)"
    assert agent["templates"] == [fake.templates["Linux by Zabbix agent"]]
    assert "99" in agent["groups"]  # the manual group is kept
    (scenario,) = fake.httptests.values()
    assert scenario["steps"][0]["url"] == "http://shop-web/health"
    (trigger,) = fake.triggers.values()
    assert trigger["priority"] == 5


def test_new_group_is_added_to_an_existing_host(api, fake):
    apply_config(api, parse_config(sample()))
    raw = sample(host_groups=SAMPLE["host_groups"] + ["Homelab/Production"])
    raw["hosts"][0]["groups"] = ["Homelab/Linux servers", "Homelab/Production"]

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
    assert fake.hosts == {} and "hostgroup.create" not in fake.calls


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
    assert "Would apply 6 change(s)" in capsys.readouterr().out
    assert cli_fake.hosts == {}

    assert main(["apply", "-f", file]) == 0
    assert "Applied 6 change(s)" in capsys.readouterr().out
    assert main(["apply", "-f", file]) == 0
    assert "No changes" in capsys.readouterr().out

    assert main(["status", "-f", file]) == 0
    out = capsys.readouterr().out
    assert "zabbix-agent" in out and "available" in out and "Problems (0)" in out
    assert cli_fake.logged_out


def test_cli_status_can_fail_on_a_problem(cli_fake, capsys):
    file = str(ROOT / "monitoring.yml")
    main(["apply", "-f", file])
    (trigger,) = cli_fake.triggers.values()
    trigger.update(value=1, lastchange="1763049600", hosts=[{"host": "shop-web"}])

    assert main(["status", "-f", file, "--fail-on", "high"]) == 2
    assert "Shop home page is down" in capsys.readouterr().out
    assert main(["status", "-f", file, "--fail-on", "disaster"]) == 0


def test_cli_reports_config_errors(tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("host_groups: [A]\nhosts:\n  - host: x\n    groups: [B]\n")
    assert main(["apply", "-f", str(bad)]) == 1
    assert "not listed in host_groups" in capsys.readouterr().err
