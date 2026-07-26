"""Native-host entrypoint for the shared embedding HTTP server.

`python -m server.services.embedding_server` and imports under this module
name all resolve to `embedding/embedding_server.py`, so Docker and native
stay on one implementation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPOSITORY_SHARED = (
    Path(__file__).resolve().parents[3] / "embedding" / "embedding_server.py"
)
_BUNDLED_SHARED = Path(__file__).with_name("_embedding_server_impl.py")
_SHARED = (
    _REPOSITORY_SHARED
    if _REPOSITORY_SHARED.is_file()
    else _BUNDLED_SHARED
)
_CANONICAL = "server.services.embedding_server"

if not _SHARED.is_file():
    raise ImportError(
        "Shared embedding server not found in the monorepo or installed "
        f"package (checked {_REPOSITORY_SHARED} and {_BUNDLED_SHARED})."
    )

_SPEC = importlib.util.spec_from_file_location(_CANONICAL, _SHARED)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load shared embedding server from {_SHARED}")

_IMPL = importlib.util.module_from_spec(_SPEC)
# Register under the package name (and under __main__ when launched via -m)
# before exec so the shared file never sees __name__ == "__main__".
sys.modules[_CANONICAL] = _IMPL
sys.modules[__name__] = _IMPL
_SPEC.loader.exec_module(_IMPL)

if __name__ == "__main__":
    _IMPL.main()
