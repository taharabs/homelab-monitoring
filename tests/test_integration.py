"""End to end against the real Docker Compose lab (Zabbix 7.0, PostgreSQL, agent 2, nginx).

The test applies monitoring.yml, waits for real data, then stops the demo website and
checks that Zabbix raises the problem, and clears it when the site comes back.

    docker compose up -d
    ZABBIX_URL=http://localhost:8080 python -m pytest -m integration -v
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from zbxctl.apply import apply_config
from zbxctl.client import ZabbixAPI
from zbxctl.config import load_config

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("ZABBIX_URL")
SHOTS = os.environ.get("SCREENSHOT_DIR")
TRIGGER = "Shop home page is down"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="ZABBIX_URL is not set"),
]


def wait_for(what: str, condition: Callable[[], bool], timeout: float = 300, every: float = 5) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            elapsed = time.monotonic() - start
            print(f"  {what}: after {elapsed:.0f}s")
            return elapsed
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def open_problems(api: ZabbixAPI) -> set[str]:
    return {p["description"] for p in api.active_problems()}


def screenshot(name: str, page_path: str) -> None:
    """Optional: capture the Zabbix web interface for the README (needs Playwright)."""
    if not SHOTS:
        return
    from playwright.sync_api import sync_playwright

    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 720})
        page.goto(f"{URL}/index.php")
        page.fill("#name", os.environ.get("ZABBIX_USER", "Admin"))
        page.fill("#password", os.environ.get("ZABBIX_PASSWORD", "zabbix"))
        page.click("#enter")
        page.wait_for_load_state("networkidle")
        page.goto(f"{URL}/{page_path}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))
        browser.close()


@pytest.fixture(scope="module")
def api():
    client = ZabbixAPI(URL)
    client.wait_until_ready(timeout=300)
    deadline = time.monotonic() + 120  # the frontend may answer before the database is ready
    while True:
        try:
            client.login(os.environ.get("ZABBIX_USER", "Admin"), os.environ.get("ZABBIX_PASSWORD", "zabbix"))
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(5)
    yield client
    client.close()


def test_apply_is_idempotent_on_a_real_server(api):
    config = load_config(ROOT / "monitoring.yml")
    first = apply_config(api, config)
    print("\n".join(f"  {a}" for a in first))
    assert {a.kind for a in first} >= {
        "update_interface",
        "create_host",
        "create_web_check",
        "create_trigger",
    }
    assert apply_config(api, config) == []


def test_agent_sends_data(api):
    wait_for("agent.ping = 1", lambda: api.item_last_value("Zabbix server", "agent.ping") == "1", timeout=300)
    wait_for(
        "CPU utilization collected",
        lambda: api.item_last_value("Zabbix server", "system.cpu.util") is not None,
        timeout=300,
    )
    wait_for(
        "agent problem cleared",
        lambda: not any("not available" in p for p in open_problems(api)),
        timeout=300,
    )


def test_website_outage_raises_and_clears_a_problem(api):
    key = "web.test.fail[Shop home page]"
    values: list = []

    def passing() -> bool:
        values.append(api.item_last_value("shop-web", key))
        return values[-1] == "0"

    try:
        wait_for("web check passes", passing, timeout=240)
    except AssertionError:
        raise AssertionError(f"last values of {key}: {values[-5:]}") from None
    assert TRIGGER not in open_problems(api)

    compose("stop", "shop-web")
    try:
        detected = wait_for("problem raised", lambda: TRIGGER in open_problems(api), timeout=240)
        assert detected < 240
        screenshot("zabbix-problem", "zabbix.php?action=problem.view")
        screenshot("zabbix-web-check", "zabbix.php?action=web.view")
    finally:
        compose("start", "shop-web")

    wait_for("problem resolved", lambda: TRIGGER not in open_problems(api), timeout=240)
    screenshot("zabbix-hosts", "zabbix.php?action=host.view")
