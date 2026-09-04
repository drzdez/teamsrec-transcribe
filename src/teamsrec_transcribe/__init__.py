"""teamsrec-transcribe: post-processing for teamsrec meeting recordings."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("teamsrec-transcribe")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

APP_NAME = "teamsrec-transcribe"
FORMAT_VERSION = 1
