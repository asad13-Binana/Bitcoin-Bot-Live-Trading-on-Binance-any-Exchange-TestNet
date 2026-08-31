"""Non-core AWS/Oracle runtime regressions; root cases run on disposable CI VMs."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_locks", ROOT / "deploy/prepare_runtime_locks.py")
locks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(locks)
ROOT_LINUX = pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires root on a disposable Linux CI VM",
)


def test_derived_image_keeps_the_pinned_upstream_and_trading_mounts():
    runtime = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    offline = yaml.safe_load((ROOT / "freqtrade/docker-compose.yml").read_text())
    mode = (ROOT / "RELEASE_MODE").read_text().strip()
    service = runtime["services"]["freqtrade"]
    recipe = (ROOT / "Dockerfile.freqtrade").read_text()
    assert "FROM " + offline["services"]["freqtrade"]["image"] in recipe
    assert service["image"] == "bitcoin-" + mode + "-freqtrade:${RELEASE_TAG:-local}"
    assert service["build"] == {"context": ".", "dockerfile": "Dockerfile.freqtrade"}
    assert 'test "$(stat -c \'%u:%a\' /home/ftuser)" = 1000:700' in recipe
    assert "chmod 0711 /home/ftuser" in recipe
    assert "ENV PYTHONUSERBASE=/home/ftuser/.local" in recipe
    assert recipe.rstrip().endswith("USER ftuser")
    assert not any(line.startswith(("ENTRYPOINT", "CMD", "COPY", "ADD")) for line in recipe.splitlines())
    assert "pip install" not in recipe
    assert service["user"] == "${BOT_UID:-1000}:${BOT_GID:-1000}"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert runtime["services"]["execution-sidecar"]["environment"]["AUTO_CONFIRM"] == "false"


def test_image_build_and_cleanup_cover_both_release_bound_images():
    installer = (ROOT / "deploy/install_artifact.sh").read_text()
    assert 'build moneyflow freqtrade' in installer
    assert 'for built_image in "$SERVICE_IMAGE" "$FREQTRADE_IMAGE"' in installer
    assert '"$FREQTRADE_IMAGE:bitcoin-${candidate_hash:0:16}"' in installer


def test_runtime_lock_paths_and_callers_are_reboot_safe():
    paths = locks.identity_locks(ROOT / "deploy/instance_identity.sh")
    assert len(paths) == 4 and len(set(paths)) == 4
    assert all(path.parent == Path("/run/lock") for path in paths)
    for name in ("bitcoin-bot-deploy", "install_artifact.sh", "backup_state.sh"):
        text = (ROOT / "deploy" / name).read_text()
        assert "prepare_runtime_locks.py" in text
        assert text.index("prepare_runtime_locks.py") < text.index("exec 9>" if name != "bitcoin-bot-deploy" else "exec 8>")
    setup = (ROOT / "deploy/oracle_setup.sh").read_text()
    assert '"$ROOT_LIBEXEC/prepare_runtime_locks.py"' in setup
    assert 'touch "$INSTALL_LOCK"' not in setup
    assert 'touch "$BACKUP_LOCK"' not in setup


@pytest.mark.parametrize("replacement", [
    "/var/lock/bitcoin-testnet.install.lock", "/run/lock/binana-testnet.install.lock",
    "/run/lock/bitcoin-live.install.lock", "/tmp/wrong",
])
def test_lock_identity_rejects_aliases_cross_instance_and_arbitrary_paths(tmp_path, replacement):
    identity = (ROOT / "deploy/instance_identity.sh").read_text()
    mode = (ROOT / "RELEASE_MODE").read_text().strip()
    replacement = replacement.replace("bitcoin-live", "bitcoin-testnet") if mode == "live" else replacement
    old = f"/run/lock/bitcoin-{mode}.install.lock"
    copy = tmp_path / "identity"
    copy.write_text(identity.replace(old, replacement))
    with pytest.raises(ValueError):
        locks.identity_locks(copy)


@ROOT_LINUX
def test_real_locks_recreated_without_replacing_active_inode(tmp_path):
    path = tmp_path / "lock"
    locks.prepare_lock(path)
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    path.write_text("owner metadata")
    locks.prepare_lock(path)
    assert path.stat().st_ino == info.st_ino
    assert path.read_text() == "owner metadata"
    path.unlink()  # Simulates /run being cleared at reboot, only inside this fixture.
    locks.prepare_lock(path)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@ROOT_LINUX
@pytest.mark.parametrize("defect", ["symlink", "hardlink", "owner", "group", "mode", "fifo"])
def test_real_locks_reject_unsafe_file_without_repair(tmp_path, defect):
    target = tmp_path / "target"
    target.write_text("must remain unchanged")
    target.chmod(0o600)
    path = tmp_path / "lock"
    if defect == "symlink":
        path.symlink_to(target)
    elif defect == "hardlink":
        os.link(target, path)
    elif defect == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("unsafe")
        path.chmod(0o600)
        if defect == "owner":
            os.chown(path, 12345, 0)
        elif defect == "group":
            os.chown(path, 0, 12345)
        else:
            path.chmod(0o644)
    before = path.lstat()
    with pytest.raises((ValueError, OSError)):
        locks.prepare_lock(path)
    after = path.lstat()
    assert (before.st_uid, before.st_gid, before.st_mode, before.st_ino) == (
        after.st_uid, after.st_gid, after.st_mode, after.st_ino
    )
    assert target.read_text() == "must remain unchanged"


@ROOT_LINUX
def test_real_locks_reject_nonsticky_shared_or_symlinked_parent(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(ValueError, match="directory"):
        locks.prepare_lock(unsafe / "lock")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        locks.prepare_lock(alias / "lock")
    assert not (tmp_path / "lock").exists()


@ROOT_LINUX
def test_complete_backup_uses_canonical_parent_and_restorable_sqlite(tmp_path):
    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 CLI is required")
    release = tmp_path / "release"
    release.mkdir()
    parent = tmp_path / "persistent"
    shared = parent / "shared"
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    for relative in ("audit", "runtime/sidecar", "runtime/moneyflow", "runtime/telegram"):
        (shared / relative).mkdir(parents=True)
    snapshots = parent / "config-snapshots"
    snapshots.mkdir()
    (snapshots / "fixture.env").write_text("EXECUTION_MODE=simulation\n")
    db = shared / "runtime/sidecar/execution_state.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('fixture only')")
    telegram_db = shared / "runtime/telegram/telegram_updates.sqlite3"
    with sqlite3.connect(telegram_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE processed (update_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO processed VALUES (123)")
    for name in ("backup_state.sh", "verify_backup.sh"):
        shutil.copyfile(ROOT / "deploy" / name, release / name)
    # Lock security is executed separately above. Keep this end-to-end backup
    # fixture completely outside production /run, /var/lib and /var/backups.
    (release / "prepare_runtime_locks.py").write_text("pass\n")
    (release / "instance_identity.sh").write_text(
        f"readonly PERSIST_PARENT={parent}\nreadonly PERSIST={shared}\n"
        f"readonly CONFIG_ROOT={snapshots}\nreadonly BACKUP_ROOT={backups}\n"
        f"readonly BACKUP_LOCK={tmp_path / 'backup.lock'}\n"
    )
    result = subprocess.run(["bash", str(release / "backup_state.sh")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    created = list(backups.iterdir())
    import tarfile
    with tarfile.open(created[0] / "deployment-metadata.tar.gz") as archive:
        assert not any(".sqlite" in member.name for member in archive.getmembers())
    assert len(created) == 1
    verified = subprocess.run(["bash", str(release / "verify_backup.sh"), str(created[0])],
                              capture_output=True, text=True, timeout=30)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert not list((created[0] / "sqlite").glob("*-wal"))
    assert not list((created[0] / "sqlite").glob("*-shm"))
    with sqlite3.connect(created[0] / "sqlite/telegram_updates.sqlite3") as conn:
        assert conn.execute("SELECT update_id FROM processed").fetchone() == (123,)
    with sqlite3.connect(created[0] / "sqlite/execution_state.sqlite") as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT value FROM evidence").fetchone() == ("fixture only",)


@pytest.mark.parametrize("service", ["moneyflow", "execution-sidecar", "telegram-broker"])
@pytest.mark.parametrize("payload,healthy", [
    ({"ok": True, "ts": 1000}, True),
    ({"ok": False, "ts": 1000}, False),
    ({"ok": "false", "ts": 1000}, False),
    ({"ok": 1, "ts": 1000}, False),
    ({"ok": True, "ts": 1000000}, False),
    ({"ok": True, "ts": -1000000}, False),
    ({"ok": True, "ts": float("nan")}, False),
    ({"ok": True, "ts": float("inf")}, False),
    ({"ok": True, "ts": True}, False),
    ({"ok": True, "ts": "1000"}, False),
    ({"ok": True}, False),
])
def test_actual_compose_health_program_rejects_stale_future_or_truthy_state(service, payload, healthy):
    from unittest.mock import mock_open, patch

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    command = compose["services"][service]["healthcheck"]["test"][1]
    program = command.removeprefix('python -c "').removesuffix('"')
    # optimise=2 models PYTHONOPTIMIZE: health must not rely on removable assert.
    with patch("builtins.open", mock_open()), patch("json.load", return_value=payload), \
            patch("time.time", return_value=1000), pytest.raises(SystemExit) as outcome:
        exec(compile(program, "<actual-compose-health>", "exec", optimize=2), {})
    assert bool(outcome.value.code) is (not healthy)


def test_offhost_glob_finds_real_timestamp_not_ten_digit_date():
    import fnmatch
    import re

    source = ROOT / "deploy/offhost_backup.sh"
    if not source.exists():
        return  # LIVE intentionally has no off-host uploader yet.
    script = source.read_text()
    match = re.search(r"-name '(20[?]+T[?]+Z)'", script)
    assert match
    assert fnmatch.fnmatchcase("20260829T225013Z", match[1])
    assert not fnmatch.fnmatchcase("2026000829T225013Z", match[1])
