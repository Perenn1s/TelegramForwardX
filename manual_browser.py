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

from local_storage import (check_profile_size, endpoint_file, managed_profile, manual_profile,
                           migrate_legacy_storage, path_size, profile_in_use, remove_tree)

# Downloaded models, telemetry, crash dumps and component payloads. Edge
# refetches or rebuilds every one of them on demand, and none of them holds
# cookies, tokens or site storage, so removing them cannot sign the profile out
# of X. "Local State", which holds the cookie encryption key, is never listed.
COMPONENT_DATA = (
    "Ad Blocking", "AutoLaunchProtocolsComponent", "Autofill", "BrowserMetrics",
    "BrowserMetrics-spare.pma",
    "CertificateRevocation", "Crashpad", "Domain Actions", "EADPData Component",
    "Edge Data Protection Lists", "Edge Entity Extraction", "Edge Notifications",
    "Edge Shopping", "Edge Sidebar", "Edge Signal Triggers", "Edge Themes Resources",
    "Edge Wallet", "Edge3pSerp", "EdgeArbitration", "EdgeLanguageDetectionModel",
    "EdgeOnDeviceSpeechModel", "FirstPartySetsPreloaded", "GPUPersistentCache",
    "GrShaderCache", "MEIPreload", "Nurturing", "OnDeviceHeadSuggestModel",
    "OptimizationHints", "OriginTrials", "PKIMetadata", "ProvenanceData",
    "ProvenanceDataAllowList", "ProvenanceDataTensors", "RecoveryImproved",
    "Safe Browsing", "SafetyTips", "ShaderCache", "SmartScreen", "Snapshots",
    "Speech Recognition", "Subresource Filter", "Trust Protection Lists", "TrustTokenKeyCommitments",
    "Typosquatting", "Web Notifications Deny List", "Well Known Domains",
    "WidevineCdm", "WorkspacesNavigationComponent", "ZxcvbnData", "component_crx_cache",
    "extensions_crx_cache", "hyphen-data",
)

# Per-profile page caches. Dropping these only makes the next page load slower.
# They are cleared before every launch, so the closed profile holds no cache.
# Cookies live in "Network", the signed-in session in "Local Storage", "Session
# Storage" and "IndexedDB"; none of those names may ever appear here.
PAGE_CACHES = (
    "Cache", "Code Cache", "Service Worker", "GPUCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "Shared Dictionary",
    "AutofillAiModelCache", "EdgeCoupons", "EntityExtraction", "blob_storage",
    "optimization_guide_hint_cache_store",
)


def prune_profile(profile):
    """Reclaim all rebuildable browser data, never login or site storage.

    Call this only while the dedicated browser is closed. Component payloads
    and every page cache go each time, so the closed profile keeps only the
    X session and a few small bookkeeping files. Only allow-listed names are
    removed, only inside this profile, and links are never followed.
    """
    profile = Path(profile)
    if not profile.is_dir():
        return 0
    if profile_in_use(profile):
        raise ValueError("The dedicated browser profile is still open. Close that browser window first; "
                         "nothing was deleted.")
    freed = sum(remove_tree(profile / name, profile) for name in COMPONENT_DATA)
    freed += sum(remove_tree(child / name, profile)
                 for child in profile.iterdir() if child.is_dir() and not child.is_symlink()
                 for name in PAGE_CACHES)
    return freed


def reclaim_profile_space(profile):
    freed = prune_profile(profile)
    if freed:
        logging.info("Reclaimed %.0f MB of rebuildable browser data from %s. "
                     "The saved X login, cookies and site storage are untouched.",
                     freed / 1024 ** 2, Path(profile).name)
    check_profile_size(profile)
    return freed


def profile_directory(settings):
    return manual_profile(settings.channel) if settings.mode == "manual" else managed_profile()


def prune_browser(base_dir, settings):
    """--prune-browser: reclaim now instead of at the next browser launch."""
    if port_in_use(settings.debug_port):
        raise ValueError("Close the dedicated MemeBot browser window before pruning its profile.")
    migrate_legacy_storage(base_dir, prune=prune_profile)
    profile = profile_directory(settings)
    if not profile.is_dir():
        raise ValueError("No dedicated browser profile yet; nothing to prune.")
    logging.info("Pruning %s.", profile.name)
    reclaim_profile_space(profile)
    logging.info("Profile now uses %.0f MB; the X session was kept.", path_size(profile) / 1024 ** 2)


def validate_endpoint(endpoint, port):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", parsed.path)):
        raise ValueError("Invalid local browser endpoint; run MemeBot.exe --open-browser again.")
    return endpoint


def saved_endpoint(settings):
    path = endpoint_file(settings.channel)
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


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# A size of 0 would mean "browser default", so 1 byte is what turns the HTTP and
# media caches off. Service Worker and code caches have no such switch; they
# live only while the window is open and are pruned before the next launch.
ZERO_CACHE_ARGS = ("--disk-cache-size=1", "--media-cache-size=1")


def launch_command(executable, profile, settings):
    """A normal visible browser that stops hoarding data it never needs here.

    This profile only ever visits x.com, so component downloads, metrics,
    crash dumps and page caches are pure growth. Signing in is ordinary
    foreground traffic and is unaffected by these switches, which also match
    managed mode's defaults.
    """
    return [str(executable), f"--user-data-dir={profile.resolve()}",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={settings.debug_port}",
            "--disable-background-networking", "--disable-component-update",
            "--disable-breakpad", "--disable-crash-reporter", "--disable-sync",
            "--no-first-run", "--no-default-browser-check",
            *ZERO_CACHE_ARGS, "--new-window", "https://x.com/home"]


def open_browser(base_dir, settings):
    """No Playwright connection or login automation occurs in this command."""
    if settings.mode != "manual":
        raise ValueError("Set Browser.mode=manual before using --open-browser.")
    marker = endpoint_file(settings.channel)
    port = settings.debug_port
    if port_in_use(port):
        try:
            if current_endpoint(port) == saved_endpoint(settings):
                logging.info("MemeBot dedicated browser is already open.")
                return
        except (ValueError, KeyError, requests.RequestException):
            pass
        raise ValueError("Browser debug port is occupied by another session. Close it or change Browser.debug_port.")
    # The closed debug port shows our browser is not attached; prune_profile
    # also refuses a profile held by a browser started without that port.
    migrate_legacy_storage(base_dir, prune=prune_profile)
    profile = manual_profile(settings.channel)
    profile.mkdir(parents=True, exist_ok=True)
    executable = browser_executable(settings.channel)
    reclaim_profile_space(profile)
    subprocess.Popen(launch_command(executable, profile, settings),
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
