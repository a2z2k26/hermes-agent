import asyncio
from pathlib import Path
from types import SimpleNamespace

from gateway.platforms.discord import InboxReviewView, _is_inbox_review_stop_trigger, _is_inbox_review_trigger
from gateway.inbox_review import InboxReviewItem


def _interaction(user_id, role_ids=None):
    return SimpleNamespace(user=SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=r) for r in (role_ids or [])]))


def test_inbox_review_view_uses_component_auth_with_roles():
    view = InboxReviewView(
        manager=None,
        items=[],
        session_id="sess",
        allowed_user_ids=set(),
        allowed_role_ids={42},
    )

    assert view._check_auth(_interaction(999, role_ids=[42])) is True
    assert view._check_auth(_interaction(999, role_ids=[7])) is False


def test_inbox_review_view_disables_destructive_delete_by_default():
    view = InboxReviewView(
        manager=None,
        items=[],
        session_id="sess",
        allowed_user_ids=set(),
    )

    assert getattr(view, "allow_delete", None) is False


def test_inbox_review_view_tracks_decisions_for_completion_summary():
    view = InboxReviewView(
        manager=None,
        items=[],
        session_id="sess",
        allowed_user_ids=set(),
    )
    view.decisions = [
        {"action": "studio", "review_status": "promoted-creative-studio"},
        {"action": "delete", "review_status": "deleted-from-vault"},
    ]

    summary = view._decision_summary_text(cleanup_report={"removed_files": 1, "removed_dirs": 0, "skipped": 0})

    assert "studio: 1" in summary
    assert "delete: 1" in summary
    assert "Temporary preview cleanup: 1 file(s), 0 dir(s), 0 skipped" in summary


def test_inbox_review_view_exposes_deterministic_content_type_actions():
    view = InboxReviewView(
        manager=None,
        items=[SimpleNamespace(note_path="note.md", title="T", suggested_route="needs-review", confidence="unknown", excerpt="")],
        session_id="sess",
        allowed_user_ids=set(),
    )

    option_values = []
    option_labels = []
    placeholders = []
    for child in view.children:
        placeholders.append(getattr(child, "placeholder", ""))
        option_values.extend(getattr(option, "value", "") for option in getattr(child, "options", []))
        option_labels.extend(getattr(option, "label", "") for option in getattr(child, "options", []))

    assert "Choose primary action..." in placeholders
    assert option_labels == [
        "Brain / Knowledge [b/k]",
        "Studio / Creative [s]",
        "Receipt [r]",
        "Calendar Event [c]",
        "Job Description [j]",
        "GitHub Repo / Project [g]",
        "Negative Reference [x]",
        "Delete from Vault [d]",
    ]
    assert "brain" in option_values
    assert "studio" in option_values
    assert "receipt" in option_values
    assert "event" in option_values
    assert "job-opportunity" in option_values
    assert "github-repository" in option_values
    assert "both" not in option_values
    assert "negative-reference" in option_values
    assert "delete" in option_values
    assert "later" not in option_values
    assert "reject" not in option_values
    assert "business-opportunity" not in option_values
    assert "learning-item" not in option_values
    assert "essay-seed" not in option_values
    assert "research-request" not in option_values


def test_inbox_review_view_mobile_buttons_are_stacked_and_named_for_review_flow():
    view = InboxReviewView(
        manager=None,
        items=[SimpleNamespace(note_path="note.md", title="T", suggested_route="studio", confidence="medium", excerpt="")],
        session_id="sess",
        allowed_user_ids=set(),
        allow_delete=True,
    )

    buttons = [child for child in view.children if getattr(child, "label", None)]
    labels = [button.label for button in buttons]
    rows = [button.row for button in buttons]

    assert labels == ["Item Details", "Stop Review"]
    assert rows == [1, 2]
    assert "Details" not in labels
    assert "Stop" not in labels
    assert "Accept Item" not in labels
    assert "Delete Item" not in labels


def test_inbox_review_embed_is_mobile_first_and_hides_absolute_paths(tmp_path):
    vault = tmp_path / "Second-Brain"
    note = vault / "00 Inbox" / "Screenshots" / (
        "Screenshot Intake — 2026-05-08 — Screenshot_20260508_183222_Instagram — db4b61806e67.md"
    )
    item = InboxReviewItem(
        note_path=note,
        title="Screenshot Intake — 2026-05-08 — Screenshot_20260508_183222_Instagram — db4b61806e67",
        suggested_route="creative-studio",
        confidence="medium",
        excerpt="Instagram Reel screenshot showing a creative technical workflow about adding brand-support to a renderer.",
    )
    manager = SimpleNamespace(vault_path=Path(vault))
    view = InboxReviewView(
        manager=manager,
        items=[item],
        session_id="sess",
        allowed_user_ids=set(),
        allow_delete=True,
    )

    embed, file_obj = view.build_current_payload(include_preview=False)
    field_map = {field.name: field.value for field in embed.fields}

    assert file_obj is None
    assert embed.title == "Inbox Review 1/1 · Screenshot"
    assert "Screenshot Intake" not in embed.title
    assert "Recommended primary action" in field_map
    assert field_map["Recommended primary action"] == "Studio / Creative"
    assert "creative-studio" not in field_map["Recommended primary action"]
    assert field_map["Location"] == "`00 Inbox/Screenshots`"
    assert str(tmp_path) not in str(embed.to_dict())
    assert "Reject" not in (embed.footer.text or "")
    assert "Intent tags" in (embed.footer.text or "")


