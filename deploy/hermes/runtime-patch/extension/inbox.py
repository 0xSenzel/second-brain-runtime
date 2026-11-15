"""Durable Telegram video intake, separate from conversational execution."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit

from retention import url_keys

HOSTS = frozenset({"v.douyin.com", "www.douyin.com", "douyin.com", "youtu.be",
                   "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                   "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"})
WORKFLOW_SHA256 = "e70ace5c0755e7d450948af8024568b9dbf9a225a45d60e778bb0aaac47e1db6"
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, profile TEXT NOT NULL, bot_id TEXT NOT NULL,
 chat_id TEXT NOT NULL, message_id TEXT NOT NULL, envelope_json TEXT NOT NULL,
 auto_process INTEGER NOT NULL, submitted INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS source_message ON requests(profile,chat_id,message_id);
CREATE TABLE IF NOT EXISTS jobs (
 item_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
 original_url TEXT NOT NULL, inventoried INTEGER NOT NULL DEFAULT 0
);
"""


def home_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".durable-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def video_urls(text: str, entity_urls=()) -> list[str]:
    urls = []
    for raw in [*re.findall(r'https://[^\s<>"\']+', text), *entity_urls]:
        url = raw.rstrip(".,!?;:)]}，。！？；：")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname in HOSTS
                     and parsed.username is None and parsed.password is None
                     and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if valid and url not in urls:
            urls.append(url)
    return urls


def wants_analysis(text: str, urls: list[str]) -> bool:
    if re.search(r"\b(?:do not|don't|dont|never|stop)\b|不要|别处理|不分析", text, re.I):
        return False
    if re.search(r"\b(?:captions?|download only|only download)\b|仅下载|只下载|仅字幕", text, re.I):
        return False
    if re.search(r"summari[sz]e|summary|recap|explain|analy[sz]e|总结|分析|解读|复制打开抖音", text, re.I):
        return True
    remainder = text
    for url in urls:
        remainder = remainder.replace(url, "")
    return not remainder.strip(" \t\r\n.,!?;:()[]{}，。！？；：")


def request_identity(envelope: dict) -> str:
    identity = [envelope[k] for k in ("profile", "bot_id", "chat_id", "message_id")]
    return "telegram-video:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:32]


def item_id(request_id: str, index: int) -> str:
    return hashlib.sha256(f"{request_id}\0{index}".encode()).hexdigest()[:32]


