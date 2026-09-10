#!/usr/bin/env python3
"""Verify a release image against its clean, pinned Harbor source checkout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")


def check_evidence(
    expected: dict,
    evidence: dict,
    *,
    manifest: dict | None = None,
    require_hardened: bool = False,
) -> None:
    """Check source, installed runtime, image labels, and optional dataset agree."""
    if require_hardened:
        require_equal("release contract", expected["runtime_contract_version"], 2)
        require_equal(
            "release profile", expected["harness_profile"], "loca-hardened-react-v2"
        )
        require_equal(
            "release agent major", expected["agent_version"].split(".")[0], "2"
        )
    if expected["runtime_contract_version"] >= 2:
        require_equal(
            "runtime isolation policy",
            expected.get("isolation_policy"),
            "landlock-seccomp-v1",
        )
        require_equal(
            "runtime isolation profile",
            expected["harness_profile"],
            "loca-hardened-react-v2",
        )
    contract = evidence["contract"]
    for key, value in expected.items():
        if key not in {"adapter_commit", "runtime_image"}:
            require_equal(f"runtime {key}", contract.get(key), value)
    require_equal(
        "image source commit", evidence["adapter_commit"], expected["adapter_commit"]
    )
    require_equal(
        "image upstream checkout",
        evidence["upstream_commit"],
        expected["upstream_commit"],
    )
    require_equal(
        "image task index", evidence["task_index_sha256"], contract["task_index_sha256"]
    )
    require_equal(
        "image patch manifest",
        evidence["patch_manifest_sha256"],
        expected["patch_manifest_sha256"],
    )
    if manifest is not None:
        pairs = {
            "adapter commit": (
                manifest["adapter"]["commit"],
                expected["adapter_commit"],
            ),
            "upstream commit": (
                manifest["upstream"]["commit"],
                expected["upstream_commit"],
            ),
            "runtime image": (manifest["runtime"]["image"], expected["runtime_image"]),
            "runtime contract": (
                manifest["adapter"]["contract_version"],
                expected["runtime_contract_version"],
            ),
            "harness profile": (
                manifest["profile"]["name"],
                expected["harness_profile"],
            ),
            "agent version": (
                manifest["adapter"]["agent"],
                f"{expected['agent_name']}@{expected['agent_version']}",
            ),
            "task index": (
                manifest["runtime"]["task_index_sha256"],
                evidence["task_index_sha256"],
            ),
        }
        for label, (actual, wanted) in pairs.items():
            require_equal(f"dataset {label}", actual, wanted)


def require_merged_adapter(harbor_src: Path, commit: str) -> None:
    """Require release provenance on the current remote adapters history."""
    git = ("git", "-C", str(harbor_src))
    remote_tip = run(
        *git, "ls-remote", "--exit-code", "origin", "refs/heads/adapters"
    ).split()[0]
    tracking_tip = run(*git, "rev-parse", "refs/remotes/origin/adapters")
    if tracking_tip != remote_tip:
        raise ValueError(
            "Hardened releases require a fresh origin/adapters; "
            "run git fetch origin adapters in the Harbor checkout and retry"
        )
    if (
        subprocess.run(
            [*git, "merge-base", "--is-ancestor", commit, remote_tip], check=False
        ).returncode
        != 0
    ):
        raise ValueError(
            "Hardened releases require Harbor HEAD to be an ancestor of origin/adapters"
        )


def source_contract(harbor_src: Path, image: str) -> dict:
    adapter = harbor_src / "adapters" / "loca-bench"
    if run(
        "git",
        "-C",
        str(harbor_src),
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "adapters/loca-bench",
    ):
        raise ValueError("Release provenance requires a clean Harbor adapter checkout")
    commit = run("git", "-C", str(harbor_src), "rev-parse", "HEAD")
    spec = importlib.util.spec_from_file_location(
        "release_contract", adapter / "src/loca_bench/contract.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load the source runtime contract")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    lock = json.loads((adapter / "src/loca_bench/upstream.lock.json").read_text())
    patch_bytes = (adapter / "patch-manifest.json").read_bytes()
    patches = json.loads(patch_bytes)
    for patch in patches["patches"]:
        require_equal(
            patch["file"],
            hashlib.sha256((adapter / patch["file"]).read_bytes()).hexdigest(),
            patch["sha256"],
        )
    patchset = hashlib.sha256(
        "".join(patch["sha256"] for patch in patches["patches"]).encode()
    ).hexdigest()
    require_equal("source patchset", patchset, patches["patchset_sha256"])
    expected = {
        "adapter_commit": commit,
        "runtime_image": image,
        "runtime_contract_version": contract.RUNTIME_CONTRACT_VERSION,
        "result_schema_version": contract.RESULT_SCHEMA_VERSION,
        "harness_profile": contract.HARNESS_PROFILE,
        "agent_name": contract.AGENT_NAME,
        "agent_version": contract.agent_version(lock["commit"]),
        "upstream_repository": lock["repository"],
        "upstream_commit": lock["commit"],
        "task_count": contract.EXPECTED_TASK_COUNT,
        "patch_manifest_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "patchset_sha256": patchset,
    }
    if contract.RUNTIME_CONTRACT_VERSION >= 2:
        expected["isolation_policy"] = contract.ISOLATION_POLICY
    return expected


IMAGE_PROBE = """
import hashlib, json, subprocess
from pathlib import Path
from loca_bench.task_index import load_task_index, task_index_sha256
root = Path('/opt/loca-harbor')
print(json.dumps({
    'contract': json.loads((root / 'runtime-contract.json').read_text()),
    'upstream_commit': subprocess.check_output(['git', '-C', '/opt/loca-upstream', 'rev-parse', 'HEAD'], text=True).strip(),
    'task_index_sha256': task_index_sha256(load_task_index(root / 'task-index.json')),
    'patch_manifest_sha256': hashlib.sha256((root / 'patch-manifest.json').read_bytes()).hexdigest(),
}))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-src", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--dataset-dir", type=Path)
    parser.add_argument("--loca-src", type=Path)
    parser.add_argument("--require-hardened", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}", args.runtime_image
    ):
        raise ValueError("A release image must use an immutable sha256 digest")
    expected = source_contract(args.harbor_src.resolve(), args.runtime_image)
    if args.require_hardened:
        require_merged_adapter(args.harbor_src.resolve(), expected["adapter_commit"])
    if args.loca_src is not None:
        require_equal(
            "generation upstream checkout",
            run("git", "-C", str(args.loca_src), "rev-parse", "HEAD"),
            expected["upstream_commit"],
        )
    manifest = None
    if args.dataset_dir is not None:
        manifests = list((args.dataset_dir / "manifests").glob("loca-bench-*.json"))
        if len(manifests) != 1:
            raise ValueError("Expected exactly one release manifest")
        manifest = json.loads(manifests[0].read_text())
    subprocess.run(["docker", "pull", args.runtime_image], check=True)
    labels = json.loads(
        run(
            "docker",
            "image",
            "inspect",
            args.runtime_image,
            "--format",
            "{{json .Config.Labels}}",
        )
    )
    evidence = json.loads(
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            args.runtime_image,
            "-c",
            IMAGE_PROBE,
        )
    )
    evidence["adapter_commit"] = labels.get("org.opencontainers.image.revision")
    check_evidence(
        expected, evidence, manifest=manifest, require_hardened=args.require_hardened
    )
    print(
        f"Verified runtime contract {expected['runtime_contract_version']} at Harbor {expected['adapter_commit']}"
    )


if __name__ == "__main__":
    main()
