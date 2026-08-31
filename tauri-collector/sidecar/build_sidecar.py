"""Freeze the collector into a single binary that Tauri can bundle.

Workflow:
  cd tauri-collector/sidecar
  pip install -e ../../collector -e ../../mcp_server pyinstaller
  python build_sidecar.py

Output:
  ../src-tauri/binaries/memento-collector-sidecar-<triple>{.exe?}
  ../src-tauri/binaries/memento-hook-runner/

The `<triple>` suffix is Tauri's requirement — it matches the running
host triple at install time so the right binary lands in the bundle.
We let `rustc -vV` tell us the triple; rustup ships with both rustc and
this script's prerequisite (`cargo tauri`), so the user already has it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from importlib import metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Callable, Iterable, Mapping
from urllib.parse import unquote, urlparse
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# Windows defaults stdout/stderr to cp1252 (a.k.a. "charmap"). The `→` and
# `✓` we print below blow up with UnicodeEncodeError on Windows runners
# (cp1252 has no codepoint for U+2192). Force utf-8 if we can — local
# Windows users hit this the same way the GitHub Actions runner does.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
BIN_DIR = HERE.parent / "src-tauri" / "binaries"
REPO_ROOT = HERE.parents[1]
COLLECTOR_SOURCE = REPO_ROOT / "collector"
MCP_SOURCE = REPO_ROOT / "mcp_server"
SOURCE_PATHS = (COLLECTOR_SOURCE, MCP_SOURCE)

# The sidecars import source from this worktree, but PyInstaller resolves their
# dependency metadata from the active interpreter.  Requiring local project
# installs keeps those two views in sync instead of freezing whatever stale
# wheel happens to be present in a shared virtual environment.
SIDECAR_ROOT_DISTRIBUTIONS = {
    "memento-brain-collector": COLLECTOR_SOURCE,
    "memento-brain-memory": MCP_SOURCE,
}
SIDECAR_ROOT_REQUIREMENTS = tuple(
    Requirement(name) for name in SIDECAR_ROOT_DISTRIBUTIONS
)


@dataclass(frozen=True, order=True)
class DependencyIssue:
    """One deterministic, actionable dependency-closure diagnostic."""

    package: str
    required_by: str
    message: str


class DependencyClosureError(RuntimeError):
    """Raised before PyInstaller can mutate build or binary directories."""

    def __init__(self, issues: Iterable[DependencyIssue]) -> None:
        ordered_issues = tuple(sorted(set(issues)))
        self.issues = ordered_issues
        report = "\n".join(f"- {issue.message}" for issue in ordered_issues)
        super().__init__(
            "Sidecar dependency closure is incomplete or incompatible:\n"
            f"{report}\n"
            "Install the local projects and their dependencies from "
            "tauri-collector/sidecar:\n"
            "  python -m pip install -e ../../collector -e ../../mcp_server pyinstaller"
        )


DistributionResolver = Callable[[str], metadata.Distribution]


def _requirement_is_active(
    requirement: Requirement,
    environment: Mapping[str, str],
    active_extras: set[str],
) -> bool:
    """Return whether a metadata requirement applies to this build.

    ``Requires-Dist`` markers are evaluated once for the base distribution and
    once per requested extra.  This matters for dependencies such as
    ``sqlalchemy[asyncio]`` whose own metadata can contain
    ``extra == 'asyncio'`` requirements.
    """

    if requirement.marker is None:
        return True
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in ("", *sorted(active_extras))
    )


def dependency_closure_issues(
    root_requirements: Iterable[Requirement | str],
    *,
    distribution_for: DistributionResolver = metadata.distribution,
    environment: Mapping[str, str] | None = None,
) -> list[DependencyIssue]:
    """Inspect only a requested distribution closure, never all site packages.

    The injected resolver and environment deliberately make this pure enough
    for focused tests.  A package can be visited again when a newly requested
    extra activates additional requirements.
    """

    marker_environment = dict(default_environment())
    if environment is not None:
        marker_environment.update(environment)

    pending: deque[tuple[Requirement, str]] = deque()
    for root_requirement in root_requirements:
        requirement = (
            root_requirement
            if isinstance(root_requirement, Requirement)
            else Requirement(root_requirement)
        )
        pending.append((requirement, "sidecar build root"))

    issues: set[DependencyIssue] = set()
    requested_extras: dict[str, set[str]] = {}
    expanded_extras: dict[str, frozenset[str]] = {}

    while pending:
        requirement, required_by = pending.popleft()
        package = canonicalize_name(requirement.name)
        try:
            distribution = distribution_for(package)
        except metadata.PackageNotFoundError:
            issues.add(
                DependencyIssue(
                    package,
                    required_by,
                    f"{requirement} is not installed (required by {required_by})",
                )
            )
            continue
        except Exception as error:
            issues.add(
                DependencyIssue(
                    package,
                    required_by,
                    f"{requirement} metadata could not be read "
                    f"(required by {required_by}): {type(error).__name__}: {error}",
                )
            )
            continue

        try:
            installed_version = Version(distribution.version)
        except InvalidVersion:
            issues.add(
                DependencyIssue(
                    package,
                    required_by,
                    f"{package} has invalid installed version {distribution.version!r} "
                    f"(required by {required_by})",
                )
            )
        else:
            if requirement.specifier and installed_version not in requirement.specifier:
                issues.add(
                    DependencyIssue(
                        package,
                        required_by,
                        f"{package} {installed_version} does not satisfy {requirement} "
                        f"(required by {required_by})",
                    )
                )

        extras = requested_extras.setdefault(package, set())
        extras.update(requirement.extras)
        active_extras = frozenset(extras)
        if expanded_extras.get(package) == active_extras:
            continue
        expanded_extras[package] = active_extras

        distribution_name = distribution.metadata.get("Name", package)
        try:
            declared_requirements = tuple(distribution.requires or ())
        except Exception as error:
            issues.add(
                DependencyIssue(
                    package,
                    required_by,
                    f"{distribution_name} dependency metadata could not be read: "
                    f"{type(error).__name__}: {error}",
                )
            )
            continue

        for declared_requirement in sorted(declared_requirements):
            try:
                dependency = Requirement(declared_requirement)
            except InvalidRequirement as error:
                issues.add(
                    DependencyIssue(
                        package,
                        distribution_name,
                        f"{distribution_name} declares invalid requirement "
                        f"{declared_requirement!r}: {error}",
                    )
                )
                continue
            if _requirement_is_active(dependency, marker_environment, extras):
                pending.append((dependency, distribution_name))

    return sorted(issues)


def _file_url_to_path(url: str) -> Path | None:
    """Decode PEP 610's local project URL on both Windows and POSIX."""

    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _local_root_distribution_issues(
    *, distribution_for: DistributionResolver = metadata.distribution
) -> list[DependencyIssue]:
    """Require PEP 610 metadata that points each root at this worktree."""

    issues: list[DependencyIssue] = []
    for package, expected_source in SIDECAR_ROOT_DISTRIBUTIONS.items():
        try:
            distribution = distribution_for(package)
        except metadata.PackageNotFoundError:
            # dependency_closure_issues already supplies the clearer missing
            # distribution report for this case.
            continue
        try:
            direct_url = distribution.read_text("direct_url.json")
            direct_url_data = json.loads(direct_url) if direct_url else None
            source = _file_url_to_path(direct_url_data["url"])
        except (KeyError, TypeError, ValueError) as error:
            source = None
            metadata_error = f" ({type(error).__name__}: {error})"
        else:
            metadata_error = ""

        if source is None:
            issues.append(
                DependencyIssue(
                    package,
                    "sidecar build root",
                    f"{package} is not installed from local source "
                    f"{expected_source}{metadata_error}; direct_url.json must point there",
                )
            )
        elif source.resolve() != expected_source.resolve():
            issues.append(
                DependencyIssue(
                    package,
                    "sidecar build root",
                    f"{package} is installed from {source.resolve()}, not local source "
                    f"{expected_source.resolve()}",
                )
            )
    return issues


