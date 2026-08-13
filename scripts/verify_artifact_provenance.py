from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "SOURCE_ZIP_NO_GIT_COMMIT"
REPOSITORIES = {
    "live": "asad13-Binana/Bitcoin-Bot-Live-Trading-on-Binance-any-Exchange",
    "testnet": (
        "asad13-Binana/"
        "Bitcoin-Bot-Live-Trading-on-Binance-any-Exchange-TestNet"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--deployment",
        action="store_true",
        help="require provenance from the main-branch GitHub artifact job",
    )
    args = parser.parse_args()
    errors = []
    try:
        provenance = json.loads((ROOT / "ARTIFACT_PROVENANCE.json").read_text(
            encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"artifact provenance is unreadable: {exc}") from exc

    mode = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
    commit = (ROOT / ".git-commit").read_text(encoding="utf-8").strip()
    source = provenance.get("source", {})
    builder = provenance.get("builder", {})
    sbom = provenance.get("sbom", {})
    attestation = provenance.get("attestation", {})
    if provenance.get("schema") != "bitcoin-bot-artifact-provenance-v1":
        errors.append("unsupported artifact provenance schema")
    if provenance.get("release_mode") != mode:
        errors.append("artifact provenance mode mismatch")
    if source.get("commit") != commit:
        errors.append("artifact provenance commit differs from .git-commit")
    if attestation.get("cryptographic") is not False:
        errors.append("artifact provenance misstates cryptographic status")
    if attestation.get("type") != "manifest-bound-github-actions-metadata":
        errors.append("artifact provenance type is invalid")
    sbom_path = ROOT / str(sbom.get("path", ""))
    if sbom_path != ROOT / "SBOM.cyclonedx.json" or not sbom_path.is_file():
        errors.append("artifact provenance SBOM path is invalid")
    elif sbom.get("sha256") != _sha256(sbom_path):
        errors.append("artifact provenance SBOM digest mismatch")

    if commit == PLACEHOLDER:
        expected = {
            "repository": "SOURCE_ZIP_NO_GIT_REPOSITORY",
            "ref": "SOURCE_ZIP_NO_GIT_REF",
        }
        if any(source.get(key) != value for key, value in expected.items()):
            errors.append("source placeholder provenance is malformed")
        if builder.get("workflow_ref") != "SOURCE_ZIP_NO_GIT_WORKFLOW":
            errors.append("source placeholder workflow is malformed")
        if args.deployment:
            errors.append("source placeholder cannot be deployed")
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append("artifact commit is not a full Git SHA")
        expected_repository = REPOSITORIES.get(mode)
        if source.get("repository") != expected_repository:
            errors.append("artifact provenance repository mismatch")
        ref = source.get("ref", "")
        if not re.fullmatch(r"refs/(heads/[^\s]+|pull/\d+/merge)", ref):
            errors.append("artifact provenance ref is invalid")
        workflow_ref = builder.get("workflow_ref", "")
        prefix = f"{expected_repository}/.github/workflows/ci.yml@"
        if not isinstance(workflow_ref, str) or not workflow_ref.startswith(prefix):
            errors.append("artifact provenance workflow identity mismatch")
        if int(builder.get("run_id", 0)) <= 0:
            errors.append("artifact provenance run_id is invalid")
        if int(builder.get("run_attempt", 0)) <= 0:
            errors.append("artifact provenance run_attempt is invalid")
        if args.deployment and ref != "refs/heads/main":
            errors.append("deployment requires a main-branch artifact")

    if errors:
        raise SystemExit("\n".join(errors))
    print("manifest-bound artifact provenance verified")


if __name__ == "__main__":
    main()
