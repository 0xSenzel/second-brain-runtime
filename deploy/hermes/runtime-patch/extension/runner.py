"""Resume durable video jobs and prune decided or aged owned artifacts."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time
import urllib.request

from inbox import Inbox, atomic_json, home_path, load_workflow, verify_workflow
from retention import prune_owned


class CaptureResumeBackend:
    """Reconnect interrupted capture jobs without retrying access blocks or cancellations."""
    def __init__(self, backend, client=None):
        self.backend, self.client = backend, client

    def capture_state(self, item):
        if not item.get("capture_job_id"):
            return None
        if self.client is None:
            from capture_remote import CaptureClient
            self.client = CaptureClient(os.environ["VIDEO_CAPTURE_URL"])
        return self.client.get(item["capture_job_id"])

    @staticmethod
    def needs_recapture(state):
        return state and (state.get("status") == "expired" or
                          state.get("status") == "failed" and
                          state.get("error") == "capture interrupted by service restart")

    def capture(self, item, save):
        if self.needs_recapture(self.capture_state(item)):
            attempts = int(item.get("restart_recaptures", 0))
            if attempts >= 3:
                raise RuntimeError("Capture restart recovery limit reached; inspect the saved job before retrying")
            save(previous_capture_job_ids=item.get("previous_capture_job_ids", []) + [item["capture_job_id"]],
                 capture_job_id=None, restart_recaptures=attempts + 1)
        try:
            return self.backend.capture(item, save)
        except RuntimeError:
            if self.needs_recapture(self.capture_state(item)):
                raise TimeoutError("Capture service restarted; reconnect using the existing work item") from None
            raise

    def process(self, item):
        return self.backend.process(item)


def capture_ready():
    base = os.environ.get("VIDEO_CAPTURE_URL", "").rstrip("/")
    if not base:
        return False
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            return json.load(response).get("status") == "ready"
    except (OSError, ValueError):
        return False


def run(home: Path, vault: Path, *, once=False, dry_run=False):
    store = Inbox(home)
    module, workflow = load_workflow(home)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with (store.root / "runner.lock").open("a") as instance:
        try:
            fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("durable video worker is already running") from None
        daemon = None
        next_prune = 0.0
        while not stop.is_set():
            try:
                verify_workflow(home)
                atomic_json(store.root / "heartbeat.json", {"at": time.time(), "pid": os.getpid(),
                            "owns_workflow_worker": daemon is not None})
                if not dry_run:
                    store.submit_pending(workflow)
                if daemon is None:
                    daemon = (workflow.root / "daemon.lock").open("a")
                    try:
                        fcntl.flock(daemon, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        daemon.close()
                        daemon = None
                if daemon is not None and not dry_run and capture_ready():
                    workflow.run_pending(CaptureResumeBackend(module.Runtime()), limit=2)
                if not dry_run:
                    store.inventory(workflow)
                request = store.root / "maintenance-request.json"
                if dry_run or time.monotonic() >= next_prune or request.exists():
                    report = prune_owned(home, vault, store.jobs(), retention_seconds=7 * 86400, dry_run=dry_run)
                    atomic_json(store.root / "last-prune.json", {"at": time.time(), **report})
                    request.unlink(missing_ok=True)
                    next_prune = time.monotonic() + 3600
                atomic_json(store.root / "heartbeat.json", {"at": time.time(), "pid": os.getpid(),
                            "owns_workflow_worker": daemon is not None, "requests": len(store.requests()),
                            "jobs": len(store.jobs())})
            except Exception as exc:
                atomic_json(store.root / "worker-error.json", {"at": time.time(), "error_type": type(exc).__name__})
                print(f"durable-video worker cycle failed: {type(exc).__name__}", flush=True)
            if once:
                break
            stop.wait(5)
        if daemon is not None:
            daemon.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--vault", type=Path, default=Path("/vault"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.home or home_path(), args.vault, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
