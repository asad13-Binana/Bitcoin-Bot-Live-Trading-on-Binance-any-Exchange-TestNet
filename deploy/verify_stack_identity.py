#!/usr/bin/env python3
from __future__ import annotations
"""Verify that the fixed Compose project is the exact expected healthy release."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


EXPECTED_SERVICES = (
    "moneyflow",
    "freqtrade",
    "execution-sidecar",
    "telegram-broker",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class StackIdentityError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _env_map(values) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        name, separator, value = str(item).partition("=")
        if separator:
            result[name] = value
    return result


def validate_snapshot(
    *,
    compose_config: dict,
    containers: list[dict],
    image_ids: dict[str, str],
    release_dir: Path,
    release_hash: str,
    config_sha256: str,
    project: str,
) -> None:
    services = compose_config.get("services") if isinstance(compose_config, dict) else None
    if not isinstance(services, dict) or set(services) != set(EXPECTED_SERVICES):
        raise StackIdentityError("resolved Compose service set is not the expected four services")
    if len(containers) != len(EXPECTED_SERVICES):
        raise StackIdentityError("Compose project does not contain exactly four containers")

    expected_compose = (release_dir / "docker-compose.yml").resolve()
    expected_workdir = release_dir.resolve()
    seen: set[str] = set()
    for container in containers:
        config = container.get("Config") or {}
        state = container.get("State") or {}
        labels = config.get("Labels") or {}
        service = str(labels.get("com.docker.compose.service", ""))
        if service not in EXPECTED_SERVICES or service in seen:
            raise StackIdentityError("container service labels are missing, duplicated, or unexpected")
        seen.add(service)
        if labels.get("com.docker.compose.project") != project:
            raise StackIdentityError(f"{service}: Compose project label mismatch")

        label_files = str(labels.get("com.docker.compose.project.config_files", ""))
        files = [Path(value).resolve() for value in label_files.split(",") if value]
        if files != [expected_compose]:
            raise StackIdentityError(f"{service}: Compose file label is not the release file")
        label_workdir = str(labels.get("com.docker.compose.project.working_dir", ""))
        if not label_workdir or Path(label_workdir).resolve() != expected_workdir:
            raise StackIdentityError(f"{service}: Compose working-directory label mismatch")

        environment = _env_map(config.get("Env"))
        if environment.get("DEPLOYED_RELEASE_HASH") != release_hash:
            raise StackIdentityError(f"{service}: deployed release hash mismatch")
        if environment.get("DEPLOYED_CONFIG_SHA256") != config_sha256:
            raise StackIdentityError(f"{service}: deployed config hash mismatch")

        expected_service = services[service] if isinstance(services[service], dict) else {}
        expected_image = str(expected_service.get("image", ""))
        if not expected_image or config.get("Image") != expected_image:
            raise StackIdentityError(f"{service}: configured image reference mismatch")
        if str(container.get("Image", "")) != str(image_ids.get(expected_image, "")):
            raise StackIdentityError(f"{service}: running image ID mismatch")
        if state.get("Running") is not True or state.get("Status") != "running":
            raise StackIdentityError(f"{service}: container is not running")
        health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
        if health.get("Status") != "healthy":
            raise StackIdentityError(f"{service}: container is not healthy")

    if seen != set(EXPECTED_SERVICES):
        raise StackIdentityError("one or more expected services are absent")


def _run(arguments: list[str], *, json_output: bool = False):
    completed = subprocess.run(
        arguments,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise StackIdentityError(f"command failed without exposing output: {arguments[0]}")
    if json_output:
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise StackIdentityError(f"{arguments[0]} returned malformed JSON") from exc
    return completed.stdout


def verify(args) -> None:
    release_input = Path(args.release_dir)
    releases_input = Path(args.releases_dir)
    config_input = Path(args.config)
    config_root_input = Path(args.config_root)
    if any(path.is_symlink() for path in (
        release_input, releases_input, config_input, config_root_input
    )):
        raise StackIdentityError("identity paths must not be symlinks")
    release_dir = release_input.resolve(strict=True)
    releases_dir = releases_input.resolve(strict=True)
    config = config_input.resolve(strict=True)
    config_root = config_root_input.resolve(strict=True)
    if any(Path(os.path.abspath(path)) != resolved for path, resolved in (
        (release_input, release_dir),
        (releases_input, releases_dir),
        (config_input, config),
        (config_root_input, config_root),
    )):
        raise StackIdentityError("identity paths must be absolute, canonical, and symlink-free")
    if release_dir.parent != releases_dir or not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}Z", release_dir.name
    ):
        raise StackIdentityError("release directory is not a timestamp-named direct child")
    if not HEX64.fullmatch(args.release_hash):
        raise StackIdentityError("release hash is malformed")
    if (config.parent != config_root or config.name != release_dir.name + ".env"
            or not config.is_file() or config.is_symlink()):
        raise StackIdentityError("release config snapshot is missing or is a symlink")
    config_sha = file_sha256(config)
    if config_sha != args.config_sha256:
        raise StackIdentityError("release config snapshot hash changed")

    compose = release_dir / "docker-compose.yml"
    if not compose.is_file() or compose.is_symlink():
        raise StackIdentityError("release Compose file is missing or is a symlink")
    command = [
        "docker", "compose", "--project-name", args.project,
        "--env-file", str(config), "-f", str(compose),
        "config", "--format", "json",
    ]
    compose_config = _run(command, json_output=True)
    ids = [
        line.strip()
        for line in _run([
            "docker", "ps", "-aq", "--filter",
            f"label=com.docker.compose.project={args.project}",
        ]).splitlines()
        if line.strip()
    ]
    containers = [_run(["docker", "inspect", value], json_output=True)[0] for value in ids]
    expected_images = {
        str(service.get("image", ""))
        for service in (compose_config.get("services") or {}).values()
        if isinstance(service, dict)
    }
    image_ids = {}
    for image in expected_images:
        inspected = _run(["docker", "image", "inspect", image], json_output=True)
        if not inspected or not isinstance(inspected[0], dict):
            raise StackIdentityError("expected image cannot be inspected")
        image_ids[image] = str(inspected[0].get("Id", ""))
    validate_snapshot(
        compose_config=compose_config,
        containers=containers,
        image_ids=image_ids,
        release_dir=release_dir,
        release_hash=args.release_hash,
        config_sha256=config_sha,
        project=args.project,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--release-dir", required=True)
    result.add_argument("--releases-dir", required=True)
    result.add_argument("--release-hash", required=True)
    result.add_argument("--config", required=True)
    result.add_argument("--config-root", required=True)
    result.add_argument("--config-sha256", required=True)
    result.add_argument("--project", default="bitcoin-bot")
    return result


def main() -> int:
    try:
        verify(parser().parse_args())
    except (OSError, StackIdentityError, ValueError, IndexError) as exc:
        print(f"stack identity rejected: {exc}", file=sys.stderr)
        return 1
    print("exact four-service stack identity and health verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
