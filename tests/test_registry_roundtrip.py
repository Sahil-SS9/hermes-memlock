"""Tests for MemLock — registry round-trip verification."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PDIR))
import store as store_mod
