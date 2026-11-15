"""rustic-scheduler."""

from importlib.metadata import PackageNotFoundError, version

try:
    #: Derived from the Git tag when the package is built (see pyproject.toml).
    __version__ = version("rustic-scheduler")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0.dev0"

#: Rustic release this version was verified against.
SUPPORTED_RUSTIC_VERSION = "0.11.4"
