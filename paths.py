#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve app root for source run and frozen (PyInstaller) run."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Writable directory next to exe (frozen) or source tree."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Bundled read-only resources (PyInstaller _MEIPASS) or source tree."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent
