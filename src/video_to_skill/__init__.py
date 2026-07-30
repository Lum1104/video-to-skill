"""Compile time-aligned video evidence into portable Agent Skills."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("video-to-skill")
except PackageNotFoundError:  # pragma: no cover - editable source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
