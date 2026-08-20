import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {
    "freqtrade/user_data/strategies/IctSmcStrategy.py":
        "023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340",
}


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_protected_strategy_hash_is_unchanged():
    for relative, expected in PROTECTED.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected


def test_offhost_backup_is_encrypted_immutable_and_instance_principal_only():
    script = read("deploy/offhost_backup.sh")
    assert "age --encrypt --recipient" in script
    assert "OCI_CLI_AUTH=instance_principal" in script
    assert "--auth instance_principal" in script
    assert "--no-overwrite" in script
    assert "--no-multipart" in script
    assert "--verify-checksum" in script
    assert "--opc-checksum-algorithm SHA256" in script
    assert "--opc-content-sha256" in script
    assert "os.chown(temporary, 0, path.parent.stat().st_gid)" in script
    assert "os.chmod(temporary, 0o640)" in script
    assert "os.chmod(temporary, 0o600)" not in script
    assert "verified-download.age" in script
    assert "--pull never" in script and "--read-only" in script
    assert "--cap-drop ALL" in script and "no-new-privileges" in script
    assert "api_key" not in script.lower()
    assert not re.search(r"os\s+object\s+(delete|rename|restore)", script)


def test_oci_cli_container_is_digest_pinned_for_both_supported_architectures():
    script = read("deploy/offhost_backup.sh")
    digests = re.findall(r"ghcr\.io/oracle/oci-cli:[^'\"]+@sha256:[0-9a-f]{64}", script)
    assert len(set(digests)) == 2


def test_private_recovery_identity_is_not_configured_on_the_vm():
    template = read("deploy/offhost-backup.env.example")
    assert "AGE_RECIPIENT=" in template
    assert "AGE_IDENTITY" not in template
    assert "PRIVATE_KEY" not in template
    assert "OFFHOST_BACKUP_ENABLED=false" in template


def test_restore_is_staging_only_and_rejects_unsafe_archives():
    restore = read("deploy/stage_offhost_restore.sh")
    assert "live state was not modified" in restore
    assert "path.is_absolute()" in restore
    assert '".." in path.parts' in restore
    assert "member.isfile() or member.isdir()" in restore
    assert "--keep-old-files" in restore
    assert "--no-same-owner" in restore
    assert "verify_backup.sh" in restore
    assert "/var/lib/bitcoin-bot/shared" not in restore


def test_systemd_orders_local_then_offhost_and_offhost_requires_explicit_enable():
    local_service = read("deploy/systemd/bitcoin-bot-state-backup.service")
    local_timer = read("deploy/systemd/bitcoin-bot-state-backup.timer")
    offhost_service = read("deploy/systemd/bitcoin-bot-offhost-backup.service")
    offhost_timer = read("deploy/systemd/bitcoin-bot-offhost-backup.timer")
    setup = read("deploy/oracle_setup.sh")
    assert "backup_state.sh" in local_service
    assert "Persistent=true" in local_timer
    assert "After=docker.service bitcoin-bot-state-backup.service network-online.target" in offhost_service
    assert "Persistent=true" in offhost_timer
    assert 'enable --now "${SYSTEMD_PREFIX}-resource-guard.timer"' in setup
    assert '"${SYSTEMD_PREFIX}-state-backup.timer"' in setup
    assert 'enable --now "${SYSTEMD_PREFIX}-offhost-backup.timer"' not in setup


def test_external_alarm_uses_oracles_grouped_absence_query_and_no_instance_auth():
    alarm = read("deploy/create_oci_host_loss_alarm.sh")
    assert ".groupBy(resourceId).absent(15m)" in alarm
    assert "--namespace oci_computeagent" in alarm
    assert "--severity CRITICAL" in alarm
    assert "--pending-duration PT5M" in alarm
    assert "--repeat-notification-duration PT6H" in alarm
    assert "OCI_CLI_AUTH:-} != instance_principal" in alarm


def test_no_artificial_activity_or_false_permanence_claim():
    combined = "\n".join(
        read(path) for path in (
            "deploy/offhost_backup.sh",
            "deploy/create_oci_host_loss_alarm.sh",
            "docs/ORACLE_HOST_LOSS_RESILIENCE.md",
        )
    ).lower()
    for forbidden in ("stress-ng", "cpuburn", "keepalive traffic"):
        assert forbidden not in combined
    assert "make an always free vm permanent" in combined
    assert "never generate artificial cpu or network traffic" in combined
