"""dank-py package."""

from importlib.metadata import PackageNotFoundError, version as package_version

__all__ = ["__version__"]

try:
    __version__ = package_version("dank-py")
except PackageNotFoundError:
    __version__ = "0.0.0"