def test_inbox_review_view_uses_attachments_when_swapping_preview_files():
    import inspect

    source = inspect.getsource(InboxReviewView._apply_review_action)

    assert 'edit_kwargs["attachments"] = [file_obj]' in source
    assert 'edit_kwargs["file"] = file_obj' not in source


def test_inbox_review_action_defers_before_vault_io_to_avoid_mobile_interaction_failure():
    import inspect

    source = inspect.getsource(InboxReviewView._apply_review_action)

    assert "await interaction.response.defer()" in source
    assert "interaction.edit_original_response" in source
    assert source.index("await interaction.response.defer()") < source.index("self.manager.apply_decision")


def test_inbox_review_action_refreshes_queue_from_vault_after_each_decision():
    first = SimpleNamespace(note_path="first.md", title="First", suggested_route="studio", confidence="unknown", excerpt="first")
    second = SimpleNamespace(note_path="second.md", title="Second", suggested_route="brain", confidence="unknown", excerpt="second")

    class Response:
        async def defer(self):
            return None

    class Manager:
        vault_path = Path("/tmp/vault")

        def __init__(self):
            self.applied = []

        def apply_decision(self, note_path, action, **kwargs):
            self.applied.append((note_path, action))
            return {"success": True, "action": action, "review_status": "promoted-creative-studio", "note_path": note_path}

        def discover_items(self, limit=100):
            return [second]

        def create_preview(self, *args, **kwargs):
            return None

    edits = []

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    manager = Manager()
    view = InboxReviewView(
        manager=manager,
        items=[first, second],
        session_id="sess",
        allowed_user_ids=set(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        response=Response(),
        edit_original_response=edit_original_response,
    )

    asyncio.run(view._apply_review_action(interaction, "studio"))

    assert manager.applied == [("first.md", "studio")]
    assert view.index == 0
    assert view.items == [second]
    assert edits
    assert edits[-1]["embed"].title == "Inbox Review 1/1 · Inbox Item"


def test_inbox_review_action_does_not_mutate_vault_if_discord_ack_fails():
    class FailingResponse:
        async def defer(self):
            raise RuntimeError("unknown interaction")

        async def send_message(self, *args, **kwargs):
            raise AssertionError("should not send after failed defer")

        async def edit_message(self, *args, **kwargs):
            raise AssertionError("should not edit after failed defer")

    class Manager:
        vault_path = Path("/tmp/vault")

        def __init__(self):
            self.called = False

        def apply_decision(self, *args, **kwargs):
            self.called = True
            return {"success": True}

    manager = Manager()
    view = InboxReviewView(
        manager=manager,
        items=[SimpleNamespace(note_path="note.md", title="T", suggested_route="studio", confidence="unknown", excerpt="")],
        session_id="sess",
        allowed_user_ids=set(),
    )
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), response=FailingResponse())

    asyncio.run(view._apply_review_action(interaction, "studio"))

    assert manager.called is False
    assert view.index == 0

def test_inbox_review_text_triggers_route_to_discord_ui_before_agent_turns():
    accepted = [
        "o inbox",
        "O INBOX",
        "review inbox",
        "inbox review",
        "/inbox review",
        "review screenshots",
        "review captures",
        "/review-inbox",
        "/review_inbox",
        "inbox",
    ]
    for trigger in accepted:
        assert _is_inbox_review_trigger(trigger)

    rejected = [
        "please review my inbox later",
        "review screenshot workflow bug",
        "inbox status report",
        "hello inbox",
        "o inbox please",
    ]
    for text in rejected:
        assert not _is_inbox_review_trigger(text)

def test_inbox_review_stop_text_triggers_route_to_discord_ui_stop_path():
    accepted = [
        "stop review",
        "stop inbox",
        "cancel review",
        "cancel inbox",
        "end review",
        "end inbox",
    ]
    for trigger in accepted:
        assert _is_inbox_review_stop_trigger(trigger)
        assert not _is_inbox_review_trigger(trigger)

    rejected = [
        "stop reviewing my notes later",
        "please cancel the review maybe",
        "review stop",
        "stop",
    ]
    for text in rejected:
        assert not _is_inbox_review_stop_trigger(text)


def test_inbox_review_view_timeout_is_long_enough_for_mobile_review():
    view = InboxReviewView(
        manager=None,
        items=[],
        session_id="sess",
        allowed_user_ids=set(),
    )

    assert view.timeout >= 2 * 60 * 60

