"""Run the forwarder and restart it after transient failures/process crashes."""

import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from delivery_store import data_directory, instance_lock
from runtime_logging import configure_logging

BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def forwarder_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--non-interactive"]
    executable = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
    return [str(executable), str(BASE_DIR / "forwarder.py"), "--non-interactive"]


def supervise(command, *, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic):
    failures = 0
    while True:
        started = clock()
        child = popen(command, cwd=BASE_DIR, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # In a console the child is hidden, so it does not receive Ctrl+C.
            child.terminate()
            child.wait()
            return 0
        if code in (0, 2):
            logging.info("Forwarder stopped with code %s; automatic restart disabled.", code)
            return 0  # Scheduled Task must not restart configuration/auth failures.
        failures = 1 if clock() - started >= 300 else min(failures + 1, 7)
        delay = min(300, 5 * 2 ** (failures - 1))
        logging.warning("Forwarder exited with code %s; restarting in %ss.", code, delay)
        sleep(delay)


def main():
    state = data_directory(BASE_DIR)
    configure_logging(log_file=str(state / "supervisor.log"))
    # Use python.exe for child exit-code reporting even when launched by pythonw.
    try:
        with instance_lock(state / "supervisor.lock"):
            return supervise(forwarder_command())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logging.error("Supervisor stopped: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
