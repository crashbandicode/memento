"""Canonical collector release version.

This module is part of the frozen sidecar, unlike distribution metadata which
may describe whichever collector happened to be installed in the build Python.
Hatch also reads this value when building the wheel, so packaged and frozen
collectors share one source of truth.
"""

__version__ = "0.0.47"
