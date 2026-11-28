"""Application data and resource paths."""

import os
import sys
from pathlib import Path


APP_DIR = getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parents[1]))
ASSET_DIR = Path(__file__).resolve().parent / "assets"


def app_path(filename):
    return os.path.join(APP_DIR, filename)


def log_message(message):
    try:
        print(message)
    except UnicodeEncodeError:
        print(str(message).encode("ascii", "replace").decode("ascii"))
