"""Command line: zbxctl apply | status | wait"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import __version__
from .apply import apply_config
from .client import ZabbixAPI
from .config import SEVERITIES, ConfigError, load_config

SEVERITY_NAMES = {v: k for k, v in SEVERITIES.items()} | {0: "not classified"}
AVAILABILITY = {"0": "unknown", "1": "available", "2": "unavailable"}


def connect() -> ZabbixAPI:
    """Connection settings come from the environment, never from the monitoring file."""
    url = os.environ.get("ZABBIX_URL", "http://localhost:8080")
    token = os.environ.get("ZABBIX_TOKEN")
    api = ZabbixAPI(url, token=token)
    if not token:
        api.login(os.environ.get("ZABBIX_USER", "Admin"), os.environ.get("ZABBIX_PASSWORD", "zabbix"))
    return api


def cmd_apply(args: argparse.Namespace) -> int:
    config = load_config(args.file)
    with connect() as api:
        actions = apply_config(api, config, dry_run=args.dry_run)
    if not actions:
        print("No changes: Zabbix already matches", args.file)
        return 0
    verb = "Would apply" if args.dry_run else "Applied"
    print(f"{verb} {len(actions)} change(s):")
    for action in actions:
        print("  ", action)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.file)
    with connect() as api:
        hosts = api.call(
            "host.get",
            {
                "output": ["host", "name", "status"],
                "filter": {"host": [h.host for h in config.hosts]},
                "selectInterfaces": ["available", "dns", "ip"],
            },
        )
        problems = api.active_problems()

    print("Hosts")
    for host in sorted(hosts, key=lambda h: h["host"]):
        interfaces = host.get("interfaces") or []
        state = AVAILABILITY.get(str(interfaces[0]["available"]), "?") if interfaces else "no agent"
        print(f"  {host['host']:<24} {state:<12} {host['name']}")
    missing = sorted({h.host for h in config.hosts} - {h["host"] for h in hosts})
    for name in missing:
        print(f"  {name:<24} {'missing':<12} run 'zbxctl apply' first")

    print(f"\nProblems ({len(problems)})")
    worst = 0
    for problem in problems:
        priority = int(problem["priority"])
        worst = max(worst, priority)
        since = dt.datetime.fromtimestamp(int(problem["lastchange"]), dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        on = ", ".join(h["host"] for h in problem.get("hosts", []))
        print(
            f"  {SEVERITY_NAMES.get(priority, priority):<14} {on:<20} "
            f"{problem['description']}  (since {since})"
        )
    if not problems:
        print("  none")

    threshold = SEVERITIES.get(args.fail_on) if args.fail_on else None
    return 2 if threshold is not None and worst >= threshold else 0


def cmd_wait(args: argparse.Namespace) -> int:
    api = ZabbixAPI(os.environ.get("ZABBIX_URL", "http://localhost:8080"))
    version = api.wait_until_ready(timeout=args.timeout)
    print(f"Zabbix API {version} is ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zbxctl", description="Zabbix monitoring as code")
    parser.add_argument("--version", action="version", version=f"zbxctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_p = sub.add_parser("apply", help="create or update what the file describes")
    apply_p.add_argument("-f", "--file", default="monitoring.yml")
    apply_p.add_argument("--dry-run", action="store_true", help="show the changes, make none")
    apply_p.set_defaults(func=cmd_apply)

    status_p = sub.add_parser("status", help="host availability and current problems")
    status_p.add_argument("-f", "--file", default="monitoring.yml")
    status_p.add_argument(
        "--fail-on",
        choices=list(SEVERITIES),
        help="exit with code 2 if a problem of this severity or worse is open",
    )
    status_p.set_defaults(func=cmd_status)

    wait_p = sub.add_parser("wait", help="wait until the Zabbix API answers")
    wait_p.add_argument("--timeout", type=float, default=300)
    wait_p.set_defaults(func=cmd_wait)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
