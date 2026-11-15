from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from second_brain import migrate_v4
from second_brain.migrate_v4 import MigrationError

REGISTRY = """---
id: map-tag-registry
type: map
title: Tag Registry
---

# Tag Registry

## Registered tags

- `alpha` — Alpha meaning.
- `beta` — Beta meaning.
- `gamma` — Gamma meaning.
- `old-x` — Old x meaning.
- `drop-me` — To be dropped.
"""


def fm_text(**fields: object) -> str:
    return "---\n" + yaml.safe_dump(fields, allow_unicode=True, sort_keys=False) + "---\n\n"


def note_text(
    note_id, title, domain, *, tags, source, source_id, maps=("Topic",), note_type="concept", extra=""
):
    front = fm_text(
        id=note_id,
        type=note_type,
        domain=domain,
        status="approved",
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-02T00:00:00+00:00",
        title=title,
        aliases=[f"{title} alias"],
        tags=list(tags),
        maps=list(maps),
        source_kind="note",
        source_url=f"https://example.com/{note_id}",
        source_note=f"[[{source}]]",
        source_id=source_id,
    )
    body = (
        f"# {title}\n\n## Summary\n\nSummary of {title}.\n\n{extra}"
        f"## Connections\n\n- None yet\n\n## Maps\n\n- [[Topic]]\n\n## Source\n\n"
        f"- Original: [[{source}]]\n- URL: https://example.com/{note_id}\n"
    )
    return front + body


def source_text(source_id, distilled, captured):
    return (
        fm_text(
            id=source_id,
            type="source",
            status="approved",
            captured=captured,
            distilled_note=f"[[{distilled}]]",
            source_kind="note",
        )
        + f"# {distilled} — Original Source\n\n## Original content\n\nRaw [[Not A Real Link]] text.\n"
    )


def write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(vault: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def proposal_json(proposal_id, title, *, revision=False, target=""):
    return {
        "id": proposal_id,
        "note_type": "concept",
        "domain": "tech",
        "title": title,
        "summary": f"Summary of {title}.",
        "content": "Original capture text.",
        "tags": ["old-x", "alpha"],
        "aliases": ["pending alias"],
        "maps": ["Topic"],
        "connections": [{"relation": "about", "target": "Linker", "reason": "related"}],
        "source_url": "https://example.com/pending",
        "source_kind": "article",
        "source_creator": "someone",
        "source_path": f"Sources/{proposal_id}-{title}.md",
        "target_path": target or f"Tech/Notes/{title}.md",
        "created_at": "2026-03-01T00:00:00+00:00",
        "status": "pending",
        "is_revision": revision,
        "body": "Body text.",
    }


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    write(root, ".brain-schema.json", json.dumps({"schema_version": 3, "domains": [], "note_types": []}))
    write(root, ".gitignore", ".second-brain/\n")
    write(root, "Maps/Tag Registry.md", REGISTRY)
    write(
        root,
        "Maps/Topic.md",
        fm_text(id="map-topic", type="map", title="Topic")
        + "# Topic\n\nHuman intro that must stay.\n\n## Recently added\n\n"
        "- [[Dup]] — one\n- [[Linker]] — two\n\nHuman outro that must stay.\n",
    )
    write(
        root,
        "Maps/Other.md",
        fm_text(id="map-other", type="map", title="Other") + "# Other\n\nNo entries yet.\n",
    )
    write(root, "Home.md", "# Home\n\n![[Reviews/Review Queue.base]]\n\nSee [[Tech/Notes/Linker]].\n")
    write(
        root,
        "Personal/Notes/Dup.md",
        note_text(
            "note-p1",
            "Dup",
            "personal",
            tags=["alpha", "old-x", "beta"],
            source="aaa-Dup",
            source_id="source-aaa",
        ),
    )
    write(
        root,
        "Tech/Notes/Dup.md",
        note_text("note-t1", "Dup", "tech", tags=["alpha"], source="bbb-Dup", source_id="source-bbb"),
    )
    write(
        root,
        "Tech/Notes/Linker.md",
        note_text(
            "note-t2",
            "Linker",
            "tech",
            tags=["alpha", "beta", "gamma", "old-x", "drop-me", "extra"],
            source="ddd-Linker",
            source_id="source-ddd",
            extra="Links: [[Dup]], [[Dup|the alias]], [[Dup#Heading]].\n\n",
        ),
    )
    write(root, "Tech/Notes/diagram.png", "not really a png")
    write(root, "Sources/aaa-Dup.md", source_text("source-aaa", "Dup", "2026-01-01T00:00:00+00:00"))
    write(root, "Sources/bbb-Dup.md", source_text("source-bbb", "Dup", "2026-01-01T00:00:00+00:00"))
    write(root, "Sources/ddd-Linker.md", source_text("source-ddd", "Linker", "2026-02-01T00:00:00+00:00"))
    write(
        root, "Sources/eee-Linker-early.md", source_text("source-eee", "Linker", "2026-01-15T00:00:00+00:00")
    )
    write(root, "Reviews/Review Queue.base", "filters: {}\n")
    write(root, "Reviews/Approved/x.md", "approved review\n")
    write(root, "Reviews/Pending/pend1.md", "pending review\n")
    write(root, "Reviews/Pending/pend2.md", "pending revision review\n")
    write(root, ".second-brain/proposals/pend1.json", json.dumps(proposal_json("pend1", "Fresh idea")))
    write(
        root,
        ".second-brain/proposals/pend2.json",
        json.dumps(proposal_json("pend2", "Linker v2", revision=True, target="Tech/Notes/Linker.md")),
    )
    done = proposal_json("pend3", "Already rejected")
    done["status"] = "rejected"
    write(root, ".second-brain/proposals/pend3.json", json.dumps(done))
    write(root, ".second-brain/proposals/pend4.json", json.dumps(proposal_json("pend4", "No review file")))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "schema 3")
    return root


