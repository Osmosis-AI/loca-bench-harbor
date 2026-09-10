#!/usr/bin/env python3
"""Verify a release image against its clean, pinned Harbor source checkout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

HARBOR_REPOSITORY = "https://github.com/Osmosis-AI/harbor.git"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")


def upstream_tree(root: Path) -> dict:
    """Fingerprint actual filesystem entries, including ignored and untracked code."""

    def fail(error):
        raise error

    result = {}
    for directory, directories, files in os.walk(root, followlinks=False, onerror=fail):
        if Path(directory) == root:
            directories[:] = [name for name in directories if name != ".git"]
            files = [name for name in files if name != ".git"]
        for name in directories + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            executable = mode & 0o111
            if stat.S_ISLNK(mode):
                value = ["symlink", os.readlink(path)]
            elif stat.S_ISDIR(mode):
                continue
            elif stat.S_ISREG(mode):
                value = [
                    "file",
                    executable,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ]
            else:
                raise ValueError(f"Unsupported upstream entry: {path}")
            result[path.relative_to(root).as_posix()] = value
    return result


def require_upstream_tree(actual: object, expected: dict) -> None:
    if not isinstance(actual, dict):
        raise ValueError("Image upstream tree evidence is missing")
    changed = sorted(
        path
        for path in actual.keys() | expected.keys()
        if actual.get(path) != expected.get(path)
    )
    if changed:
        raise ValueError(
            f"Image upstream tree differs at {len(changed)} paths: {', '.join(changed[:10])}"
        )


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
        if key not in {"adapter_commit", "runtime_image", "upstream_tree"}:
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
    if expected["runtime_contract_version"] >= 2:
        require_upstream_tree(evidence.get("upstream_tree"), expected["upstream_tree"])
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
    """Require release provenance on the official Osmosis-AI adapters history."""
    git = ("git", "-C", str(harbor_src))
    remote_tip = run(
        *git, "ls-remote", "--exit-code", HARBOR_REPOSITORY, "refs/heads/adapters"
    ).split()[0]
    tracking_tip = run(*git, "rev-parse", "refs/remotes/origin/adapters")
    if tracking_tip != remote_tip:
        raise ValueError(
            "Hardened releases require a fresh origin/adapters from official Osmosis-AI/harbor; "
            f"verify origin points to {HARBOR_REPOSITORY}, then "
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


def require_upstream_source(loca_src: Path, commit: str) -> None:
    """Require generation to use a clean checkout at the pinned upstream commit."""
    git = ("git", "-C", str(loca_src))
    require_equal(
        "generation upstream checkout", run(*git, "rev-parse", "HEAD"), commit
    )
    if run(*git, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Release provenance requires a clean LOCA upstream checkout")


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


def expected_upstream_tree(
    harbor_src: Path, expected: dict, loca_src: Path | None
) -> dict:
    """Export pinned Git objects and apply audited patches without editing the source."""
    with tempfile.TemporaryDirectory(prefix="loca-release-tree-") as temporary:
        root = Path(temporary)
        source = loca_src
        if source is None:
            source = root / "objects"
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    expected["upstream_repository"],
                    expected["upstream_commit"],
                ],
                check=True,
            )
        archive = root / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(
                [
                    "git",
                    "--no-replace-objects",
                    "-C",
                    str(source),
                    "archive",
                    "--format=tar",
                    expected["upstream_commit"],
                ],
                stdout=output,
                check=True,
            )
        tree = root / "tree"
        tree.mkdir()
        with tarfile.open(archive) as contents:
            contents.extractall(tree, filter="data")
        adapter = harbor_src / "adapters/loca-bench"
        manifest = (adapter / "patch-manifest.json").read_bytes()
        require_equal(
            "source patch manifest",
            hashlib.sha256(manifest).hexdigest(),
            expected["patch_manifest_sha256"],
        )
        for patch in json.loads(manifest)["patches"]:
            path = adapter / patch["file"]
            require_equal(
                patch["file"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                patch["sha256"],
            )
            subprocess.run(["git", "-C", str(tree), "apply", str(path)], check=True)
        return upstream_tree(tree)


IMAGE_PROBE = (
    """
import hashlib, json, os, stat, subprocess
from pathlib import Path
"""
    + inspect.getsource(upstream_tree)
    + """
from loca_bench.task_index import load_task_index, task_index_sha256
root = Path('/opt/loca-harbor')
print(json.dumps({
    'contract': json.loads((root / 'runtime-contract.json').read_text()),
    'upstream_commit': subprocess.check_output(['git', '-C', '/opt/loca-upstream', 'rev-parse', 'HEAD'], text=True).strip(),
    'task_index_sha256': task_index_sha256(load_task_index(root / 'task-index.json')),
    'patch_manifest_sha256': hashlib.sha256((root / 'patch-manifest.json').read_bytes()).hexdigest(),
    'upstream_tree': upstream_tree(Path('/opt/loca-upstream')) if json.loads((root / 'runtime-contract.json').read_text())['runtime_contract_version'] >= 2 else None,
}))
"""
)


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
        require_upstream_source(args.loca_src.resolve(), expected["upstream_commit"])
    if expected["runtime_contract_version"] >= 2:
        expected["upstream_tree"] = expected_upstream_tree(
            args.harbor_src.resolve(),
            expected,
            args.loca_src.resolve() if args.loca_src else None,
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
            "-B",
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
