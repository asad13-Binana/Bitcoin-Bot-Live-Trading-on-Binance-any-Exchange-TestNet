"""Native Docker proof using restrictive build context and disposable owned mounts.

Only GitHub-hosted Linux CI. No cloud installation, credentials or orders.
The legacy recipe is a negative control, not a supported deployment option.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IDENTITIES = ((994, 985), (12345, 23456))
LEGACY_RECIPE = """FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app PIP_NO_CACHE_DIR=1 HOME=/tmp
COPY requirements.services.lock /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.services.lock && useradd --create-home --uid 10001 bot
COPY --chown=bot:bot services /app/services
COPY --chown=bot:bot freqtrade/user_data/strategies/IctSmcStrategy.py /app/freqtrade/user_data/strategies/IctSmcStrategy.py
COPY --chown=bot:bot RELEASE_MANIFEST.json RELEASE_SHA256.txt VALIDATION_STATUS.json /app/
COPY RELEASE_MODE /app/RELEASE_MODE
RUN chmod 0444 /app/RELEASE_MODE
USER bot
"""


def run(args, *, check=True):
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    result = subprocess.run(args, env=env, cwd=ROOT, text=True, capture_output=True,
                            timeout=900, check=False)
    if check and result.returncode:
        raise RuntimeError(f"container regression failed\n{result.stdout}\n{result.stderr}")
    return result


def container(image, uid, gid, *args, readonly=True, mounts=()):
    command = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", f"{uid}:{gid}",
               "--memory", "180m", "--cpus", "0.08", "--pids-limit", "128",
               "--tmpfs", "/tmp:size=32m,mode=1777", "-e", "EXECUTION_MODE=simulation",
               "-e", "LIVE_TRADING_ENABLED=false", "-e", "BTC_QUOTE_ALLOWLIST=USDT",
               "-e", "COINGECKO_CONTEXT_ENABLED=false", "-e", "COINMARKETCAP_CONTEXT_ENABLED=false",
               "-e", "REQUIRE_EXTERNAL_CONFLUENCE=false",
               "-e", "RUNTIME_DIR=/app/shared/runtime/moneyflow"]
    if readonly:
        command.append("--read-only")
    for source, destination, ro in mounts:
        command += ["--mount", f"type=bind,src={source},dst={destination}" + (",readonly" if ro else "")]
    return command + [image, *args]


def restricted_context(scratch):
    context = scratch / "context"
    context.mkdir()
    for name in ("Dockerfile.services", ".dockerignore", "requirements.services.lock", "RELEASE_MODE",
                 "RELEASE_MANIFEST.json", "RELEASE_SHA256.txt", "VALIDATION_STATUS.json",
                 "freqtrade/user_data/strategies/IctSmcStrategy.py"):
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copytree(ROOT / "services", context / "services", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in context.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="also test this freshly packaged Compose image")
    options = parser.parse_args()
    if os.name != "posix" or os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("RUNNER_ENVIRONMENT") != "github-hosted":
        raise SystemExit("only run on a disposable GitHub-hosted Linux runner")
    scratch = Path(tempfile.mkdtemp(prefix="bitcoin-services-proof-"))
    scratch.chmod(0o755)
    images = []
    try:
        context = restricted_context(scratch)
        image = f"bitcoin-services-proof:{scratch.name}"
        images.append(image)
        run(["docker", "build", "-t", image, "-f", str(context / "Dockerfile.services"), str(context)])
        legacy = scratch / "legacy.Dockerfile"
        legacy.write_text(LEGACY_RECIPE)
        old_image = image + "-negative"
        images.append(old_image)
        run(["docker", "build", "-t", old_image, "-f", str(legacy), str(context)])
        for uid, gid in IDENTITIES:
            failed = run(container(old_image, uid, gid, "python", "-c",
                                   "import services; import services.moneyflow.service"), check=False)
            if not failed.returncode or not any(word in failed.stderr for word in ("PermissionError", "ModuleNotFoundError")):
                raise RuntimeError("legacy restrictive-context negative control did not fail at import")
        print("LEGACY_RESTRICTIVE_CONTEXT_BOTH_UIDS_NEGATIVE_CONTROL=PASS")
        fixture = scratch / "fixture.py"
        shutil.copyfile(ROOT / "scripts/services_runtime_fixture.py", fixture)
        fixture.chmod(0o444)
        proof_mount = (fixture, "/proof.py", True)
        targets = [image] + ([options.image] if options.image else [])
        for target in targets:
            for uid, gid in (*IDENTITIES, (10001, 10001)):
                # Test DAC with a writable root layer too: --read-only must not
                # mask code ownership mistakes (including the image default UID).
                for readonly in (True, False):
                    result = run(container(target, uid, gid, "python", "/proof.py", "inspect", str(uid), str(gid),
                                           readonly=readonly, mounts=[proof_mount]))
                    print(result.stdout.strip() + f" READ_ONLY={readonly}")
            for uid, gid in IDENTITIES:
                state = scratch / f"state-{uid}-{targets.index(target)}"
                for relative in ("pair", "moneyflow", "runtime/moneyflow", "audit"):
                    (state / relative).mkdir(parents=True, exist_ok=True)
                for name in ("active_pair.json", "current_pairlist.json", "freqtrade-active.json"):
                    shutil.copyfile(ROOT / "shared/pair" / name, state / "pair" / name)
                for path in (state, *state.rglob("*")):
                    path.chmod(0o750 if path.is_dir() else 0o640)
                run(["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(state)])
                mounts = [proof_mount] + [(state / rel, f"/app/shared/{rel}", rel == "pair")
                                          for rel in ("pair", "moneyflow", "runtime/moneyflow", "audit")]
                result = run(container(target, uid, gid, "python", "/proof.py", "collect", str(uid), str(gid), mounts=mounts))
                print(result.stdout.strip())
                other_uid, other_gid = next(identity for identity in IDENTITIES if identity != (uid, gid))
                denied = run(container(target, other_uid, other_gid, "python", "-c",
                                       "from pathlib import Path; Path('/app/shared/moneyflow/foreign').write_text('forbidden')",
                                       mounts=mounts), check=False)
                if not denied.returncode or "PermissionError" not in denied.stderr:
                    raise RuntimeError("foreign identity unexpectedly wrote host state")
                print(f"FOREIGN_UID_STATE_WRITE_DENIED_OWNER_{uid}=PASS")
            metadata = json.loads(run(["docker", "image", "inspect", target]).stdout)[0]
            assert metadata["Config"]["User"] == "bot"
            print(f"IMAGE={metadata['Id']} ARCHITECTURE={metadata['Architecture']}")
        print("SERVICES_RUNTIME_PROOF=PASS; ORDERS_SUBMITTED=0; HOST_DEPLOYMENT=NOT_TESTED")
    finally:
        # Exact newly-created fixtures only; never a real bot project or volume.
        if scratch.parent == Path(tempfile.gettempdir()).resolve() and scratch.name.startswith("bitcoin-services-proof-") and not scratch.is_symlink():
            run(["sudo", "-n", "rm", "-rf", "--one-file-system", "--", str(scratch)])
        for image in images:
            run(["docker", "image", "rm", image], check=False)


if __name__ == "__main__":
    main()
