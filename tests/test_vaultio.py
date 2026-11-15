from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from second_brain.vaultio import GitJournal, VaultLock, atomic_move, atomic_write


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "keep.md").write_text("keep\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_atomic_write_replaces_content_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "note.md"
    atomic_write(target, "one")
    atomic_write(target, "two")

    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["note.md"]


def test_atomic_move_refuses_to_overwrite(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")

    with pytest.raises(FileExistsError):
        atomic_move(tmp_path / "a.md", tmp_path / "b.md")
    assert (tmp_path / "a.md").read_text() == "a"
    assert (tmp_path / "b.md").read_text() == "b"

    atomic_move(tmp_path / "a.md", tmp_path / "deep" / "c.md")
    assert not (tmp_path / "a.md").exists()
    assert (tmp_path / "deep" / "c.md").read_text() == "a"


def test_lock_is_reentrant_in_one_thread(tmp_path: Path) -> None:
    with VaultLock(tmp_path, timeout=1), VaultLock(tmp_path, timeout=1):
        pass
    with VaultLock(tmp_path, timeout=1):
        pass


def test_lock_excludes_another_thread(tmp_path: Path) -> None:
    events: list[str] = []
    ready = threading.Event()

    def other() -> None:
        ready.wait()
        with VaultLock(tmp_path, timeout=5):
            events.append("other")

    thread = threading.Thread(target=other)
    thread.start()
    with VaultLock(tmp_path):
        ready.set()
        time.sleep(0.2)
        events.append("main")
    thread.join()

    assert events == ["main", "other"]


def test_lock_times_out_when_held_elsewhere(tmp_path: Path) -> None:
    holder_ready, release = threading.Event(), threading.Event()

    def hold() -> None:
        with VaultLock(tmp_path):
            holder_ready.set()
            release.wait()

    thread = threading.Thread(target=hold)
    thread.start()
    holder_ready.wait()
    try:
        with pytest.raises(TimeoutError), VaultLock(tmp_path, timeout=0.2):
            pass
    finally:
        release.set()
        thread.join()


def test_journal_commits_only_the_named_paths(repo: Path) -> None:
    (repo / "new.md").write_text("new\n")
    (repo / "dirty.md").write_text("unrelated\n")
    (repo / "keep.md").write_text("changed by user\n")

    warnings = GitJournal(repo).commit("add new", [repo / "new.md"])

    assert warnings == []
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["new.md"]
    assert _git(repo, "log", "-1", "--format=%an|%s").strip() == "second-brain|add new"
    status = _git(repo, "status", "--porcelain")
    assert "dirty.md" in status and "keep.md" in status


def test_journal_records_deletions_and_ignores_other_staged_files(repo: Path) -> None:
    (repo / "staged.md").write_text("staged by user\n")
    _git(repo, "add", "staged.md")
    (repo / "keep.md").unlink()

    warnings = GitJournal(repo).commit("remove", [Path("keep.md")])

    assert warnings == []
    assert _git(repo, "show", "--name-status", "--format=", "HEAD").split() == ["D", "keep.md"]
    assert "A  staged.md" in _git(repo, "status", "--porcelain")


def test_journal_is_a_no_op_without_a_repo_or_when_disabled(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.md").write_text("a")
    assert GitJournal(plain).commit("x", [plain / "a.md"]) == []

    (repo / "n.md").write_text("n")
    monkeypatch.setenv("SECOND_BRAIN_GIT_AUTOCOMMIT", "0")
    assert GitJournal(repo).commit("x", [repo / "n.md"]) == []
    assert "n.md" in _git(repo, "status", "--porcelain")


def test_journal_returns_warnings_instead_of_raising(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "n.md").write_text("n")
    monkeypatch.setenv("PATH", "")

    warnings = GitJournal(repo).commit("x", [repo / "n.md"])

    assert warnings and "git" in warnings[0]


def test_journal_refuses_paths_outside_the_vault(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "x.md"
    outside.write_text("x")

    assert GitJournal(repo).commit("x", [outside])
