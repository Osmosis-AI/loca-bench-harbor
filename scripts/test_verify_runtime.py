"""Offline regressions for the release provenance gate."""

import copy
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from verify_runtime import (
    check_evidence,
    expected_upstream_tree,
    main,
    require_merged_adapter,
    require_upstream_source,
    require_upstream_tree,
    upstream_tree,
)


class RuntimeReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "adapter_commit": "a" * 40,
            "runtime_image": "ghcr.io/osmosis-ai/loca-bench-runtime@sha256:" + "b" * 64,
            "runtime_contract_version": 2,
            "harness_profile": "loca-hardened-react-v2",
            "isolation_policy": "landlock-seccomp-v1",
            "agent_name": "loca-official-react",
            "agent_version": "2.0.0+loca.12345678",
            "upstream_commit": "12345678" + "c" * 32,
            "patch_manifest_sha256": "d" * 64,
            "patchset_sha256": "e" * 64,
            "upstream_tree": {"source.py": ["file", 0, "0" * 64]},
        }
        contract = {
            key: value
            for key, value in self.expected.items()
            if key not in {"adapter_commit", "runtime_image", "upstream_tree"}
        }
        contract["task_index_sha256"] = "f" * 64
        self.evidence: dict[str, Any] = {
            "contract": contract,
            "adapter_commit": self.expected["adapter_commit"],
            "upstream_commit": self.expected["upstream_commit"],
            "task_index_sha256": "f" * 64,
            "patch_manifest_sha256": self.expected["patch_manifest_sha256"],
            "upstream_tree": copy.deepcopy(self.expected["upstream_tree"]),
        }
        self.manifest = {
            "adapter": {
                "commit": self.expected["adapter_commit"],
                "contract_version": 2,
                "agent": "loca-official-react@2.0.0+loca.12345678",
            },
            "upstream": {"commit": self.expected["upstream_commit"]},
            "runtime": {
                "image": self.expected["runtime_image"],
                "task_index_sha256": "f" * 64,
            },
            "profile": {"name": "loca-hardened-react-v2"},
        }

    def test_matching_hardened_release(self):
        check_evidence(
            self.expected, self.evidence, manifest=self.manifest, require_hardened=True
        )

    def test_mismatched_image_evidence_is_rejected(self):
        for field in (
            "adapter_commit",
            "upstream_commit",
            "task_index_sha256",
            "patch_manifest_sha256",
            "upstream_tree",
        ):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = "unknown"
                with self.assertRaises(ValueError):
                    check_evidence(self.expected, evidence)
        for field in (
            "runtime_contract_version",
            "harness_profile",
            "isolation_policy",
            "patchset_sha256",
        ):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["contract"][field] = "stale"
                with self.assertRaises(ValueError):
                    check_evidence(self.expected, evidence)

    def test_dataset_cannot_claim_another_image_or_source(self):
        for section, key in (
            ("adapter", "commit"),
            ("runtime", "image"),
            ("profile", "name"),
        ):
            with self.subTest(section=section, key=key):
                manifest = copy.deepcopy(self.manifest)
                manifest[section][key] = "stale"
                with self.assertRaises(ValueError):
                    check_evidence(self.expected, self.evidence, manifest=manifest)

    def test_historical_runtime_can_validate_but_cannot_make_new_release(self):
        self.expected.update(
            runtime_contract_version=1,
            harness_profile="loca-official-react-v1",
            agent_version="1.3.0+loca.12345678",
        )
        self.expected.pop("isolation_policy")
        self.expected.pop("upstream_tree")
        self.evidence.pop("upstream_tree")
        self.evidence["contract"] = {**self.expected, "task_index_sha256": "f" * 64}
        check_evidence(self.expected, self.evidence)
        with self.assertRaisesRegex(ValueError, "release contract"):
            check_evidence(self.expected, self.evidence, require_hardened=True)


class AdapterAncestryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / "origin"
        self.checkout = self.root / "checkout"
        self.git(self.root, "init", "-b", "adapters", str(self.origin))
        self.commit(self.origin)
        self.git(self.root, "clone", str(self.origin), str(self.checkout))
        self.base = self.git(self.checkout, "rev-parse", "HEAD")
        official = patch("verify_runtime.HARBOR_REPOSITORY", str(self.origin))
        official.start()
        self.addCleanup(official.stop)

    @staticmethod
    def git(repo, *args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def commit(self, repo):
        self.git(
            repo,
            "-c",
            "user.name=Release Gate Test",
            "-c",
            "user.email=release-gate@example.invalid",
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "-m",
            "fixture",
        )

    def test_remote_tip_and_ancestor_are_accepted_without_changing_refs(self):
        require_merged_adapter(self.checkout, self.base)
        self.commit(self.origin)
        self.git(self.checkout, "fetch", "origin", "adapters")
        refs = self.git(self.checkout, "show-ref")
        require_merged_adapter(self.checkout, self.base)
        self.assertEqual(self.git(self.checkout, "show-ref"), refs)

    def test_unmerged_commit_is_rejected_even_with_an_identical_tree(self):
        self.git(self.checkout, "checkout", "-b", "review")
        self.commit(self.checkout)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "HEAD^{tree}"),
            self.git(self.checkout, "rev-parse", "origin/adapters^{tree}"),
        )
        with self.assertRaisesRegex(ValueError, "ancestor of origin/adapters"):
            require_merged_adapter(self.checkout, head)

    def test_stale_tracking_ref_cannot_accept_a_commit_removed_from_remote(self):
        self.commit(self.origin)
        self.git(self.checkout, "pull", "--ff-only")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.origin, "reset", "--hard", self.base)
        with self.assertRaisesRegex(ValueError, "fresh origin/adapters"):
            require_merged_adapter(self.checkout, head)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/adapters"), head)

    def test_fork_origin_cannot_authorize_an_unmerged_commit(self):
        fork = self.root / "fork"
        self.git(self.root, "clone", str(self.origin), str(fork))
        self.commit(fork)
        self.git(self.checkout, "remote", "set-url", "origin", str(fork))
        self.git(self.checkout, "pull", "--ff-only")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/adapters"), head)
        self.assertNotEqual(self.git(self.origin, "rev-parse", "HEAD"), head)
        with self.assertRaisesRegex(ValueError, "official Osmosis-AI/harbor"):
            require_merged_adapter(self.checkout, head)

    def test_missing_official_branch_and_unavailable_repository_fail_closed(self):
        self.git(self.origin, "branch", "-m", "gone")
        with self.assertRaises(subprocess.CalledProcessError):
            require_merged_adapter(self.checkout, self.base)
        with (
            patch("verify_runtime.HARBOR_REPOSITORY", str(self.root / "missing")),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            require_merged_adapter(self.checkout, self.base)

    def test_generation_rejects_dirty_upstream_before_probing_the_image(self):
        source = self.checkout / "generator.py"
        source.write_text("original")
        self.git(self.checkout, "add", "generator.py")
        self.commit(self.checkout)
        commit = self.git(self.checkout, "rev-parse", "HEAD")
        require_upstream_source(self.checkout, commit)
        with self.assertRaisesRegex(ValueError, "generation upstream checkout"):
            require_upstream_source(self.checkout, self.base)

        argv = [
            "verify_runtime.py",
            "--harbor-src",
            str(self.checkout),
            "--loca-src",
            str(self.checkout),
            "--runtime-image",
            "example.invalid/runtime@sha256:" + "b" * 64,
        ]
        for state in ("modified", "staged", "untracked"):
            with self.subTest(state=state):
                self.git(
                    self.checkout, "restore", "--staged", "--worktree", "generator.py"
                )
                target = self.checkout / "extra.py" if state == "untracked" else source
                target.write_text("changed")
                if state == "staged":
                    self.git(self.checkout, "add", "generator.py")
                with (
                    patch("sys.argv", argv),
                    patch(
                        "verify_runtime.source_contract",
                        return_value={"upstream_commit": commit},
                    ),
                    self.assertRaisesRegex(ValueError, "clean LOCA upstream checkout"),
                ):
                    main()

    def test_only_hardened_cli_checks_release_ancestry(self):
        image = "example.invalid/runtime@sha256:" + "b" * 64
        for hardened in (False, True):
            with self.subTest(hardened=hardened):
                argv = [
                    "verify_runtime.py",
                    "--harbor-src",
                    str(self.checkout),
                    "--runtime-image",
                    image,
                ]
                if hardened:
                    argv.append("--require-hardened")
                with (
                    patch("sys.argv", argv),
                    patch(
                        "verify_runtime.source_contract",
                        return_value={
                            "adapter_commit": self.base,
                            "runtime_contract_version": 2,
                        },
                    ),
                    patch("verify_runtime.require_merged_adapter") as ancestry,
                    patch("verify_runtime.expected_upstream_tree", return_value={}),
                    patch(
                        "verify_runtime.run",
                        side_effect=[
                            '{"org.opencontainers.image.revision":"commit"}',
                            "{}",
                        ],
                    ),
                    patch("verify_runtime.subprocess.run"),
                    patch("verify_runtime.check_evidence"),
                    redirect_stdout(io.StringIO()),
                ):
                    main()
                if hardened:
                    ancestry.assert_called_once_with(self.checkout.resolve(), self.base)
                else:
                    ancestry.assert_not_called()


class UpstreamTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "--quiet", "-b", "main")
        self.before = "first = 1\n" + "\n" * 8 + "second = 1\n"
        self.after = self.before.replace("= 1", "= 2")
        (self.source / "required.py").write_text(self.before)
        (self.source / "other.py").write_text("safe\n")
        (self.source / ".gitignore").write_text("ignored.py\n__pycache__/\n")
        (self.source / "entry.py").symlink_to("required.py")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Tree Test",
            "-c",
            "user.email=tree@example.invalid",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "fixture",
        )
        self.expected = {
            "upstream_commit": self.git("rev-parse", "HEAD").strip(),
            "upstream_repository": str(self.source),
        }
        (self.source / "required.py").write_text(self.after)
        self.adapter = self.root / "harbor/adapters/loca-bench"
        self.adapter.mkdir(parents=True)
        self.patch = self.adapter / "fix.patch"
        self.patch.write_text(self.git("diff", "--", "required.py"))
        manifest = json.dumps(
            {
                "patches": [
                    {
                        "file": self.patch.name,
                        "sha256": hashlib.sha256(self.patch.read_bytes()).hexdigest(),
                    }
                ]
            }
        )
        (self.adapter / "patch-manifest.json").write_text(manifest)
        self.expected["patch_manifest_sha256"] = hashlib.sha256(
            manifest.encode()
        ).hexdigest()
        self.patched_tree = upstream_tree(self.source)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.source), *args], text=True
        )

    def test_expected_tree_uses_pinned_objects_and_applies_audited_patches(self):
        (self.source / "required.py").write_text("dirty source must not be copied\n")
        original = upstream_tree(self.source)
        for source in (self.source, None):
            with self.subTest(reuse_source=source is not None):
                actual = expected_upstream_tree(
                    self.root / "harbor", self.expected, source
                )
                self.assertEqual(actual, self.patched_tree)
                self.assertEqual(upstream_tree(self.source), original)

    def test_modified_patch_is_rejected(self):
        self.patch.write_text("unreviewed patch")
        with self.assertRaisesRegex(ValueError, "fix.patch"):
            expected_upstream_tree(self.root / "harbor", self.expected, self.source)

    def test_actual_tree_rejects_missing_and_extra_source_or_changed_metadata(self):
        cases = (
            "missing_patch",
            "missing_hunk",
            "other_file",
            "ignored_source",
            "untracked_source",
            "bytecode",
            "symlink",
            "executable",
        )
        for case in cases:
            with self.subTest(case=case):
                actual = self.root / case
                shutil.copytree(
                    self.source,
                    actual,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(".git"),
                )
                if case == "missing_patch":
                    (actual / "required.py").write_text(self.before)
                elif case == "missing_hunk":
                    (actual / "required.py").write_text(
                        self.after.replace("second = 2", "second = 1")
                    )
                elif case == "other_file":
                    (actual / "other.py").write_text("changed\n")
                elif case in {"ignored_source", "untracked_source"}:
                    (
                        actual
                        / ("ignored.py" if case == "ignored_source" else "extra.py")
                    ).write_text("unexpected\n")
                elif case == "bytecode":
                    (actual / "__pycache__").mkdir()
                    (actual / "__pycache__/other.cpython-312.pyc").write_bytes(
                        b"unexpected bytecode"
                    )
                elif case == "symlink":
                    (actual / "entry.py").unlink()
                    (actual / "entry.py").symlink_to("other.py")
                else:
                    (actual / "other.py").chmod(0o755)
                with self.assertRaisesRegex(ValueError, "Image upstream tree differs"):
                    require_upstream_tree(upstream_tree(actual), self.patched_tree)
        (self.source / "__pycache__").mkdir()
        require_upstream_tree(upstream_tree(self.source), self.patched_tree)


if __name__ == "__main__":
    unittest.main()