TAG_MAP = {"old-x": "alpha", "drop-me": None, "extra": "beta"}
MAP_TAGS = {"Topic": ["alpha", "beta"]}


def read(vault: Path, rel: str) -> tuple[dict, str]:
    text = (vault / rel).read_text(encoding="utf-8")
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def test_plan_is_json_and_writes_nothing(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    json.dumps(result.as_dict())
    assert not result.as_dict()["empty"]
    assert git(vault, "status", "--porcelain") == ""
    assert (vault / "Tech/Notes/Dup.md").exists()


def test_clash_rename_and_link_rewrite(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    assert [r["new_title"] for r in result.renames] == ["Dup (Tech)"]
    migrate_v4.apply(result)
    assert (vault / "Notes/Dup.md").exists()
    assert (vault / "Notes/Dup (Tech).md").exists()
    _, body = read(vault, "Notes/Linker.md")
    assert "[[Dup (Tech)]]" in body
    assert "[[Dup (Tech)|the alias]]" in body
    assert "[[Dup (Tech)#Heading]]" in body
    assert "[[Dup]]" not in body
    # Path-style links follow the move; the embed of the retired queue gets the new view.
    home = (vault / "Home.md").read_text()
    assert "[[Linker]]" in home
    assert "![[Views/Review.base]]" in home


def test_note_frontmatter_sources_and_orphans(vault: Path):
    migrate_v4.apply(migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS))
    front, body = read(vault, "Notes/Linker.md")
    assert front["status"] == "approved"
    assert front["id"] == "note-t2"
    assert front["created"] == "2026-01-01T00:00:00+00:00"
    assert "maps" not in front and "source_note" not in front and "source_id" not in front
    # Oldest capture first: the orphan captured 2026-01-15 precedes the primary of 2026-02-01.
    assert front["sources"] == ["[[eee-Linker-early]]", "[[ddd-Linker]]"]
    assert front["source_url"] == "https://example.com/note-t2"
    assert (
        "- Original: [[eee-Linker-early]]\n- Original: [[ddd-Linker]]\n- URL: https://example.com/note-t2"
        in body
    )
    # Everything outside the Source section is untouched, including the stale Maps section.
    assert "## Maps\n\n- [[Topic]]" in body
    source_front, source_body = read(vault, "Sources/eee-Linker-early.md")
    assert source_front["distilled_note"] == "[[Linker]]"
    assert source_front["note_id"] == "note-t2"
    assert "Raw [[Not A Real Link]] text." in source_body


def test_tag_map_dedupes_drops_and_caps(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    over = [c for c in result.note_changes if c["id"] == "note-t2"][0]
    assert over["tags_after"] == ["alpha", "beta", "gamma"]
    migrate_v4.apply(result)
    assert read(vault, "Notes/Dup.md")[0]["tags"] == ["alpha", "beta"]
    registry = (vault / "Maps/Tag Registry.md").read_text()
    assert "`old-x`" not in registry and "`drop-me`" not in registry
    assert "- `alpha` — Alpha meaning." in registry
    assert "# Tag Registry" in registry


def test_tag_cap_reported(vault: Path):
    many = {"drop-me": "beta", "old-x": "gamma"}
    write(
        vault,
        "Tech/Notes/Many.md",
        note_text(
            "note-t9",
            "Many",
            "tech",
            tags=["alpha", "beta", "gamma", "t4", "t5", "t6"],
            source="zzz-Many",
            source_id="source-zzz",
        ),
    )
    write(vault, "Sources/zzz-Many.md", source_text("source-zzz", "Many", "2026-01-01T00:00:00+00:00"))
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "more")
    result = migrate_v4.plan(vault, tag_map=many, map_tags=MAP_TAGS)
    entry = [c for c in result.note_changes if c["id"] == "note-t9"][0]
    assert len(entry["tags_after"]) == 5 and entry["tags_dropped_over_limit"] == ["t6"]
    assert any("note-t9" in w for w in result.warnings)


def test_map_wrapping_preserves_human_text(vault: Path):
    migrate_v4.apply(migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS))
    front, body = read(vault, "Maps/Topic.md")
    assert front["map_tags"] == ["alpha", "beta"]
    assert front["type"] == "map"
    assert "Human intro that must stay." in body and "Human outro that must stay." in body
    start, end = body.index(migrate_v4.GENERATED_START), body.index(migrate_v4.GENERATED_END)
    block = body[start:end]
    # Regeneration replaces the wrapped hand-written entries with one line per tagged note,
    # linked by file stem, so the renamed clash gets its own entry rather than a duplicate.
    assert "- [[Dup]] —" in block and "- [[Dup (Tech)]] —" in block and "- [[Linker]] —" in block
    assert "Human" not in block
    other = (vault / "Maps/Other.md").read_text()
    assert migrate_v4.GENERATED_START in other and migrate_v4.GENERATED_END in other


