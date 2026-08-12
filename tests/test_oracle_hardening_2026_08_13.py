from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_strategy_full_file_remains_frozen():
    strategy = ROOT / "freqtrade/user_data/strategies/IctSmcStrategy.py"
    assert hashlib.sha256(strategy.read_bytes()).hexdigest() == (
        "023d5f9a09c3a9057986ec1a79fe74cc987b79dd52c3453683d09d023f08e340"
    )


def test_oracle_target_is_ubuntu_2404_arm64_by_default():
    setup = text("deploy/oracle_setup.sh")
    assert "REQUIRED_UBUNTU_VERSION=${REQUIRED_UBUNTU_VERSION:-24.04}" in setup
    assert "REQUIRE_ARM64=${REQUIRE_ARM64:-true}" in setup
    assert "Oracle A1 target requires arm64" in setup
    assert "PHYSICAL_MEMORY_MIB >= 1400" in setup


def test_docker_uses_current_official_repository_without_convenience_script():
    setup = text("deploy/oracle_setup.sh")
    assert "Types: deb" in setup
    assert "URIs: https://download.docker.com/linux/ubuntu" in setup
    assert "Signed-By: /etc/apt/keyrings/docker.asc" in setup
    assert "/etc/apt/sources.list.d/docker.sources" in setup
    assert all(
        package in setup
        for package in (
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
        )
    )
    assert "get.docker.com" not in setup
    assert "DOCKER_VERSION=${DOCKER_VERSION:-}" in setup


def test_deployment_user_and_runner_have_no_docker_group_privilege():
    setup = text("deploy/oracle_setup.sh")
    assert 'usermod -aG docker "$DEPLOY_USER"' not in setup
    assert 'gpasswd -d "$DEPLOY_USER" docker' in setup
    assert "ENABLE_GITHUB_RUNNER=${ENABLE_GITHUB_RUNNER:-false}" in setup
    assert "for forbidden_group in docker sudo adm lxd disk root" in setup
    assert "GitHub self-hosted runner disabled by default" in setup


def test_testnet_identity_and_endpoint_fail_closed():
    env = text(".env.example")
    installer = text("deploy/install_artifact.sh")
    wrapper = text("deploy/bitcoin-bot-deploy")
    assert "BOT_PRODUCT=BITCOIN-BOT" in env
    assert "BOT_ENVIRONMENT=TESTNET" in env
    assert "BOT_INSTANCE_ID=BITCOIN-TN-TYO-01" in env
    assert "this TestNet package requires BOT_ENVIRONMENT=TESTNET" in installer
    assert "testnet package rejects a non-Testnet Binance execution endpoint" in installer
    assert "self-hosted wrapper permits EXECUTION_MODE=simulation only" in wrapper
    assert "BINANCE_API_KEY" in wrapper and "requires empty Binance credentials" in wrapper


def test_monitoring_defaults_to_unique_loopback_port_and_detects_collision():
    config = text("monitoring/api/configuration.py")
    installer = text("deploy/install_monitoring.sh")
    assert '_int("MONITOR_PORT", 8091, 1, 65535)' in config
    assert "MONITOR_PORT=${MONITOR_PORT:-8091}" in installer
    assert "MONITOR_BIND_HOST must be loopback" in installer
    assert "monitor port ${MONITOR_BIND_HOST}:${MONITOR_PORT} is already occupied" in installer
    for example in (
        "monitoring/.env.monitor.simulation.example",
        "monitoring/.env.monitor.testnet.example",
        "monitoring/.env.monitor.live.example",
    ):
        payload = text(example)
        assert "MONITOR_PORT=8091" in payload
        assert "MONITOR_URL=http://127.0.0.1:8091" in payload


def test_compose_has_bounded_resources_logs_and_no_public_ports_or_socket():
    raw = text("docker-compose.yml")
    compose = yaml.safe_load(raw)
    assert compose["name"] == "bitcoin-bot"
    assert set(compose["services"]) == {
        "moneyflow", "freqtrade", "execution-sidecar", "telegram-broker"
    }
    for service in compose["services"].values():
        assert "ports" not in service
        assert service.get("mem_limit")
        assert float(service.get("cpus", 0)) > 0
        assert int(service.get("pids_limit", 0)) > 0
        assert service.get("restart") == "unless-stopped"
        assert service.get("logging", {}).get("driver") == "json-file"
        assert "/var/run/docker.sock" not in raw
    assert sum(float(service["cpus"]) for service in compose["services"].values()) <= 1.0