def ensure_sidecar_dependency_closure() -> None:
    """Fail before any PyInstaller build-directory or binary mutation occurs."""

    issues = dependency_closure_issues(SIDECAR_ROOT_REQUIREMENTS)
    issues.extend(_local_root_distribution_issues())
    if issues:
        raise DependencyClosureError(issues)


def host_triple() -> str:
    """Ask rustc for the host triple — single source of truth."""
    try:
        out = subprocess.check_output(["rustc", "-vV"], text=True)
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except FileNotFoundError:
        pass
    # Last-resort heuristic if rustc isn't installed yet — gets the
    # common cases right but you really should install rustc, since
    # the Tauri build itself needs it.
    arch = platform.machine().lower()
    if arch in ("amd64", "x86_64"):
        arch_t = "x86_64"
    elif arch in ("arm64", "aarch64"):
        arch_t = "aarch64"
    else:
        arch_t = arch
    system = platform.system()
    if system == "Windows":
        return f"{arch_t}-pc-windows-msvc"
    if system == "Darwin":
        return f"{arch_t}-apple-darwin"
    return f"{arch_t}-unknown-linux-gnu"


def _build_one(spec_name: str, exe_name: str, triple: str, exe_suffix: str) -> Path:
    """Run PyInstaller for a single .spec, move the binary into BIN_DIR
    under Tauri's per-triple naming convention. Returns the final path."""
    spec = HERE / spec_name
    work = HERE / "build"
    dist = HERE / "dist"
    for d in (work, dist):
        if d.exists():
            shutil.rmtree(d)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--workpath", str(work),
        "--distpath", str(dist),
        str(spec),
    ]
    print("->", " ".join(cmd))
    env = dict(os.environ)
    # Never let an ambient editable install or PYTHONPATH from another Memento
    # worktree decide which code gets frozen. The spec files pin the same roots
    # for hook discovery and Analysis.
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in SOURCE_PATHS)
    subprocess.run(cmd, check=True, env=env)

    src = dist / f"{exe_name}{exe_suffix}"
    if not src.exists():
        raise RuntimeError(f"PyInstaller did not produce {src}")
    target = BIN_DIR / f"{exe_name}-{triple}{exe_suffix}"
    if target.exists():
        target.unlink()
    shutil.move(str(src), str(target))
    if exe_suffix == "":
        target.chmod(0o755)

    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dist, ignore_errors=True)
    return target


