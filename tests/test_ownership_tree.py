"""Real Linux ownership regressions; mounts run only in an isolated CI namespace."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ownership_tree", ROOT / "deploy/ownership_tree.py")
ownership = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ownership)
ROOT_LINUX = pytest.mark.skipif(
    sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires root on disposable Linux",
)
MOUNT_LINUX = pytest.mark.skipif(
    sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0
    or os.environ.get("BITCOIN_MOUNT_TESTS") != "1",
    reason="requires explicitly isolated privileged Linux mount namespace",
)


def owner(path):
    info = path.lstat()
    return info.st_uid, info.st_gid


def run(*args, **kwargs):
    return subprocess.run(list(args), capture_output=True, text=True, check=True, **kwargs)


@contextmanager
def mounted(target, kind, source=None):
    # Only caller-created temporary fixtures; never a deployment or workspace root.
    if kind == "tmpfs":
        run("mount", "-t", "tmpfs", "-o", "size=4m", "ownership-test", str(target))
    else:
        run("mount", "--bind", str(source), str(target))
    try:
        yield
    finally:
        run("umount", "--", str(target))


def test_setup_wires_both_real_ownership_calls_and_retains_root_path_validation():
    setup = (ROOT / "deploy/oracle_setup.sh").read_text()
    assert setup.count('python3 -I "$SCRIPT_DIR/ownership_tree.py" --owner') == 2
    assert '--owner "$BOT_USER:$BOT_GROUP" \\' in setup
    assert '"$APP_ROOT/releases" "$PERSIST" "$CONFIG_ROOT"' in setup
    assert '--owner root:root "$APP_ROOT/monitoring-venvs"' in setup
    assert "chown -R" not in setup
    assert '$(readlink -f "$protected"' in setup
    assert setup.index("[[ $EUID -eq 0 ]]") < setup.index("SCRIPT_DIR=")
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["runtime-container"]["steps"]
                if "mount-boundary" in s.get("name", ""))
    assert "unshare --mount --propagation private" in step["run"]
    assert "BITCOIN_MOUNT_TESTS=1" in step["run"]


@pytest.mark.parametrize("uid,gid", [(-1, 0), (0, -1), (2**32 - 1, 0), (True, 0)])
def test_invalid_owner_never_reaches_filesystem(tmp_path, uid, gid):
    with pytest.raises(ValueError):
        ownership.change_ownership([tmp_path], uid, gid)


@ROOT_LINUX
def test_actual_gnu_chown_rejects_old_option_without_mutation(tmp_path):
    target = tmp_path / "evidence"
    target.write_text("unchanged")
    result = subprocess.run(
        ["chown", "-R", "--one-file-system", "12345:12345", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0 and "one-file-system" in result.stderr
    assert owner(target) == (0, 0) and target.read_text() == "unchanged"


@ROOT_LINUX
@pytest.mark.parametrize("uid,gid", [(12345, 12346), (0, 0)])
def test_cli_changes_roots_files_empty_dirs_and_links_not_targets(tmp_path, uid, gid):
    tree = tmp_path / "tree"
    tree.mkdir()
    nested = tree / "nested"
    nested.mkdir()
    empty = nested / "empty"
    empty.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("do not follow")
    names = ["normal", "-leading-option", "space name", "line\nbreak", "unicodé"]
    files = [nested / name for name in names]
    for path in files:
        path.write_text("data")
    links = [tree / "file-link", tree / "dir-link", tree / "dangling"]
    links[0].symlink_to(sentinel)
    links[1].symlink_to(outside, target_is_directory=True)
    links[2].symlink_to(tmp_path / "absent")
    expected = [tree, nested, empty, *files, *links]
    initial = (12340, 12341) if uid == 0 else (0, 0)
    for path in expected:
        os.chown(path, *initial, follow_symlinks=False)
    result = run(sys.executable, "-I", str(ROOT / "deploy/ownership_tree.py"),
                 "--owner", f"{uid}:{gid}", str(tree))
    assert "descriptor-safe ownership verified" in result.stdout
    assert all(owner(path) == (uid, gid) for path in expected)
    assert owner(sentinel) == (0, 0) and owner(outside) == (0, 0)
    assert sentinel.read_text() == "do not follow"


@ROOT_LINUX
@pytest.mark.parametrize("defect", ["hardlink", "fifo", "root_symlink", "ancestor_symlink"])
def test_preflight_refuses_unsafe_objects_before_changing_any_root(tmp_path, defect):
    good = tmp_path / "good"
    good.mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged")
    target = tree
    if defect == "hardlink":
        os.link(sentinel, tree / "alias")
    elif defect == "fifo":
        os.mkfifo(tree / "fifo")
    elif defect == "root_symlink":
        target = tmp_path / "alias"
        target.symlink_to(tree, target_is_directory=True)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        target = alias / "tree"
    with pytest.raises((ValueError, OSError)):
        ownership.change_ownership([good, target], 12345, 12346)
    assert owner(good) == owner(tree) == owner(sentinel) == (0, 0)
    assert sentinel.read_text() == "unchanged"


@MOUNT_LINUX
@pytest.mark.parametrize("kind", ["tmpfs", "bind", "file_bind"])
@pytest.mark.parametrize("uid,gid", [(12345, 12346), (0, 0)])
def test_mount_and_contents_not_owned_including_same_device_bind(tmp_path, kind, uid, gid):
    tree = tmp_path / "tree"
    tree.mkdir()
    ordinary = tree / "ordinary"
    ordinary.write_text("normal")
    source = tmp_path / "source"
    target = tree / "mounted"
    if kind == "file_bind":
        source.write_text("foreign")
        target.write_text("covered")
    else:
        source.mkdir()
        (source / "sentinel").write_text("foreign")
        target.mkdir()
    initial = (12340, 12341) if uid == 0 else (0, 0)
    os.chown(tree, *initial)
    os.chown(ordinary, *initial)
    with mounted(target, kind, source):
        sentinel = target if kind == "file_bind" else target / "sentinel"
        if kind == "tmpfs":
            sentinel.write_text("foreign")
            assert target.stat().st_dev != tree.stat().st_dev
        else:
            assert target.stat().st_dev == tree.stat().st_dev
        os.chown(target, *initial)
        os.chown(sentinel, *initial)
        with pytest.raises(OSError) as error:
            ownership.change_ownership([tree], uid, gid)
        assert error.value.errno == 18  # EXDEV, including same-device bind mounts.
        assert owner(tree) == owner(ordinary) == owner(target) == owner(sentinel) == initial
        assert sentinel.read_text() == "foreign"


@MOUNT_LINUX
@pytest.mark.parametrize("kind", ["tmpfs", "bind"])
def test_selected_root_mount_is_refused_before_ownership(tmp_path, kind):
    tree = tmp_path / "tree"
    tree.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    with mounted(tree, kind, source):
        with pytest.raises(OSError) as error:
            ownership.change_ownership([tree], 12345, 12346)
        assert error.value.errno == 18
        assert owner(tree) == (0, 0)


@MOUNT_LINUX
@pytest.mark.parametrize("kind", ["tmpfs", "bind"])
def test_find_xdev_counterexample_changes_mount_root(tmp_path, kind):
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tree / "mounted"
    target.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    with mounted(target, kind, source):
        sentinel = target / "sentinel"
        sentinel.write_text("foreign")
        run("find", str(tree), "-xdev", "-exec", "chown", "--no-dereference",
            "12345:12346", "{}", "+")
        assert owner(target) == (12345, 12346)  # -xdev still visits the mount root.
        expected = (0, 0) if kind == "tmpfs" else (12345, 12346)
        assert owner(sentinel) == expected  # Same-device bind traversal is NOT pruned.


@ROOT_LINUX
def test_replaced_path_does_not_redirect_descriptor_chown(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    path = tree / "file"
    path.write_text("original")
    original_inode = path.stat().st_ino
    outside = tmp_path / "outside"
    outside.write_text("foreign")
    real_chown = ownership.LinuxOwnership.chown

    def replace_after_open(api, fd, uid, gid):
        if os.fstat(fd).st_ino == original_inode:
            path.rename(tree / "retained")
            path.symlink_to(outside)
        real_chown(api, fd, uid, gid)

    monkeypatch.setattr(ownership.LinuxOwnership, "chown", replace_after_open)
    ownership.change_ownership([tree], 12345, 12346)
    assert owner(tree / "retained") == (12345, 12346)
    assert owner(outside) == (0, 0) and outside.read_text() == "foreign"


@ROOT_LINUX
def test_replaced_inventory_object_fails_closed(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    path = tree / "file"
    path.write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("foreign")
    real_inventory = ownership.inventory

    def replace_after_inventory(api, fd):
        result = real_inventory(api, fd)
        path.rename(tree / "retained")
        path.symlink_to(outside)
        return result

    monkeypatch.setattr(ownership, "inventory", replace_after_inventory)
    with pytest.raises(ValueError, match="changed after inventory"):
        ownership.change_ownership([tree], 12345, 12346)
    assert owner(outside) == (0, 0)


@ROOT_LINUX
def test_setup_rejects_nonroot_before_any_setup_command():
    result = subprocess.run(
        ["bash", "-s"], input=(ROOT / "deploy/oracle_setup.sh").read_text(),
        user=65534, group=65534, extra_groups=[], cwd="/",
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "run setup through sudo" in result.stderr
    assert "SCRIPT_DIR" not in result.stderr


@ROOT_LINUX
def test_root_setup_prefix_accepts_explicit_unprivileged_administrator(tmp_path):
    import pwd

    deploy_user = pwd.getpwuid(65534).pw_name
    prefix = (ROOT / "deploy/oracle_setup.sh").read_text().split(
        "# This installer intentionally targets", 1,
    )[0]
    shutil.copyfile(ROOT / "deploy/instance_identity.sh", tmp_path / "instance_identity.sh")
    script = tmp_path / "oracle_setup.sh"
    script.write_text(prefix + '\nprintf "%s:%s:%s\\n" "$EUID" "$DEPLOY_USER" "$DEPLOYMENT_PROFILE"\n')
    result = run("bash", str(script), env={
        **os.environ, "DEPLOY_USER": deploy_user,
        "DEPLOYMENT_PROFILE": "single-bot-experiment",
    })
    assert f"0:{deploy_user}:single-bot-experiment" in result.stdout
    assert "gpasswd" not in prefix and "usermod" not in prefix
