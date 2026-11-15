"""Conservative pruning of unchanged, explicitly owned video job artifacts.

Schema 4: a job is decided when the vault holds a note for its video outside Inbox/
(Notes/ approved, or Archive/Inbox/ discarded) and none inside Inbox/.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
import tempfile
import time

RESERVED = {"item.json", ".durable-owned.json", ".durable-pruned.json", ".durable-lifecycle.json"}
ITEM_ID = re.compile(r"[a-f0-9]{32}\Z")


def _relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid inventory path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe relative path")
    return parts


def _safe(root, relative):
    path = Path(root)
    if path.is_symlink():
        raise ValueError("symlinked root")
    for part in _relative(relative):
        path /= part
        if path.is_symlink():
            raise ValueError("symlinked path")
    return path


def _read_json(root, relative):
    path = _safe(root, relative)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("missing or linked metadata")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("metadata must be an object")
    return result


def _note(vault, relative):
    path = _safe(vault, relative)
    if not path.is_file():
        raise ValueError("missing or nonregular review/note")
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not match:
        raise ValueError("missing note frontmatter")
    return match[1], text[match.end():].strip("\n")


def url_keys(url):
    """Identity keys of a video URL: the URL itself and any numeric post or YouTube id in it."""
    if not isinstance(url, str) or not url.strip():
        return set()
    url = url.strip()
    keys = {url.rstrip("/")}
    keys.update(re.findall(r"(?<!\d)\d{10,}(?!\d)", url))
    match = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if match:
        keys.add(match[1])
    return keys


def _scalar(metadata, name):
    # Backend-generated frontmatter: one `name: value` line, optionally quoted.
    match = re.search(rf"(?m)^{re.escape(name)}:[ \t]*(.*?)[ \t]*$", metadata)
    if not match:
        return None
    value = match[1]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _epoch(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def _vault_notes(vault, keys):
    """Inbox, Notes and Archive/Inbox notes whose source_url shares an identity key.

    Fails closed (ValueError) when the vault is not a readable schema 4 vault, when a folder
    is missing, or when an unparseable note mentions one of the keys.
    """
    schema = _read_json(vault, ".brain-schema.json")
    if schema.get("schema_version") != 4:
        raise ValueError("vault is not schema 4")
    found = []
    for folder in ("Inbox", "Notes", "Archive/Inbox"):
        directory = _safe(vault, folder)
        if not directory.is_dir():
            raise ValueError(f"{folder} unavailable")
        for path in sorted(directory.glob("*.md")):
            relative = f"{folder}/{path.name}"
            try:
                metadata, _ = _note(vault, relative)
            except (ValueError, UnicodeDecodeError):
                if path.is_symlink():
                    raise ValueError("symlinked note")
                raw = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                if any(key in raw for key in keys):
                    raise ValueError("unparseable note may match this video")
                continue
            if not keys & url_keys(_scalar(metadata, "source_url")):
                continue
            sources = re.findall(r"\[\[(.+?)\]\]", next(iter(re.findall(r"(?m)^sources:.*$", metadata)), ""))
            found.append({"folder": folder, "name": path.name, "status": _scalar(metadata, "status"),
                          "updated": _epoch(_scalar(metadata, "updated")), "sources": sources})
    return found


def _source_exists(vault, stem):
    for folder in ("Sources", "Archive/Sources"):
        try:
            if _safe(vault, f"{folder}/{stem}.md").is_file():
                return True
        except ValueError:
            return False
    return False


def _eligibility(home, vault, record, now, retention_seconds):
    identifier = record.get("item_id")
    if not isinstance(identifier, str) or not ITEM_ID.fullmatch(identifier):
        raise ValueError("invalid item identity")
    job = _safe(home, f"social-video-work/{identifier}")
    if job.resolve().is_relative_to(vault.resolve()) or vault.resolve().is_relative_to(job.resolve()):
        raise ValueError("job directory overlaps vault")
    item = _read_json(job, "item.json")
    owner = _read_json(job, ".durable-owned.json")
    request = record.get("request_id")
    if not request or item.get("request_id") != request or owner.get("request_id") != request or owner.get("item_id") != identifier:
        raise ValueError("request identity mismatch")
    if record.get("original_url") != item.get("original_url"):
        raise ValueError("original URL mismatch")
    if owner.get("capture_preparations"):
        return job, owner, "capture_pending", False
    marker_path = _safe(job, ".durable-lifecycle.json")
    marker = _read_json(job, ".durable-lifecycle.json") if marker_path.exists() else {}
    if marker.get("item_id", identifier) != identifier or marker.get("request_id", request) != request:
        raise ValueError("lifecycle identity mismatch")
    if marker.get("awaiting_review") is True:
        return job, owner, "inline_review_pending", False
    state = item.get("state")
    if state not in {"completed", "partial", "failed", "blocked"}:
        return job, owner, "nonterminal", False
    urls = {value for value in (item.get("original_url"), item.get("canonical_url")) if isinstance(value, str) and value}
    keys = set().union(*(url_keys(url) for url in urls))
    notes = _vault_notes(vault, keys)
    # Undecided: any Inbox file, including one a human already marked approved but not yet promoted.
    if any(note["folder"] == "Inbox" for note in notes):
        return job, owner, "inbox_undecided", False
    finished = item.get("finished_at")
    finished = finished if isinstance(finished, (int, float)) and not isinstance(finished, bool) else None
    # Only notes written after this job finished can be its capture; older notes for the same
    # video cannot close a newer job.
    linked = [note for note in notes if finished is not None and note["updated"] is not None
              and note["updated"] >= finished]
    if linked:
        for note in linked:
            if note["folder"] == "Notes":
                if note["status"] != "approved" or not note["sources"] or not all(
                        _source_exists(vault, stem) for stem in note["sources"]):
                    return job, owner, "save_unverified", False
        return job, owner, "capture_decided_verified", True
    if marker.get("review_closed") is True and marker.get("outcome") == "rejected":
        return job, owner, "inline_proposal_rejected", True
    if state != "completed":
        return job, owner, "unresolved_analysis", False
    if finished is None or not 0 < finished <= now:
        return job, owner, "unknown_completion_time", False
    expired = now - finished >= retention_seconds
    return job, owner, "completed_retention_expired" if expired else "retention_window", expired


def _open_parent(job, relative):
    parts = _relative(relative)
    descriptor = os.open(job, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _check_owned(job, entry):
    relative = entry.get("path")
    parts = _relative(relative)
    if any(part in RESERVED or part.startswith(".durable-") for part in parts):
        raise ValueError("reserved metadata in inventory")
    descriptor, name = _open_parent(job, relative)
    try:
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("linked or nonregular inventory file")
        if info.st_size != entry.get("size") or info.st_mtime_ns != entry.get("mtime_ns"):
            raise ValueError("owned file changed")
        sha = entry.get("sha256")
        if sha is None and info.st_ino != entry.get("inode"):
            raise ValueError("owned file inode changed")
        if sha is not None:
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("invalid owned checksum")
            file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(file_descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("owned file changed while checking")
                if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                    raise ValueError("owned checksum changed")
        return info
    finally:
        os.close(descriptor)


def _prune_job(job, owner, reason, now, dry_run, progress):
    entries = owner.get("files")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("invalid ownership inventory")
    paths = [entry.get("path") for entry in entries]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate inventory path")
    checked = [(entry, _check_owned(job, entry)) for entry in entries]
    total, deleted = 0, []
    for entry, before in checked:
        if before is None:
            continue
        current = _check_owned(job, entry)
        if current is None or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("owned file changed before deletion")
        if not dry_run:
            descriptor, name = _open_parent(job, entry["path"])
            try:
                final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (final.st_dev, final.st_ino, final.st_nlink, final.st_size, final.st_mtime_ns) != (
                        current.st_dev, current.st_ino, 1, current.st_size, current.st_mtime_ns):
                    raise ValueError("owned file changed at deletion")
                os.unlink(name, dir_fd=descriptor)
            finally:
                os.close(descriptor)
        total += before.st_size
        deleted.append(entry["path"])
        progress.update(deleted_bytes=total, files=list(deleted))
    if not dry_run and deleted:
        receipt = _safe(job, ".durable-pruned.json")
        descriptor, temporary = tempfile.mkstemp(prefix=".durable-pruned-", dir=job)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"item_id": owner["item_id"], "request_id": owner["request_id"], "pruned_at": now,
                           "reason": reason, "files": deleted, "deleted_bytes": total}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return total, deleted


def prune_owned(home: Path, vault: Path, records: list[dict], retention_seconds=7 * 86400, now=None, dry_run=False) -> dict:
    """Prune inventoried files only; retain job identity and all vault content."""
    home, vault = Path(home), Path(vault)
    now = time.time() if now is None else now
    result = {"jobs": [], "deleted_bytes": 0, "dry_run": dry_run, "errors": []}
    descriptor = None
    try:
        if retention_seconds < 0 or not vault.is_dir() or vault.is_symlink():
            raise ValueError("invalid retention settings or unavailable vault")
        lock = _safe(home, "social-video-work/worker.lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if os.fstat(descriptor).st_nlink != 1:
            raise ValueError("linked worker lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["reason"] = "active_worker"
            return result
        for record in records:
            report = {"item_id": record.get("item_id"), "deleted_bytes": 0, "files": [], "errors": []}
            job_descriptor = None
            try:
                identifier = record.get("item_id")
                if not isinstance(identifier, str) or not ITEM_ID.fullmatch(identifier):
                    raise ValueError("invalid item identity")
                job_lock = _safe(home, f"social-video-work/{identifier}/.durable-prune.lock")
                job_descriptor = os.open(job_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                if os.fstat(job_descriptor).st_nlink != 1:
                    raise ValueError("linked job lock")
                fcntl.flock(job_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job, owner, reason, eligible = _eligibility(home, vault, record, now, retention_seconds)
                report["reason"] = reason
                if eligible:
                    _prune_job(job, owner, reason, now, dry_run, report)
            except (OSError, ValueError, KeyError, TypeError) as error:
                report["reason"] = "partial_prune_error" if report["files"] else "protected_uncertain"
                report["errors"].append(str(error))
            finally:
                if job_descriptor is not None:
                    os.close(job_descriptor)
            result["deleted_bytes"] += report["deleted_bytes"]
            result["jobs"].append(report)
    except (OSError, ValueError, TypeError) as error:
        result["reason"] = "protected_uncertain"
        result["errors"].append(str(error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result
