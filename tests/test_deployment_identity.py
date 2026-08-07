from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_stack_identity", ROOT / "deploy/verify_stack_identity.py"
)
identity = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(identity)


RELEASE_HASH = "a" * 64
CONFIG_HASH = "b" * 64
RELEASE_DIR = Path("/opt/bitcoin-bot/releases/20260722T000000Z")
PROJECT = "bitcoin-bot"


def fixtures():
    images = {
        "moneyflow": "bitcoin-bot-services:bitcoin-aaaaaaaaaaaaaaaa",
        "freqtrade": "freqtradeorg/freqtrade:2026.6@sha256:" + "d" * 64,
        "execution-sidecar": "bitcoin-bot-services:bitcoin-aaaaaaaaaaaaaaaa",
        "telegram-broker": "bitcoin-bot-services:bitcoin-aaaaaaaaaaaaaaaa",
    }
    compose = {"services": {name: {"image": image} for name, image in images.items()}}
    image_ids = {image: "sha256:" + str(index) * 64 for index, image in enumerate(set(images.values()), 1)}
    containers = []
    for name, image in images.items():
        containers.append({
            "Image": image_ids[image],
            "Config": {
                "Image": image,
                "Env": [
                    f"DEPLOYED_RELEASE_HASH={RELEASE_HASH}",
                    f"DEPLOYED_CONFIG_SHA256={CONFIG_HASH}",
                ],
                "Labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": name,
                    "com.docker.compose.project.config_files": str(RELEASE_DIR / "docker-compose.yml"),
                    "com.docker.compose.project.working_dir": str(RELEASE_DIR),
                },
            },
            "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy"}},
        })
    return compose, containers, image_ids


def validate(compose, containers, image_ids):
    identity.validate_snapshot(
        compose_config=compose,
        containers=containers,
        image_ids=image_ids,
        release_dir=RELEASE_DIR,
        release_hash=RELEASE_HASH,
        config_sha256=CONFIG_HASH,
        project=PROJECT,
    )


def test_exact_stack_identity_passes():
    validate(*fixtures())


@pytest.mark.parametrize("defect", [
    "missing", "extra", "duplicate", "release", "config", "compose_file",
    "working_dir", "project", "image_reference", "image_id", "stopped", "unhealthy",
])
def test_stack_identity_rejects_wrong_or_unhealthy_container(defect):
    compose, containers, image_ids = fixtures()
    if defect == "missing":
        containers.pop()
    elif defect == "extra":
        containers.append(copy.deepcopy(containers[0]))
        containers[-1]["Config"]["Labels"]["com.docker.compose.service"] = "unexpected"
    elif defect == "duplicate":
        containers[1]["Config"]["Labels"]["com.docker.compose.service"] = "moneyflow"
    elif defect == "release":
        containers[0]["Config"]["Env"][0] = "DEPLOYED_RELEASE_HASH=" + "c" * 64
    elif defect == "config":
        containers[0]["Config"]["Env"][1] = "DEPLOYED_CONFIG_SHA256=" + "c" * 64
    elif defect == "compose_file":
        containers[0]["Config"]["Labels"]["com.docker.compose.project.config_files"] = "/tmp/wrong.yml"
    elif defect == "working_dir":
        containers[0]["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/tmp/wrong"
    elif defect == "project":
        containers[0]["Config"]["Labels"]["com.docker.compose.project"] = "wrong"
    elif defect == "image_reference":
        containers[0]["Config"]["Image"] = "wrong:image"
    elif defect == "image_id":
        containers[0]["Image"] = "sha256:" + "f" * 64
    elif defect == "stopped":
        containers[0]["State"].update(Running=False, Status="exited")
    elif defect == "unhealthy":
        containers[0]["State"]["Health"]["Status"] = "unhealthy"
    with pytest.raises(identity.StackIdentityError):
        validate(compose, containers, image_ids)


def test_stack_identity_rejects_compose_service_drift():
    compose, containers, image_ids = fixtures()
    compose["services"]["fifth-service"] = {"image": "unexpected"}
    with pytest.raises(identity.StackIdentityError):
        validate(compose, containers, image_ids)