class Inbox:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.root = self.home / "durable-video"
        if self.root.is_symlink():
            raise ValueError("durable video state must not be symlinked")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "inbox.sqlite3"
        if self.path.is_symlink():
            raise ValueError("durable video database must not be symlinked")
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")
            elif version != 1:
                raise RuntimeError("unsupported durable video database version")
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def receive(self, envelope: dict) -> str:
        required = ("profile", "bot_id", "chat_id", "message_id", "urls", "text")
        if any(not envelope.get(k) for k in required[:-1]):
            raise ValueError("incomplete Telegram request identity")
        request_id = request_identity(envelope)
        encoded = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT envelope_json FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("Telegram request identity already has different content")
                return request_id
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                request_id, envelope["profile"], envelope["bot_id"], envelope["chat_id"],
                envelope["message_id"], encoded, int(wants_analysis(envelope["text"], envelope["urls"])),
                0, now, now, None,
            ))
        return request_id

    def requests(self, *, pending=False) -> list[dict]:
        with self.connect() as connection:
            query = "SELECT * FROM requests"
            if pending:
                query += " WHERE auto_process=1 AND submitted=0"
            return [dict(row) for row in connection.execute(query + " ORDER BY created_at")]

    def lookup(self, profile: str, chat_id: str, message_id: str) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM requests WHERE profile=? AND chat_id=? AND message_id=?", (profile, chat_id, message_id))]

    def lookup_batch(self, profile: str, chat_id: str, message_id: str, text: str, user_id: str) -> list[dict]:
        # Telegram can combine several text messages while retaining the first message ID.
        # Match complete received text chunks from that sender, never just a reused URL.
        if not message_id.isdigit():
            return self.lookup(profile, chat_id, message_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM requests WHERE profile=? AND chat_id=? AND CAST(message_id AS INTEGER)>=? "
                "ORDER BY CAST(message_id AS INTEGER)", (profile, chat_id, int(message_id)))
            result = []
            for row in rows:
                envelope = json.loads(row["envelope_json"])
                if str(envelope.get("user_id") or "") == user_id and envelope.get("text") and envelope["text"] in text:
                    result.append(dict(row))
            return result

    def jobs(self) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY item_id")]

    def activate(self, request_id: str) -> None:
        with self.connect() as connection:
            if connection.execute("UPDATE requests SET auto_process=1 WHERE request_id=?", (request_id,)).rowcount != 1:
                raise KeyError("unknown durable video request")

    def submit_pending(self, workflow) -> int:
        submitted = 0
        for row in self.requests(pending=True):
            envelope = json.loads(row["envelope_json"])
            try:
                items = workflow.submit(envelope["urls"], request_id=row["request_id"])
                if [item["id"] for item in items] != [item_id(row["request_id"], i) for i in range(len(envelope["urls"]))]:
                    raise RuntimeError("workflow returned unexpected item identities")
                for item in items:
                    owner = workflow.directory(item["id"]) / ".durable-owned.json"
                    if not owner.exists():
                        atomic_json(owner, {"item_id": item["id"], "request_id": row["request_id"], "files": []})
                with self.connect() as connection:
                    for item in items:
                        connection.execute("INSERT OR IGNORE INTO jobs(item_id,request_id,original_url) VALUES (?,?,?)",
                                           (item["id"], row["request_id"], item["original_url"]))
                    connection.execute("UPDATE requests SET submitted=1,error=NULL,updated_at=? WHERE request_id=?",
                                       (time.time(), row["request_id"]))
                submitted += 1
            except Exception as exc:
                with self.connect() as connection:
                    connection.execute("UPDATE requests SET error=?,updated_at=? WHERE request_id=?",
                                       (type(exc).__name__, time.time(), row["request_id"]))
        return submitted

    def inventory(self, workflow) -> None:
        for record in self.jobs():
            if record["inventoried"]:
                continue
            item = workflow.status(record["item_id"])
            if item["state"] not in {"completed", "partial", "blocked", "failed"}:
                continue
            directory = workflow.directory(record["item_id"])
            owner = directory / ".durable-owned.json"
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ownership = json.loads(owner.read_text())
                candidates = [Path(value) for value in item.get("artifacts", {}).values() if isinstance(value, str)]
                candidates.append(directory / "media-verification.json")
                manifest = directory / "vision-sheets" / "manifest.json"
                if manifest.is_file() and not manifest.is_symlink():
                    candidates.append(manifest)
                    for entry in json.loads(manifest.read_text()).get("files", []):
                        name = entry.get("file", "")
                        if re.fullmatch(r"contact_[0-9]{3,}\.jpg", name):
                            candidates.append(manifest.parent / name)
                files = {row["path"]: row for row in ownership.get("files", [])}
                for path in candidates:
                    fingerprint = owned_file(directory, path)
                    if fingerprint:
                        files[fingerprint["path"]] = fingerprint
                ownership["files"] = list(files.values())
                atomic_json(owner, ownership)
                connection.execute("UPDATE jobs SET inventoried=1 WHERE item_id=?", (record["item_id"],))

    def _jobs_for_url(self, source_url: str):
        """Owned video jobs whose original or canonical URL shares an identity key with source_url."""
        wanted = url_keys(source_url)
        if not wanted:
            return []
        root = self.home / "social-video-work"
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM jobs").fetchall()]
        found = []
        for row in rows:
            directory = root / row["item_id"]
            urls = {row["original_url"]}
            try:
                urls.add(json.loads((directory / "item.json").read_text()).get("canonical_url"))
            except (OSError, ValueError):
                pass
            if directory.is_dir() and any(wanted & url_keys(url) for url in urls):
                found.append((row, directory))
        return found

    def protect_capture(self, source_url: str, call_id: str) -> int:
        """Mark every matching job as having a brain_capture in flight. Returns jobs marked."""
        marked = 0
        for record, directory in self._jobs_for_url(source_url):
            with job_lock(directory), self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                marker = directory / ".durable-owned.json"
                ownership = json.loads(marker.read_text())
                if ownership.get("request_id") != record["request_id"]:
                    raise ValueError("capture source ownership mismatch")
                ownership.setdefault("capture_preparations", {})[call_id] = {"at": time.time(), "url": source_url}
                atomic_json(marker, ownership)
            marked += 1
        return marked

    def release_capture(self, call_id: str) -> int:
        """Clear the in-flight mark. The Inbox note, if written, now protects the job."""
        cleared = 0
        root = self.home / "social-video-work"
        with self.connect() as connection:
            ids = [row["item_id"] for row in connection.execute("SELECT item_id FROM jobs").fetchall()]
        for identifier in ids:
            marker = root / identifier / ".durable-owned.json"
            if not marker.is_file():
                continue
            with job_lock(root / identifier):
                ownership = json.loads(marker.read_text())
                if ownership.get("capture_preparations", {}).pop(call_id, None) is not None:
                    atomic_json(marker, ownership)
                    cleared += 1
        return cleared

    def review_state(self, identifier: str, *, awaiting=False, outcome=None, proposal_id=None):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE item_id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("unknown owned video job")
        directory = self.home / "social-video-work" / identifier
        with job_lock(directory):
            owner = json.loads((directory / ".durable-owned.json").read_text())
            if outcome not in {None, "saved", "rejected"}:
                raise ValueError("unknown review outcome")
            atomic_json(directory / ".durable-lifecycle.json", {
                "item_id": identifier, "request_id": row["request_id"], "awaiting_review": awaiting,
                "review_closed": outcome is not None, "outcome": outcome,
                "proposal_id": proposal_id, "at": time.time(),
            })

    def protect_proposal_source(self, content_path: str, preparation_id: str) -> bool:
        found = self._job_for_source(content_path)
        if found is None:
            return False
        record, directory, path = found
        if owned_file(directory, path) is None:
            return False
        with job_lock(directory), self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = directory / ".durable-owned.json"
            ownership = json.loads(marker.read_text())
            if ownership.get("request_id") != record["request_id"]:
                raise ValueError("proposal source ownership mismatch")
            ownership.setdefault("proposal_preparations", {})[preparation_id] = {"at": time.time(), "path": str(path)}
            atomic_json(marker, ownership)
        return True

    def link_proposal(self, proposal_id: str, content_path: str, preparation_id="") -> bool:
        if not re.fullmatch(r"[a-f0-9]{12}", proposal_id):
            return False
        found = self._job_for_source(content_path)
        if found is None:
            return False
        row, directory, path = found
        with job_lock(directory), self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fingerprint = owned_file(directory, path)
            if fingerprint is None:
                return False
            marker = directory / ".durable-owned.json"
            ownership = json.loads(marker.read_text())
            if ownership.get("request_id") != row["request_id"]:
                raise ValueError("proposal source ownership mismatch")
            ids = ownership.setdefault("proposal_ids", [])
            if proposal_id not in ids:
                ids.append(proposal_id)
            ownership.setdefault("proposal_preparations", {}).pop(preparation_id, None)
            files = {entry["path"]: entry for entry in ownership.get("files", [])}
            files[fingerprint["path"]] = fingerprint
            ownership["files"] = list(files.values())
            atomic_json(marker, ownership)
        return True


