"""Offline regressions for the release provenance gate."""

import copy
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from verify_runtime import check_evidence, main, require_merged_adapter


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
        }
        contract = {
            key: value
            for key, value in self.expected.items()
            if key not in {"adapter_commit", "runtime_image"}
        }
        contract["task_index_sha256"] = "f" * 64
        self.evidence: dict[str, Any] = {
            "contract": contract,
            "adapter_commit": self.expected["adapter_commit"],
            "upstream_commit": self.expected["upstream_commit"],
            "task_index_sha256": "f" * 64,
            "patch_manifest_sha256": self.expected["patch_manifest_sha256"],
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

    def test_missing_remote_branch_and_unavailable_origin_fail_closed(self):
        self.git(self.origin, "branch", "-m", "gone")
        with self.assertRaises(subprocess.CalledProcessError):
            require_merged_adapter(self.checkout, self.base)
        self.git(
            self.checkout, "remote", "set-url", "origin", str(self.root / "missing")
        )
        with self.assertRaises(subprocess.CalledProcessError):
            require_merged_adapter(self.checkout, self.base)

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


if __name__ == "__main__":
    unittest.main()
