#!/usr/bin/env python3
"""Credential-free normal-entrypoint proof on a disposable GitHub-hosted VM.

Runs --version/trade --help and imports only; never starts a trading loop.
Synthetic writable mounts and an isolated CI project are removed afterwards.
No production credentials, cloud account, network trading API or host state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ("freqtradeorg/freqtrade:2026.6@sha256:"
            "d451af021d5e08b70580c0eea5848534e9846b57391b34821c0a5814416397e6")


def run(args: list[str], env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True,
                            timeout=900, check=False)
    if check and result.returncode:
        # Only synthetic environment/configuration enters these commands.
        raise RuntimeError(f"runtime proof failed: {args[0]}\n{result.stdout}\n{result.stderr}")
    return result


def main() -> None:
    if os.name != "posix" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("run this isolated container proof only on a GitHub-hosted Linux CI VM")
    mode = (ROOT / "RELEASE_MODE").read_text().strip()
    if mode not in {"testnet", "live"}:
        raise SystemExit("invalid Bitcoin release mode")
    scratch = Path(tempfile.mkdtemp(prefix=f"bitcoin-{mode}-runtime-proof-"))
    scratch.chmod(0o755)
    project = f"bitcoin-{mode}-runtime-ci"
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env.update({
        "BOT_UID": "994", "BOT_GID": "985", "RELEASE_TAG": "runtime-ci",
        "SHARED_HOST_PATH": str(scratch / "shared"), "EXECUTION_MODE": "simulation",
        "FREQTRADE_API_PASSWORD": secrets.token_hex(24),
        "FREQTRADE_API_JWT_SECRET": secrets.token_hex(32),
        "FREQTRADE_API_WS_TOKEN": secrets.token_hex(32),
        "SIGNAL_HMAC_KEY": secrets.token_hex(32), "COMMAND_HMAC_KEY": secrets.token_hex(32),
        "TELEGRAM_BOT_TOKEN": "123456789:" + secrets.token_hex(24),
        "TELEGRAM_OWNER_CHAT_ID": "1", "SIDECAR_RELEASE_HASH": "a" * 64,
        "DEPLOYED_RELEASE_HASH": "a" * 64, "DEPLOYED_CONFIG_SHA256": "b" * 64,
    })
    override = scratch / "network-isolation.json"
    override.write_text(json.dumps({"services": {"freqtrade": {"network_mode": "none"}}}))
    command = ["docker", "compose", "--project-name", project,
               "--env-file", str(ROOT / ".env.example"), "-f", str(ROOT / "docker-compose.yml"),
               "-f", str(override)]
    try:
        for relative in ("pair", "signals/inbox", "signals/processed", "signals/rejected", "freqtrade"):
            (scratch / "shared" / relative).mkdir(parents=True, exist_ok=True)
        for name in ("active_pair.json", "current_pairlist.json", "freqtrade-active.json"):
            shutil.copyfile(ROOT / "shared/pair" / name, scratch / "shared/pair" / name)
        # This is the only ownership change: the newly allocated synthetic mounts.
        run(["sudo", "-n", "chown", "-R", "994:985", str(scratch / "shared")], env)
        run(["sudo", "-n", "chmod", "-R", "u=rwX,g=rX,o=", str(scratch / "shared")], env)
        run(command + ["build", "freqtrade"], env)
        upstream = run(["docker", "run", "--rm", "--network", "none", "--read-only",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--user", "994:985", UPSTREAM, "--version"], env, check=False)
        if upstream.returncode == 0 or not any(
            text in (upstream.stdout + upstream.stderr).lower()
            for text in ("executable file not found", "permission denied")
        ):
            raise RuntimeError("upstream arbitrary-UID negative control did not reproduce the reported failure")
        print("UPSTREAM_ARBITRARY_UID_NEGATIVE_CONTROL=PASS")
        for args in (["--version"], ["trade", "--help"]):
            result = run(command + ["run", "--rm", "--no-deps", "-T", "freqtrade", *args], env)
            if "freqtrade" not in (result.stdout + result.stderr).lower():
                raise RuntimeError("normal entrypoint returned no Freqtrade output")
        print("ROOT_COMPOSE_NORMAL_ENTRYPOINT_UID_994_GID_985=PASS")
        probe = """
import importlib.metadata, os, pathlib, site
import freqtrade, rapidjson, ccxt
from services.common.market_policy import canonical_pair
assert os.getuid() == 994 and os.getgid() == 985
assert os.environ["PYTHONPATH"] == "/freqtrade/services_src"
assert os.environ["PYTHONUSERBASE"] == "/home/ftuser/.local"
assert "/home/ftuser/.local/" in site.getusersitepackages()
assert importlib.metadata.version("freqtrade").startswith("2026.6")
assert canonical_pair("BTC/USDT") == "BTC/USDT"
assert pathlib.Path("/home/ftuser/.local/bin/freqtrade").is_file()
try:
    list(pathlib.Path("/home/ftuser").iterdir())
except PermissionError:
    pass
else:
    raise AssertionError("non-owner can list ftuser home")
for path in ("/freqtrade/shared/freqtrade", "/freqtrade/shared/signals/inbox"):
    marker = pathlib.Path(path) / "CI_ONLY_PERMISSION_PROBE"
    marker.write_text("synthetic fixture")
    marker.unlink()
print("PINNED_IMPORTS_USER_SITE_AND_MOUNT_PERMISSIONS=PASS")
"""
        result = run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                                "freqtrade", "-c", probe], env)
        print(result.stdout.strip())
        # Reuse only the isolated synthetic mounts for a second unrelated host
        # identity. No production user, config, host state or entrypoint changes.
        env.update({"BOT_UID": "12345", "BOT_GID": "23456"})
        run(["sudo", "-n", "chown", "-R", "12345:23456", str(scratch / "shared")], env)
        for args in (["--version"], ["trade", "--help"]):
            result = run(command + ["run", "--rm", "--no-deps", "-T", "freqtrade", *args], env)
            if "freqtrade" not in (result.stdout + result.stderr).lower():
                raise RuntimeError("second identity normal entrypoint returned no Freqtrade output")
        second_probe = probe.replace("os.getuid() == 994 and os.getgid() == 985",
                                     "os.getuid() == 12345 and os.getgid() == 23456")
        result = run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                                "freqtrade", "-c", second_probe], env)
        print("ROOT_COMPOSE_NORMAL_ENTRYPOINT_UID_12345_GID_23456=PASS")
        print(result.stdout.strip())
        inspected = json.loads(run(["docker", "image", "inspect",
                                    f"bitcoin-{mode}-freqtrade:runtime-ci"], env).stdout)[0]
        config = inspected["Config"]
        if config.get("Entrypoint") != ["freqtrade"] or config.get("User") != "ftuser":
            raise RuntimeError("derived image changed upstream entrypoint or final user")
        print("DERIVED_IMAGE_ID=" + inspected["Id"])
        print("ARCHITECTURE=" + inspected["Architecture"])
        print("ORDERS_SUBMITTED=0; HOST_DEPLOYMENT=NOT_TESTED")
    finally:
        run(command + ["down", "--remove-orphans"], env, check=False)
        # Never use a broad cleanup; validate the exact tempfile created above.
        if scratch.parent == Path(tempfile.gettempdir()).resolve() and scratch.name.startswith(
            f"bitcoin-{mode}-runtime-proof-"
        ) and not scratch.is_symlink():
            run(["sudo", "-n", "rm", "-rf", "--one-file-system", "--", str(scratch)], env)


if __name__ == "__main__":
    main()