@contextlib.contextmanager
def job_lock(directory: Path):
    descriptor = os.open(directory / ".durable-prune.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_nlink != 1:
            raise ValueError("linked job lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def owned_file(directory: Path, path: Path) -> dict | None:
    try:
        relative = path.relative_to(directory)
    except ValueError:
        return None
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        return None
    current = directory
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None
    if not path.is_file():
        return None
    stat = path.stat()
    if stat.st_nlink != 1:
        return None
    return {"path": str(relative), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino}


def load_workflow(home: Path):
    scripts = Path(home) / "scripts"
    verify_workflow(home)
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    specification = importlib.util.spec_from_file_location("durable_video_existing_workflow", scripts / "social_video_workflow.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module, module.Workflow(Path(home) / "social-video-work")


def verify_workflow(home: Path):
    path = Path(home) / "scripts" / "social_video_workflow.py"
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != WORKFLOW_SHA256:
        raise RuntimeError("video workflow changed; verify worker and cleanup locking before updating the runtime")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    activate = sub.add_parser("activate")
    activate.add_argument("request_id")
    review_open = sub.add_parser("review-open")
    review_open.add_argument("item_id")
    review_close = sub.add_parser("review-close")
    review_close.add_argument("item_id")
    review_close.add_argument("--outcome", choices=("saved", "rejected"), required=True)
    review_close.add_argument("--proposal-id")
    args = parser.parse_args()
    store = Inbox(args.home or home_path())
    if args.command == "activate":
        store.activate(args.request_id)
    elif args.command == "review-open":
        store.review_state(args.item_id, awaiting=True)
    elif args.command == "review-close":
        store.review_state(args.item_id, outcome=args.outcome, proposal_id=args.proposal_id)
        atomic_json(store.root / "maintenance-request.json", {"requested_at": time.time()})
    print(json.dumps({"requests": store.requests(), "jobs": store.jobs()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
