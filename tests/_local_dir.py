"""Imported first by every test module: tests never touch the real %LOCALAPPDATA%."""

import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="memebot-local-test-")
os.environ["MEMEBOT_LOCAL_DIR"] = ROOT
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
