from __future__ import annotations

from collector.config import _runtime_platform


def test_runtime_platform_distinguishes_wsl2_from_linux() -> None:
    assert _runtime_platform(
        system="Linux",
        release="6.6.87.2-microsoft-standard-WSL2",
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
    ) == "WSL2"


def test_runtime_platform_preserves_native_linux() -> None:
    assert _runtime_platform(
        system="Linux",
        release="6.12.0-generic",
        environ={},
    ) == "Linux"


def test_runtime_platform_keeps_wsl1_distinct() -> None:
    assert _runtime_platform(
        system="Linux",
        release="4.4.0-19041-Microsoft",
        environ={"WSL_INTEROP": "/run/WSL/123_interop"},
    ) == "WSL"
