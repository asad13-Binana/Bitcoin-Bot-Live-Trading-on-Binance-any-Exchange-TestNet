from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "SBOM.cyclonedx.json"
LOCKS = {
    "monitoring": ROOT / "monitoring/requirements-monitoring.lock",
    "services": ROOT / "requirements.services.lock",
}
PACKAGE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def build() -> dict:
    dependencies: dict[str, dict[str, object]] = {}
    lock_hashes = []
    for scope, path in sorted(LOCKS.items()):
        lock_hashes.append({
            "name": f"bitcoin-bot:lock-sha256:{scope}",
            "value": _sha256(path),
        })
        for line in path.read_text(encoding="utf-8").splitlines():
            match = PACKAGE.match(line.strip())
            if not match:
                continue
            name = _normalise(match.group(1))
            version = match.group(2)
            item = dependencies.setdefault(
                name, {"version": version, "scopes": set()})
            if item["version"] != version:
                raise ValueError(
                    f"conflicting locked versions for {name}: "
                    f"{item['version']} and {version}")
            item["scopes"].add(scope)  # type: ignore[union-attr]

    components = []
    for name, item in sorted(dependencies.items()):
        version = str(item["version"])
        purl = f"pkg:pypi/{name}@{version}"
        components.append({
            "bom-ref": purl,
            "name": name,
            "properties": [{
                "name": "bitcoin-bot:dependency-scope",
                "value": ",".join(sorted(item["scopes"])),
            }],
            "purl": purl,
            "type": "library",
            "version": version,
        })

    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "components": components,
        "metadata": {
            "component": {
                "bom-ref": "pkg:generic/bitcoin-bot@1.1",
                "name": "bitcoin-bot",
                "type": "application",
                "version": "1.1",
            },
            "properties": lock_hashes,
            "tools": {"components": [{
                "name": "build_sbom.py",
                "type": "application",
                "version": "1",
            }]},
        },
        "specVersion": "1.6",
        "version": 1,
    }


def payload() -> bytes:
    return (json.dumps(build(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = payload()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            raise SystemExit("SBOM.cyclonedx.json is stale; run scripts/build_sbom.py")
        print("deterministic CycloneDX SBOM verified")
        return
    OUTPUT.write_bytes(expected)
    print(f"wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
