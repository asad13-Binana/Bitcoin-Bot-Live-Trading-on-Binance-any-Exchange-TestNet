#!/usr/bin/env python3
"""Verify the static and host-level contract for four isolated bot instances.

The default check is safe on any checkout and never reads credentials.
Use --host after all four repositories are installed on the Oracle VM. Host
validation prints only status labels; secret values and hashes are never shown.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "deploy" / "four_bot_host_contract.json"
IDENTITY_PATH = ROOT / "deploy" / "instance_identity.sh"
COMPOSE_PATH = ROOT / "docker-compose.yml"

IDENTITY_FIELDS = {
    "slug": "INSTANCE_SLUG",
    "release_mode": "INSTANCE_MODE",
    "compose_project": "COMPOSE_PROJECT_NAME",
    "service_image": "SERVICE_IMAGE",
    "app_root": "APP_ROOT",
    "private_root": "PRIVATE_ROOT",
    "persistent_root": "PERSIST_PARENT",
    "bot_user": "BOT_USER",
    "monitor_user": "MONITOR_USER",
    "systemd_prefix": "SYSTEMD_PREFIX",
    "monitor_port": "EXPECTED_MONITOR_PORT",
}

UNIQUE_FIELDS = (
    "slug",
    "compose_project",
    "service_image",
    "app_root",
    "private_root",
    "persistent_root",
    "monitor_port",
    "bot_user",
    "monitor_user",
    "systemd_prefix",
    "deploy_wrapper",
)


class ContractError(RuntimeError):
    pass


def load_contract() -> dict[str, Any]:
    data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ContractError("unsupported four-bot contract schema")
    instances = data.get("instances")
    if not isinstance(instances, list) or len(instances) != 4:
        raise ContractError("contract must define exactly four instances")
    return data


def parse_identity() -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"^readonly ([A-Z][A-Z0-9_]*)=(.*)$")
    for raw in IDENTITY_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(raw.strip())
        if not match:
            continue
        key, value = match.groups()
        if "$" in value:
            continue
        values[key] = value.strip().strip("'\\\"")
    return values


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match:
            raise ContractError(f"{path}: invalid KEY=VALUE line {number}")
        key, value = match.groups()
        if key in values:
            raise ContractError(f"{path}: duplicate key {key}")
        values[key] = value.strip().strip("'\\\"")
    return values


def local_instance(contract: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    slug = identity.get("INSTANCE_SLUG")
    matches = [item for item in contract["instances"] if item["slug"] == slug]
    if len(matches) != 1:
        raise ContractError("local INSTANCE_SLUG is absent or ambiguous in the contract")
    return matches[0]


def check_uniqueness(contract: dict[str, Any]) -> None:
    instances = contract["instances"]
    for field in UNIQUE_FIELDS:
        values = [str(item[field]) for item in instances]
        if len(values) != len(set(values)):
            raise ContractError(f"contract field is not unique: {field}")
    ports = sorted(int(item["monitor_port"]) for item in instances)
    if ports != [8090, 8091, 8092, 8093]:
        raise ContractError("monitor ports must be the reserved 8090-8093 sequence")
    host = contract["host_profile"]
    cpu = round(sum(float(item["container_cpu_limit"]) for item in instances), 6)
    memory = sum(int(item["container_memory_mib"]) for item in instances)
    if cpu != float(host["maximum_aggregate_container_cpus"]):
        raise ContractError("aggregate CPU limit does not reconcile")
    if memory != int(host["maximum_aggregate_container_memory_mib"]):
        raise ContractError("aggregate memory limit does not reconcile")


def memory_to_mib(value: str, unit: str) -> int:
    amount = int(value)
    return amount if unit.lower() == "m" else amount * 1024


def check_local_static(contract: dict[str, Any]) -> str:
    identity = parse_identity()
    item = local_instance(contract, identity)
    for contract_key, identity_key in IDENTITY_FIELDS.items():
        actual = identity.get(identity_key)
        expected = str(item[contract_key])
        if actual != expected:
            raise ContractError(
                f"identity mismatch for {identity_key}: expected {expected}, found {actual}"
            )

    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", compose)
    if not name_match or name_match.group(1) != item["compose_project"]:
        raise ContractError("Compose project name is not bound to this instance")
    service_section = compose.partition("\nservices:\n")[2]
    if not service_section:
        raise ContractError("Compose file has no services section")
    service_section = re.split(
        r"(?m)^(?=[^\s#])", service_section, maxsplit=1
    )[0]
    compose_services = set(
        re.findall(r"(?m)^  ([a-z0-9][a-z0-9-]*):\s*$", service_section)
    )
    if compose_services != set(item["services"]):
        raise ContractError("Compose service set does not match the host contract")
    expected_image = item["service_image"] + ":" + chr(36) + "{RELEASE_TAG:-local}"
    if expected_image not in compose:
        raise ContractError("repository-specific service image tag is missing")

    cpus = [float(value) for value in re.findall(r"(?m)^\s+cpus:\s*[\\\"']?([0-9.]+)", compose)]
    if round(sum(cpus), 6) != float(item["container_cpu_limit"]):
        raise ContractError("Compose CPU limits do not reconcile to the instance contract")
    memory = sum(
        memory_to_mib(value, unit)
        for value, unit in re.findall(r"(?mi)^\s+mem_limit:\s*([0-9]+)([mg])\s*$", compose)
    )
    if memory != int(item["container_memory_mib"]):
        raise ContractError("Compose memory limits do not reconcile to the instance contract")

    env = parse_env(ROOT / ".env.example")
    expected_shared = f"{item['persistent_root']}/shared"
    if env.get("SHARED_HOST_PATH") != expected_shared:
        raise ContractError(".env.example does not bind the dedicated persistent root")

    if item["family"] == "binana":
        relevant_modes = (item["release_mode"],)
    elif item["release_mode"] == "testnet":
        relevant_modes = ("simulation", "testnet")
    else:
        relevant_modes = ("simulation", "live")
    for mode in relevant_modes:
        template = ROOT / "monitoring" / f".env.monitor.{mode}.example"
        values = parse_env(template)
        if values.get("MONITOR_PORT") != str(item["monitor_port"]):
            raise ContractError(f"{template.name} has a colliding monitor port")
        for key in ("BOT_DIRECTORY", "MONITOR_SHARED_ROOT", "MONITOR_AUDIT_LOG"):
            value = values.get(key, "")
            if item["slug"] not in value:
                raise ContractError(f"{template.name} is not namespaced: {key}")

    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )
    if workflow_text and item["deploy_wrapper"] not in workflow_text:
        raise ContractError("GitHub deployment workflow does not use the dedicated wrapper")
    return item["slug"]


def read_meminfo() -> tuple[int, int]:
    values: dict[str, int] = {}
    for raw in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = raw.split(":", 1)
        values[key] = int(value.strip().split()[0]) // 1024
    return values.get("MemTotal", 0), values.get("SwapTotal", 0)


def canonical_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and path.resolve() == path


def check_host(contract: dict[str, Any]) -> None:
    if os.name != "posix" or not Path("/proc/meminfo").exists():
        raise ContractError("--host must run on the Linux Oracle VM")
    if os.geteuid() != 0:
        raise ContractError("--host must run as root so credential isolation can be verified")

    profile = contract["host_profile"]
    cpu_count = os.cpu_count() or 0
    if cpu_count < int(profile["minimum_ocpus"]):
        raise ContractError(f"host CPU count is below {profile['minimum_ocpus']}")
    memory, swap = read_meminfo()
    if memory < int(profile["minimum_physical_memory_mib"]):
        raise ContractError(f"physical memory is below {profile['minimum_physical_memory_mib']} MiB")
    if swap < int(profile["minimum_swap_mib"]):
        raise ContractError(f"swap is below {profile['minimum_swap_mib']} MiB")
    free_mib = shutil.disk_usage("/").free // (1024 * 1024)
    if free_mib < int(profile["minimum_free_disk_mib"]):
        raise ContractError(f"root filesystem free space is below {profile['minimum_free_disk_mib']} MiB")

    legacy = (Path("/opt/bitcoin-bot"), Path("/opt/binana-freqtrade-v101"))
    if any(path.exists() for path in legacy):
        raise ContractError("legacy generic deployment root exists; migrate it before four-bot start")

    telegram_tokens: dict[str, str] = {}
    hmac_keys: dict[str, str] = {}
    monitor_tokens: dict[str, str] = {}
    binance_keys: dict[str, str] = {}
    for item in contract["instances"]:
        for key in ("app_root", "private_root", "persistent_root"):
            path = Path(item[key])
            if not canonical_directory(path):
                raise ContractError(f"{item['slug']} has missing or non-canonical {key}")
        current = Path(item["app_root"]) / "current"
        if not current.is_symlink() or not current.resolve().is_dir():
            raise ContractError(f"{item['slug']} current release symlink is missing or invalid")
        releases = Path(item["app_root"]) / "releases"
        try:
            current.resolve().relative_to(releases.resolve())
        except ValueError as error:
            raise ContractError(
                f"{item['slug']} current release escapes its dedicated releases root"
            ) from error
        if not Path(item["deploy_wrapper"]).is_file():
            raise ContractError(f"{item['slug']} deployment wrapper is missing")
        try:
            import pwd
            pwd.getpwnam(item["bot_user"])
            pwd.getpwnam(item["monitor_user"])
        except KeyError as error:
            raise ContractError(f"{item['slug']} runtime identity is missing: {error}") from error

        env_path = Path(item["private_root"]) / ".env"
        values = parse_env(env_path)
        telegram_value = values.get("TELEGRAM_BOT_TOKEN", "")
        if len(telegram_value) < 24:
            raise ContractError(f"{item['slug']} has missing/short TELEGRAM_BOT_TOKEN")
        if telegram_value in telegram_tokens:
            raise ContractError(
                f"TELEGRAM_BOT_TOKEN is reused by {telegram_tokens[telegram_value]} "
                f"and {item['slug']}"
            )
        telegram_tokens[telegram_value] = item["slug"]

        for secret_name in ("SIGNAL_HMAC_KEY", "COMMAND_HMAC_KEY"):
            value = values.get(secret_name, "")
            if len(value) < 24:
                raise ContractError(f"{item['slug']} has missing/short {secret_name}")
            if value in hmac_keys:
                raise ContractError(
                    f"an HMAC key is reused by {hmac_keys[value]} and "
                    f"{item['slug']}:{secret_name}"
                )
            hmac_keys[value] = f"{item['slug']}:{secret_name}"

        for monitor_env in sorted(Path(item["private_root"]).glob("*-monitor.env")):
            monitor_values = parse_env(monitor_env)
            monitor_value = monitor_values.get("MONITOR_TOKEN", "")
            if len(monitor_value) < 32:
                raise ContractError(f"{item['slug']} has missing/short MONITOR_TOKEN")
            previous = monitor_tokens.get(monitor_value)
            if previous and previous != item["slug"]:
                raise ContractError(
                    f"MONITOR_TOKEN is reused by {previous} and {item['slug']}"
                )
            monitor_tokens[monitor_value] = item["slug"]
            if monitor_values.get("TELEGRAM_REPORTS_ENABLED") == "true":
                report_token = monitor_values.get("TELEGRAM_MONITOR_BOT_TOKEN", "")
                if len(report_token) < 24:
                    raise ContractError(
                        f"{item['slug']} enabled monitor reports without a valid token"
                    )
                if report_token in telegram_tokens:
                    raise ContractError(
                        f"a Telegram token is reused by {telegram_tokens[report_token]} "
                        f"and {item['slug']}:monitor"
                    )
                telegram_tokens[report_token] = f"{item['slug']}:monitor"
        execution_mode = values.get("EXECUTION_MODE", "")
        allowed_modes = (
            {"testnet"} if item["family"] == "binana" and item["release_mode"] == "testnet"
            else {"simulation", item["release_mode"]}
        )
        if execution_mode not in allowed_modes:
            raise ContractError(
                f"{item['slug']} has invalid EXECUTION_MODE={execution_mode or 'missing'}"
            )
        api_key = values.get("BINANCE_API_KEY", "")
        if execution_mode == "simulation":
            if api_key or values.get("BINANCE_API_SECRET", ""):
                raise ContractError(
                    f"{item['slug']} simulation mode must not retain Binance credentials"
                )
        else:
            if not api_key:
                raise ContractError(
                    f"{item['slug']} {execution_mode} execution has no Binance API key"
                )
            if api_key in binance_keys:
                raise ContractError(
                    f"Binance API key is reused by {binance_keys[api_key]} and "
                    f"{item['slug']}; use separate accounts or subaccounts"
                )
            binance_keys[api_key] = item["slug"]

    if not shutil.which("docker"):
        raise ContractError("Docker is unavailable on the shared Oracle host")
    result = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    if result.returncode != 0:
        raise ContractError("Docker Compose project inventory failed")
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ContractError("Docker Compose project inventory returned invalid JSON")
    projects = {
        row.get("Name"): str(row.get("Status", "")).lower()
        for row in rows
        if isinstance(row, dict) and row.get("Name")
    }
    allowed = {item["compose_project"] for item in contract["instances"]}
    forbidden = {"bitcoin-bot", "binana-freqtrade-v101"}
    if set(projects) & forbidden:
        raise ContractError("legacy generic Compose project is still installed")
    missing = allowed - set(projects)
    if missing:
        raise ContractError(
            "required bot Compose projects are missing: " + ", ".join(sorted(missing))
        )
    not_running = {
        name for name in allowed if not projects.get(name, "").startswith("running")
    }
    if not_running:
        raise ContractError(
            "bot Compose projects are not fully running: "
            + ", ".join(sorted(not_running))
        )
    unexpected = {
        name for name in projects
        if name and ("bitcoin" in name or "binana" in name) and name not in allowed
    }
    if unexpected:
        raise ContractError(
            "unexpected bot Compose project found: " + ", ".join(sorted(unexpected))
        )

    for item in contract["instances"]:
        containers = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label=com.docker.compose.project={item['compose_project']}",
                "--format", '{{.Label "com.docker.compose.service"}}|{{.State}}',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if containers.returncode != 0:
            raise ContractError(f"{item['slug']} container inventory failed")
        observed: dict[str, str] = {}
        for raw in containers.stdout.splitlines():
            service, separator, state = raw.partition("|")
            if not separator or not service or service in observed:
                raise ContractError(f"{item['slug']} container inventory is ambiguous")
            observed[service] = state
        if set(observed) != set(item["services"]):
            raise ContractError(f"{item['slug']} service set is incomplete or unexpected")
        stopped = sorted(name for name, state in observed.items() if state != "running")
        if stopped:
            raise ContractError(
                f"{item['slug']} has non-running services: {', '.join(stopped)}"
            )
        for timer in item["required_timers"]:
            active = subprocess.run(
                ["systemctl", "is-active", "--quiet", timer],
                check=False,
                timeout=15,
            )
            if active.returncode != 0:
                raise ContractError(f"{item['slug']} required timer is inactive: {timer}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        action="store_true",
        help="also validate the fully configured shared Oracle host without printing secrets",
    )
    args = parser.parse_args()
    try:
        contract = load_contract()
        check_uniqueness(contract)
        slug = check_local_static(contract)
        if args.host:
            check_host(contract)
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: four-bot co-host contract verified for {slug}")
    if args.host:
        print("OK: Oracle host resources, identities, paths and credential separation verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