def test_pending_proposals_become_inbox_notes(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    assert sorted(c["proposal_id"] for c in result.proposal_conversions) == ["pend1", "pend2"]
    migrate_v4.apply(result)
    front, body = read(vault, "Inbox/Fresh idea.md")
    assert front["id"] == "note-pend1"
    assert front["status"] == "inbox"
    assert front["created"] == "2026-03-01T00:00:00+00:00"
    assert front["tags"] == ["alpha"]
    assert "revises" not in front
    assert front["sources"] == ["[[pend1-Fresh idea]]"]
    assert "Summary of Fresh idea." in body and "## Details\n\nBody text." in body
    assert "- about: [[Linker]] — related" in body
    revision, _ = read(vault, "Inbox/Linker v2.md")
    assert revision["revises"] == "note-t2"
    source_front, source_body = read(vault, "Sources/pend1-Fresh idea.md")
    assert source_front["note_id"] == "note-pend1" and source_front["distilled_note"] == "[[Fresh idea]]"
    assert "Original capture text." in source_body
    assert not list((vault / "Inbox").glob("Already*")) and not list((vault / "Inbox").glob("No review*"))


def test_reviews_archived_view_and_stamp(vault: Path):
    result = migrate_v4.apply(migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS))
    assert result["applied"] and result["notes_moved"] == 3
    assert not (vault / "Reviews").exists()
    assert (vault / "Archive/Reviews/Pending/pend1.md").exists()
    base = yaml.safe_load((vault / "Views/Review.base").read_text())
    assert base["filters"]["and"] == ['file.inFolder("Inbox")']
    assert base["views"][0]["type"] == "table"
    assert base["views"][0]["order"] == [
        "file.name", "status", "type", "domain", "tags", "created", "review_problems",
    ]  # fmt: skip
    stamp = json.loads((vault / ".brain-schema.json").read_text())
    assert stamp == {
        "schema_version": 4,
        "domains": ["personal", "tech"],
        "note_types": ["concept", "essay", "project", "prompt", "skill"],
    }
    log = git(vault, "log", "--format=%s").splitlines()
    assert log == ["Migrate vault to schema 4", "schema 3"]
    assert git(vault, "status", "--porcelain") == ""


