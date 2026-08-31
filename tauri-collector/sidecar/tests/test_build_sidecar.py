"""Pure regression tests for the sidecar dependency-closure guard."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from importlib import metadata
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from packaging.utils import canonicalize_name


BUILD_SIDECAR = Path(__file__).resolve().parents[1] / "build_sidecar.py"
SPEC = importlib.util.spec_from_file_location("sidecar_build_sidecar", BUILD_SIDECAR)
assert SPEC is not None and SPEC.loader is not None
sidecar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sidecar
SPEC.loader.exec_module(sidecar)


class FakeDistribution:
    def __init__(
        self,
        name: str,
        version: str,
        requires: list[str] | None = None,
        direct_url: str | None = None,
    ) -> None:
        self.direct_url = direct_url
        self.metadata = {"Name": name}
        self.requires = requires
        self.version = version

    def read_text(self, filename: str) -> str | None:
        return self.direct_url if filename == "direct_url.json" else None


def distribution_resolver(
    distributions: list[FakeDistribution],
):
    by_name = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in distributions
    }

    def resolve(name: str) -> FakeDistribution:
        try:
            return by_name[canonicalize_name(name)]
        except KeyError as error:
            raise metadata.PackageNotFoundError(name) from error

    return resolve


class DependencyClosureTests(unittest.TestCase):
    def test_complete_recursive_closure_has_no_issues(self) -> None:
        resolver = distribution_resolver(
            [
                FakeDistribution(
                    "memento-brain-collector",
                    "1.0",
                    ["memento-brain-memory>=1.0", "httpx>=0.27"],
                ),
                FakeDistribution("memento-brain-memory", "1.1", ["boto3>=1.35"]),
                FakeDistribution("httpx", "0.28"),
                FakeDistribution("boto3", "1.35"),
            ]
        )

        issues = sidecar.dependency_closure_issues(
            ["memento-brain-collector", "memento-brain-memory"],
            distribution_for=resolver,
        )

        self.assertEqual(issues, [])

    def test_missing_transitive_dependency_is_reported_with_origin(self) -> None:
        resolver = distribution_resolver(
            [
                FakeDistribution("memento-brain-collector", "1.0", ["memory>=1.0"]),
                FakeDistribution("memory", "1.0", ["boto3>=1.35"]),
            ]
        )

        issues = sidecar.dependency_closure_issues(
            ["memento-brain-collector"], distribution_for=resolver
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].package, "boto3")
        self.assertIn("boto3>=1.35 is not installed", issues[0].message)
        self.assertEqual(issues[0].required_by, "memory")

    def test_incompatible_installed_version_is_reported(self) -> None:
        resolver = distribution_resolver(
            [
                FakeDistribution("memento-brain-collector", "1.0", ["boto3>=1.35"]),
                FakeDistribution("boto3", "1.34"),
            ]
        )

        issues = sidecar.dependency_closure_issues(
            ["memento-brain-collector"], distribution_for=resolver
        )

        self.assertEqual(len(issues), 1)
        self.assertIn("boto3 1.34 does not satisfy boto3>=1.35", issues[0].message)

    def test_markers_skip_inactive_requirements_and_extras_activate_them(self) -> None:
        resolver = distribution_resolver(
            [
                FakeDistribution(
                    "root",
                    "1.0",
                    [
                        "windows-only>=1; sys_platform == 'win32'",
                        "sqlalchemy[asyncio]>=2.0",
                    ],
                ),
                FakeDistribution(
                    "sqlalchemy",
                    "2.0",
                    [
                        "greenlet>=3; extra == 'asyncio'",
                        "postgres-only>=1; extra == 'postgresql'",
                    ],
                ),
            ]
        )

        issues = sidecar.dependency_closure_issues(
            ["root"],
            distribution_for=resolver,
            environment={"sys_platform": "linux"},
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].package, "greenlet")
        self.assertNotIn("windows-only", issues[0].message)
        self.assertNotIn("postgres-only", issues[0].message)

    def test_local_root_distributions_must_match_the_worktree(self) -> None:
        roots = dict(sidecar.SIDECAR_ROOT_DISTRIBUTIONS)
        matching_resolver = distribution_resolver(
            [
                FakeDistribution(
                    package,
                    "1.0",
                    direct_url=json.dumps({"url": source.as_uri()}),
                )
                for package, source in roots.items()
            ]
        )

        self.assertEqual(
            sidecar._local_root_distribution_issues(
                distribution_for=matching_resolver
            ),
            [],
        )

        collector_path = roots["memento-brain-collector"]
        mismatch_resolver = distribution_resolver(
            [
                FakeDistribution(
                    "memento-brain-collector",
                    "1.0",
                    direct_url=json.dumps(
                        {"url": (collector_path.parent / "wrong-collector").as_uri()}
                    ),
                ),
                FakeDistribution(
                    "memento-brain-memory",
                    "1.0",
                    direct_url=json.dumps(
                        {"url": roots["memento-brain-memory"].as_uri()}
                    ),
                ),
            ]
        )

        issues = sidecar._local_root_distribution_issues(
            distribution_for=mismatch_resolver
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].package, "memento-brain-collector")
        self.assertIn("not local source", issues[0].message)

    def test_main_does_not_start_a_build_when_guard_fails(self) -> None:
        error = sidecar.DependencyClosureError(
            [
                sidecar.DependencyIssue(
                    "boto3",
                    "memento-brain-memory",
                    "boto3>=1.35 is not installed (required by memento-brain-memory)",
                )
            ]
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(
                sidecar, "ensure_sidecar_dependency_closure", side_effect=error
            ),
            mock.patch.object(sidecar, "_build_one") as build_one,
            mock.patch.object(sidecar, "_build_onedir") as build_onedir,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(sidecar.main(), 1)

        build_one.assert_not_called()
        build_onedir.assert_not_called()
        self.assertIn("Sidecar dependency closure is incomplete", stderr.getvalue())

    def test_main_builds_mcp_as_onedir_resource(self) -> None:
        output = io.StringIO()
        collector_binary = Path("collector-sidecar.exe")
        hook_directory = Path("memento-hook-runner")
        mcp_directory = Path("memento-mcp-sidecar")

        with (
            mock.patch.object(sidecar, "ensure_sidecar_dependency_closure"),
            mock.patch.object(sidecar, "host_triple", return_value="test-triple"),
            mock.patch.object(sidecar, "_build_one", return_value=collector_binary) as build_one,
            mock.patch.object(
                sidecar,
                "_build_onedir",
                side_effect=[hook_directory, mcp_directory],
            ) as build_onedir,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(sidecar.main(), 0)

        build_one.assert_called_once_with(
            "collector.spec",
            "memento-collector-sidecar",
            "test-triple",
            ".exe",
        )
        self.assertEqual(
            build_onedir.call_args_list,
            [
                mock.call("hook_runner.spec", "memento-hook-runner"),
                mock.call("mcp.spec", "memento-mcp-sidecar"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
