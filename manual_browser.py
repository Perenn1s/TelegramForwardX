"""Open a dedicated ordinary browser with a persistent MemeBot profile."""

import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import urlsplit

import requests


def manual_profile(base_dir, channel):
    return Path(base_dir) / "state" / f"x-browser-manual-{channel}"


def endpoint_file(base_dir, channel):
    return Path(base_dir) / "state" / f"x-browser-endpoint-{channel}.json"


def validate_endpoint(endpoint, port):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", parsed.path)):
        raise ValueError("Invalid local browser endpoint; run MemeBot.exe --open-browser again.")
    return endpoint


def saved_endpoint(base_dir, settings):
    path = endpoint_file(base_dir, settings.channel)
    if not path.exists():
        raise ValueError("First run MemeBot.exe --open-browser and sign in to X in that window.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_endpoint(data["endpoint"], settings.debug_port)


def browser_executable(channel):
    if channel not in ("msedge", "chrome"):
        raise ValueError("Manual browser mode supports installed Edge or Chrome.")
    relative = ("Microsoft/Edge/Application/msedge.exe" if channel == "msedge"
                else "Google/Chrome/Application/chrome.exe")
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root and (path := Path(root) / relative).is_file():
            return path
    raise ValueError(f"Cannot find installed {channel}. Install Edge/Chrome or select the other browser.")


def current_endpoint(port):
    # This is loopback browser control, never an X developer API request.
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        response.raise_for_status()
        return validate_endpoint(response.json()["webSocketDebuggerUrl"], port)


def open_browser(base_dir, settings):
    """No Playwright connection or login automation occurs in this command."""
    if settings.mode != "manual":
        raise ValueError("Set Browser.mode=manual before using --open-browser.")
    marker = endpoint_file(base_dir, settings.channel)
    port = settings.debug_port
    with socket.socket() as sock:
        sock.settimeout(0.5)
        in_use = sock.connect_ex(("127.0.0.1", port)) == 0
    if in_use:
        try:
            if current_endpoint(port) == saved_endpoint(base_dir, settings):
                logging.info("MemeBot dedicated browser is already open.")
                return
        except (ValueError, KeyError, requests.RequestException):
            pass
        raise ValueError("Browser debug port is occupied by another session. Close it or change Browser.debug_port.")
    profile = manual_profile(base_dir, settings.channel)
    profile.mkdir(parents=True, exist_ok=True)
    executable = browser_executable(settings.channel)
    subprocess.Popen([str(executable), f"--user-data-dir={profile.resolve()}",
                      "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
                      "--new-window", "https://x.com/home"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            endpoint = current_endpoint(port)
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(json.dumps({"endpoint": endpoint}), encoding="utf-8")
            temporary.replace(marker)
            logging.info("Ordinary %s opened with the MemeBot profile. Log in manually if needed; keep the window open until login is verified.",
                         settings.channel)
            return
        except (ValueError, KeyError, requests.RequestException):
            time.sleep(0.25)
    raise ValueError("Browser did not enable its local connection. Close the dedicated window and retry --open-browser.")