def _build_onedir(spec_name: str, directory_name: str) -> Path:
    """Build a PyInstaller onedir artifact and place its whole directory.

    Onedir applications are bundled as Tauri resources rather than external
    binaries. Keep the directory intact so its executable and `_internal` DLL
    tree remain adjacent at invocation time.
    """

    spec = HERE / spec_name
    work = HERE / "build"
    dist = HERE / "dist"
    for directory in (work, dist):
        if directory.exists():
            shutil.rmtree(directory)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--workpath", str(work),
        "--distpath", str(dist),
        str(spec),
    ]
    print("->", " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in SOURCE_PATHS)
    subprocess.run(cmd, check=True, env=env)

    source = dist / directory_name
    if not source.is_dir():
        raise RuntimeError(f"PyInstaller did not produce {source}")
    target = BIN_DIR / directory_name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))

    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dist, ignore_errors=True)
    return target


def main() -> int:
    try:
        ensure_sidecar_dependency_closure()
    except DependencyClosureError as error:
        print(error, file=sys.stderr)
        return 1

    triple = host_triple()
    print(f"Building sidecars for triple: {triple}")

    # Sanity: PyInstaller installed?
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Run: pip install pyinstaller", file=sys.stderr)
        return 1
    for source_path in reversed(SOURCE_PATHS):
        sys.path.insert(0, str(source_path))

    # Sanity: both runtime entry points importable?  Importing only the package
    # root is insufficient: collector.__init__ intentionally has no runtime
    # dependencies, so PyInstaller could otherwise emit a binary that crashes
    # immediately when collector.main imports an omitted direct dependency.
    try:
        import collector
        import collector.main  # noqa: F401
    except ImportError:
        print(
            "collector runtime not importable. Run: pip install -e ../../collector",
            file=sys.stderr,
        )
        return 1
    collector_path = Path(collector.__file__).resolve()
    if not collector_path.is_relative_to(COLLECTOR_SOURCE.resolve()):
        print(
            f"collector resolved outside this worktree: {collector_path}",
            file=sys.stderr,
        )
        return 1
    try:
        import mcp_server
    except ImportError:
        print("mcp_server not importable. Run: pip install -e ../../mcp_server", file=sys.stderr)
        return 1
    mcp_path = Path(mcp_server.__file__).resolve()
    if not mcp_path.is_relative_to(MCP_SOURCE.resolve()):
        print(
            f"mcp_server resolved outside this worktree: {mcp_path}",
            file=sys.stderr,
        )
        return 1

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    exe_suffix = ".exe" if platform.system() == "Windows" else ""

    # Build collector first (faster, easier to debug if anything fails).
    collector_path = _build_one(
        "collector.spec", "memento-collector-sidecar", triple, exe_suffix
    )
    print(f"\nv Collector sidecar -> {collector_path}")

    hook_runner_path = _build_onedir("hook_runner.spec", "memento-hook-runner")
    print(f"v Hook runner       -> {hook_runner_path}")

    # Then MCP server — larger dep tree (mcp SDK + openai + asyncpg + ...).
    mcp_path = _build_onedir("mcp.spec", "memento-mcp-sidecar")
    print(f"v MCP sidecar       -> {mcp_path}")

    print("\nNow run `cargo tauri build` from tauri-collector/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