def test_attachment_left_in_place_with_warning(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    assert any("diagram.png" in w for w in result.warnings)
    assert result.removed_dirs == ["Personal"]
    migrate_v4.apply(result)
    assert (vault / "Tech/Notes/diagram.png").exists()
    assert not (vault / "Tech/Notes/Dup.md").exists()
    assert not (vault / "Personal").exists()


def test_empty_old_trees_are_removed(vault: Path):
    (vault / "Tech/Notes/diagram.png").unlink()
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "drop attachment")
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    assert result.removed_dirs == ["Personal", "Tech"]
    migrate_v4.apply(result)
    assert not (vault / "Personal").exists() and not (vault / "Tech").exists()


def test_dirty_tree_refused(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    (vault / "Home.md").write_text("changed\n")
    with pytest.raises(MigrationError, match="uncommitted"):
        migrate_v4.apply(result)
    assert (vault / "Tech/Notes/Dup.md").exists()


def test_wrong_stamp_refused(vault: Path):
    (vault / ".brain-schema.json").write_text(json.dumps({"schema_version": 2}))
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "stamp 2")
    with pytest.raises(MigrationError, match="schema 3"):
        migrate_v4.plan(vault)


def test_stale_stamp_at_apply_refused(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    (vault / ".brain-schema.json").write_text(json.dumps({"schema_version": 2}))
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "stamp 2")
    with pytest.raises(MigrationError, match="schema 3"):
        migrate_v4.apply(result)


def test_non_git_vault_refused(vault: Path):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)
    subprocess.run(["rm", "-rf", str(vault / ".git")], check=True)
    with pytest.raises(MigrationError, match="git repository"):
        migrate_v4.apply(result)


def test_second_plan_is_empty(vault: Path):
    migrate_v4.apply(migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS))
    again = migrate_v4.plan(vault)
    assert again.as_dict()["empty"] and "already on schema 4" in again.message
    assert again.moves == [] and again.note_changes == []
    assert migrate_v4.apply(again)["applied"] is False


def test_failed_apply_rolls_back(vault: Path, monkeypatch):
    result = migrate_v4.plan(vault, tag_map=TAG_MAP, map_tags=MAP_TAGS)

    def boom(root):
        raise MigrationError("boom")

    monkeypatch.setattr(migrate_v4, "_regenerate", boom)
    with pytest.raises(MigrationError, match="boom"):
        migrate_v4.apply(result)
    assert git(vault, "status", "--porcelain") == ""
    assert (vault / "Tech/Notes/Dup.md").exists() and not (vault / "Notes").exists()
