from pathlib import Path

import pytest

from gateway.inbox_review import (
    InboxReviewManager,
    is_safe_non_vault_cleanup_path,
    cleanup_expired_cache,
    normalize_review_action,
)


def test_manager_discovers_unreviewed_inbox_note_with_asset_and_excerpt(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    asset_dir = vault / "_assets" / "screenshots" / "2026-05-06"
    inbox.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "screen.png"
    asset.write_bytes(b"fake image bytes")
    note = inbox / "Screenshot Intake — Test.md"
    note.write_text(
        "---\n"
        "title: Test Capture\n"
        "candidate_territory: creative-studio\n"
        "routing_confidence: high\n"
        "review_needed: true\n"
        "image: _assets/screenshots/2026-05-06/screen.png\n"
        "---\n"
        "# Test Capture\n\n"
        "## What This Is\nA product UI reference.\n\n"
        "## Extracted Text\nImportant OCR text here.\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    items = manager.discover_items(limit=5)

    assert len(items) == 1
    item = items[0]
    assert item.title == "Test Capture"
    assert item.note_path == note
    assert item.asset_path == asset
    assert item.suggested_route == "creative-studio"
    assert "A product UI reference" in item.excerpt


def test_manager_skips_rejected_and_processed_notes(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    (inbox / "Rejected.md").write_text("---\nreview_status: rejected-candidate\n---\n# Rejected\n", encoding="utf-8")
    (inbox / "Processed.md").write_text("---\nstatus: processed\n---\n# Processed\n", encoding="utf-8")
    (inbox / "Review Needed False.md").write_text(
        "---\nreview_needed: false\n---\n# Already addressed by an interrupted review\n",
        encoding="utf-8",
    )
    (inbox / "Unknown Reviewed Status.md").write_text(
        "---\nreview_status: classified-custom-future-type\n---\n# Already addressed by a newer action\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")

    assert manager.discover_items() == []


def test_manager_orders_unreviewed_items_by_source_timestamp_across_folders_and_dedupes_assets(tmp_path):
    vault = tmp_path / "vault"
    screenshots = vault / "00 Inbox" / "Screenshots"
    captures = vault / "00 Inbox" / "Captures"
    asset_dir = vault / "_assets" / "screenshots"
    screenshots.mkdir(parents=True)
    captures.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "same.png"
    asset.write_bytes(b"same image")

    old_note = screenshots / "Old.md"
    old_note.write_text(
        "---\n"
        "title: Old\n"
        "review_needed: true\n"
        "source_mtime: 2026-07-20T09:00:00-06:00\n"
        "image: _assets/screenshots/same.png\n"
        "---\n# Old\n",
        encoding="utf-8",
    )
    duplicate_old_note = captures / "Duplicate Old.md"
    duplicate_old_note.write_text(
        "---\n"
        "title: Duplicate Old\n"
        "review_needed: true\n"
        "source_mtime: 2026-07-20T09:00:00-06:00\n"
        "image: _assets/screenshots/same.png\n"
        "---\n# Duplicate Old\n",
        encoding="utf-8",
    )
    newest_note = captures / "Newest.md"
    newest_note.write_text(
        "---\n"
        "title: Newest\n"
        "review_needed: true\n"
        "source_mtime: 2026-07-22T09:00:00-06:00\n"
        "image: _assets/screenshots/new.png\n"
        "---\n# Newest\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    items = manager.discover_items(limit=10)

    assert [item.title for item in items] == ["Newest", "Old"]


def test_apply_decision_deletes_note_and_unshared_asset_when_explicitly_allowed(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    asset_dir = vault / "_assets" / "screenshots"
    inbox.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "screen.png"
    asset.write_bytes(b"fake image bytes")
    note = inbox / "Candidate.md"
    note.write_text(
        "---\n"
        "title: Candidate\n"
        "review_needed: true\n"
        "image: _assets/screenshots/screen.png\n"
        "---\n"
        "# Candidate\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "delete", user="tester", allow_delete=True)

    assert result["success"] is True
    assert result["review_status"] == "deleted-from-vault"
    assert not note.exists()
    assert not asset.exists()
    ledgers = list((vault / "05 Agent Coordination" / "Inbox Review Reports").glob("Inbox Delete Ledger — *.md"))
    assert ledgers
    assert "Note deleted: `00 Inbox/Screenshots/Candidate.md`" in ledgers[0].read_text(encoding="utf-8")


def test_apply_decision_refuses_delete_without_explicit_confirmation(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    note = inbox / "Candidate.md"
    note.write_text("# Candidate\n", encoding="utf-8")

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "delete", user="tester")

    assert result["success"] is False
    assert "explicit" in result["error"].lower()
    assert note.exists()


def test_review_action_keyboard_aliases_match_dropdown_actions():
    assert normalize_review_action("b") == "brain"
    assert normalize_review_action("k") == "brain"
    assert normalize_review_action("s") == "studio"
    assert normalize_review_action("r") == "receipt"
    assert normalize_review_action("c") == "event"
    assert normalize_review_action("j") == "job-opportunity"
    assert normalize_review_action("g") == "github-repository"
    assert normalize_review_action("github") == "github-repository"
    assert normalize_review_action("repo") == "github-repository"
    assert normalize_review_action("d") == "delete"
    assert normalize_review_action("x") == "negative-reference"
    assert normalize_review_action("both") == "both"  # unsupported by UI/menu; retained as raw legacy value
    assert normalize_review_action("n") == "negative-reference"
    assert normalize_review_action("negative-reference") == "negative-reference"


def test_apply_decision_accepts_core_review_actions(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")

    both_note = inbox / "Bridge Candidate.md"
    both_note.write_text("---\ntitle: Bridge Candidate\nreview_needed: true\n---\n# Bridge Candidate\n", encoding="utf-8")
    both_result = manager.apply_decision(both_note, "both", user="tester")
    assert both_result["success"] is True
    assert both_result["review_status"] == "promoted-bridge"
    assert "review_status: promoted-bridge" in both_note.read_text(encoding="utf-8")

    negative_note = inbox / "Negative Candidate.md"
    negative_note.write_text("---\ntitle: Negative Candidate\nreview_needed: true\n---\n# Negative Candidate\n", encoding="utf-8")
    negative_result = manager.apply_decision(negative_note, "x", user="tester")
    assert negative_result["success"] is True
    assert negative_result["action"] == "negative-reference"
    assert negative_result["review_status"] == "classified-negative-reference"
    negative_text = negative_note.read_text(encoding="utf-8")
    assert "content_type: negative-reference" in negative_text
    assert "territory: creative-studio" in negative_text
    assert "tags: [negative-reference, anti-pattern]" in negative_text


def test_apply_decision_accepts_suggested_candidate_route(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    note = inbox / "Candidate.md"
    note.write_text(
        "---\n"
        "title: Candidate\n"
        "candidate_territory: creative-studio\n"
        "review_needed: true\n"
        "---\n"
        "# Candidate\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "suggested", user="tester")

    assert result["success"] is True
    assert result["action"] == "studio"
    assert result["review_status"] == "promoted-creative-studio"
    text = note.read_text(encoding="utf-8")
    assert "review_status: promoted-creative-studio" in text
    assert "- Action: `studio`" in text


def test_apply_decision_marks_deterministic_content_type_without_deleting_or_promoting_to_brain_studio(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    note = inbox / "Receipt Candidate.md"
    note.write_text("---\ntitle: Receipt Candidate\nreview_needed: true\n---\n# Receipt Candidate\n", encoding="utf-8")

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "receipt", user="tester")

    assert result["success"] is True
    assert result["action"] == "receipt"
    assert result["review_status"] == "classified-receipt"
    assert result["content_type"] == "receipt"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "review_status: classified-receipt" in text
    assert "content_type: receipt" in text
    assert "sensitivity: financial-record" in text
    assert "territory: inbox" in text
    assert "- Content type: `receipt`" in text


def test_apply_decision_marks_github_repository_as_second_brain_object_with_tags(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    note = inbox / "GitHub Candidate.md"
    note.write_text(
        "---\n"
        "title: GitHub Candidate\n"
        "review_needed: true\n"
        "tags:\n"
        "  - capture/screenshot\n"
        "---\n"
        "# GitHub Candidate\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "github", user="tester")

    assert result["success"] is True
    assert result["action"] == "github-repository"
    assert result["review_status"] == "classified-github-repository"
    assert result["content_type"] == "github-repository"
    text = note.read_text(encoding="utf-8")
    assert "territory: second-brain" in text
    assert "content_type: github-repository" in text
    assert "source_type: github-repository" in text
    assert "source_platform: github" in text
    assert "tags: [capture/screenshot, github, repository, codebase]" in text
    assert "  - capture/screenshot" not in text
    assert "- Content type: `github-repository`" in text


def test_apply_decision_accepts_suggested_object_type_before_territory(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "00 Inbox" / "Screenshots"
    inbox.mkdir(parents=True)
    note = inbox / "Job Candidate.md"
    note.write_text(
        "---\n"
        "title: Job Candidate\n"
        "candidate_content_type: job-opportunity\n"
        "candidate_territory: second-brain\n"
        "review_needed: true\n"
        "---\n"
        "# Job Candidate\n",
        encoding="utf-8",
    )

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.apply_decision(note, "suggested", user="tester")

    assert result["success"] is True
    assert result["action"] == "job-opportunity"
    assert result["review_status"] == "classified-job-opportunity"
    text = note.read_text(encoding="utf-8")
    assert "content_type: job-opportunity" in text
    assert "sensitivity: career-opportunity" in text


def test_create_review_report_writes_additive_session_summary(tmp_path):
    vault = tmp_path / "vault"
    (vault / "05 Agent Coordination" / "Inbox Review Reports").mkdir(parents=True)
    note_a = vault / "00 Inbox" / "Screenshots" / "A.md"
    note_b = vault / "00 Inbox" / "Screenshots" / "B.md"

    manager = InboxReviewManager(vault_path=vault, hermes_home=tmp_path / "hermes")
    result = manager.create_review_report(
        session_id="discord-test-session",
        decisions=[
            {"title": "A", "note_path": str(note_a), "action": "studio", "review_status": "promoted-creative-studio"},
            {"title": "B", "note_path": str(note_b), "action": "delete", "review_status": "deleted-from-vault"},
        ],
        total_items=2,
        cleanup_report={"removed_files": 1, "removed_dirs": 1, "skipped": 0},
        user="tester",
    )

    assert result["success"] is True
    report_path = Path(result["report_path"])
    assert report_path.exists()
    assert report_path.is_relative_to(vault)
    text = report_path.read_text(encoding="utf-8")
    assert "Inbox Review Report" in text
    assert "studio: 1" in text
    assert "delete: 1" in text
    assert "removed_files: 1" in text


def test_cleanup_guard_allows_only_known_non_vault_cache_paths(tmp_path):
    hermes = tmp_path / "hermes"
    vault = tmp_path / "vault"
    allowed = hermes / "cache" / "inbox-review" / "session" / "preview.webp"
    denied_vault = vault / "_assets" / "screen.png"
    denied_skill = hermes / "skills" / "x" / "SKILL.md"

    assert is_safe_non_vault_cleanup_path(allowed, hermes, vault) is True
    assert is_safe_non_vault_cleanup_path(denied_vault, hermes, vault) is False
    assert is_safe_non_vault_cleanup_path(denied_skill, hermes, vault) is False


def test_cleanup_expired_cache_removes_old_files_and_prunes_empty_dirs(tmp_path):
    hermes = tmp_path / "hermes"
    vault = tmp_path / "vault"
    root = hermes / "cache" / "inbox-review"
    old_dir = root / "old-session"
    new_dir = root / "new-session"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_file = old_dir / "preview.webp"
    new_file = new_dir / "preview.webp"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    old_time = 1_000_000
    new_time = 2_000_000
    old_file.touch()
    new_file.touch()
    import os
    os.utime(old_file, (old_time, old_time))
    os.utime(new_file, (new_time, new_time))

    report = cleanup_expired_cache(root, hermes_home=hermes, vault_path=vault, ttl_seconds=60, now=new_time)

    assert report["removed_files"] == 1
    assert not old_file.exists()
    assert new_file.exists()
    assert not old_dir.exists()
