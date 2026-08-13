from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "ARTIFACT_PROVENANCE.json"
SBOM = ROOT / "SBOM.cyclonedx.json"
SOURCE_PLACEHOLDER = "SOURCE_ZIP_NO_GIT_COMMIT"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(args: argparse.Namespace) -> dict:
    commit = args.commit or (ROOT / ".git-commit").read_text(
        encoding="utf-8").strip()
    placeholder = commit == SOURCE_PLACEHOLDER
    required = {
        "repository": args.repository,
        "ref": args.ref,
        "workflow_ref": args.workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
    }
    if not placeholder and any(value in {None, ""} for value in required.values()):
        raise SystemExit("real artifact provenance requires every GitHub Actions field")
    return {
        "attestation": {
            "cryptographic": False,
            "limitation": (
                "GitHub artifact attestations for private repositories require "
                "GitHub Enterprise Cloud; this record is manifest-bound workflow "
                "provenance, not a GitHub or Sigstore attestation"
            ),
            "type": "manifest-bound-github-actions-metadata",
        },
        "builder": {
            "run_attempt": int(args.run_attempt or 0),
            "run_id": int(args.run_id or 0),
            "workflow_ref": args.workflow_ref or "SOURCE_ZIP_NO_GIT_WORKFLOW",
        },
        "release_mode": (ROOT / "RELEASE_MODE").read_text(
            encoding="utf-8").strip(),
        "sbom": {
            "path": SBOM.name,
            "sha256": _sha256(SBOM),
        },
        "schema": "bitcoin-bot-artifact-provenance-v1",
        "source": {
            "commit": commit,
            "ref": args.ref or "SOURCE_ZIP_NO_GIT_REF",
            "repository": args.repository or "SOURCE_ZIP_NO_GIT_REPOSITORY",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit")
    parser.add_argument("--repository")
    parser.add_argument("--ref")
    parser.add_argument("--workflow-ref")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    args = parser.parse_args()
    payload = json.dumps(build(args), indent=2, sort_keys=True) + "\n"
    OUTPUT.write_bytes(payload.encode("utf-8"))
    print(f"wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