def test_resource_guard_is_root_only_bounded_and_project_scoped():
    guard = text("deploy/resource_guard.sh")
    service = text("deploy/systemd/bitcoin-bot-resource-guard.service")
    timer = text("deploy/systemd/bitcoin-bot-resource-guard.timer")
    assert "resource_guard.sh must run as root" in guard
    assert "DISK_WARN_PERCENT=${DISK_WARN_PERCENT:-85}" in guard
    assert "DISK_CRITICAL_PERCENT=${DISK_CRITICAL_PERCENT:-95}" in guard
    assert "com.docker.compose.project=$PROJECT" in guard
    assert "EXPECTED_SERVICES=(moneyflow freqtrade execution-sidecar telegram-broker)" in guard
    assert "CONTAINER_STATUS_FILE=$PERSIST/runtime/container_status.json" in guard
    assert '[docker, "inspect", *identifiers]' in guard
    assert "ProtectSystem=strict" in service
    assert "OnUnitActiveSec=1min" in timer


def test_monitoring_runtime_has_no_docker_socket_or_cli_access():
    installer = text("deploy/install_monitoring.sh")
    unit = text("monitoring/systemd/bitcoin-bot-monitor-snapshot.service")
    assert "rm -f /usr/local/libexec/bitcoin-bot-monitor-snapshot" in installer
    assert "User=botmon" in unit
    assert "ExecStart=/usr/bin/test -r" in unit
    assert "docker.sock" not in unit
    assert "/usr/bin/docker" not in unit


def test_backup_uses_sqlite_online_backup_and_root_only_validation():
    backup = text("deploy/backup_state.sh")
    verify = text("deploy/verify_backup.sh")
    assert "backup_state.sh must run as root" in backup
    assert ".backup '$target'" in backup
    assert "PRAGMA quick_check;" in backup
    assert "config-snapshots.tar.gz" in backup
    assert "audit-evidence.tar.gz" in backup
    assert "SHA256SUMS" in backup
    assert "verify_backup.sh must run as root" in verify
    assert "sha256sum -c SHA256SUMS" in verify
    assert "unsafe archive path" in verify


def test_oracle_diagnostic_is_redacted_and_uses_real_https_timings():
    diagnostic = text("deploy/oracle_validate.sh")
    assert "TIMING_SAMPLES=${TIMING_SAMPLES:-10}" in diagnostic
    assert "https://testnet.binance.vision/api/v3/time" in diagnostic
    for metric in (
        "time_namelookup", "time_connect", "time_appconnect",
        "time_starttransfer", "time_total",
    ):
        assert metric in diagnostic
    assert "median=" in diagnostic and "p95=" in diagnostic
    assert "Authorization: Bearer Oracle" in diagnostic
    assert "BINANCE_API_SECRET" not in diagnostic
    assert "TELEGRAM_BOT_TOKEN" not in diagnostic


def test_bitcoin_namespace_does_not_use_binana_runtime_paths():
    forbidden = (
        "/opt/binance-freqtrade-v101",
        "/etc/binance-freqtrade-v101",
        "/var/lib/binance-freqtrade-v101",
        "/var/log/binance-freqtrade-v101",
    )
    audited = [
        "deploy/oracle_setup.sh",
        "deploy/install_artifact.sh",
        "deploy/install_monitoring.sh",
        "deploy/bitcoin-bot-deploy",
        "deploy/oracle_validate.sh",
        "deploy/resource_guard.sh",
        "deploy/backup_state.sh",
        "docker-compose.yml",
    ]
    for path in audited:
        payload = text(path)
        assert not any(value in payload for value in forbidden), path


def test_security_updates_keep_automatic_reboot_disabled_and_chrony_bounded():
    setup = text("deploy/oracle_setup.sh")
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in setup
    assert "chronyc waitsync 30 \"$CHRONY_MAX_OFFSET_SECONDS\"" in setup
    assert "vm.swappiness=10" in setup
    assert "chmod 0600 \"$SWAP_FILE\"" in setup
