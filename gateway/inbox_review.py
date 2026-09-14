from __future__ import annotations

"""Phone-safe Obsidian Inbox review helpers for Discord UI flows.

This module intentionally separates durable vault state from disposable UI/cache
state. Review actions update vault notes except for the explicit ``delete``
action, which is only honored when a trusted Discord review flow opts in with
``allow_delete=True`` and writes an audit ledger entry.
"""

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

try:  # Optional; previews gracefully degrade to text-only if unavailable.
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - depends on local optional dependency
    Image = None
    ImageOps = None


INBOX_FOLDERS = (
    "00 Inbox/Screenshots",
    "00 Inbox/Camera Captures",
    "00 Inbox/Receipts",
    "00 Inbox/Captures",
    "00 Inbox/Links",
    "00 Inbox/Needs Review",
)

REVIEWED_STATUSES = {
    "processed",
    "promoted",
    "rejected",
    "archived",
}

REVIEWED_REVIEW_STATUSES = {
    "rejected-candidate",
    "promoted-second-brain",
    "promoted-creative-studio",
    "promoted-bridge",
    "classified-receipt",
    "classified-calendar-event",
    "classified-job-opportunity",
    "classified-github-repository",
    "classified-negative-reference",
    # Legacy statuses may exist from earlier experimental review actions; keep
    # them as reviewed so old notes do not re-enter the queue, but do not expose
    # the actions in CONTENT_TYPE_ACTIONS or the Discord UI.
    "classified-business-opportunity",
    "classified-learning-item",
    "classified-essay-seed",
    "classified-research-request",
    "reviewed",
    "done",
}

ACTION_TO_STATUS = {
    "brain": ("promoted-second-brain", "processed"),
    "second-brain": ("promoted-second-brain", "processed"),
    "studio": ("promoted-creative-studio", "processed"),
    "creative-studio": ("promoted-creative-studio", "processed"),
    "both": ("promoted-bridge", "processed"),
    "bridge": ("promoted-bridge", "processed"),
}

CONTENT_TYPE_ACTIONS = {
    "receipt": {
        "aliases": {"receipt", "bill", "expense", "financial-record"},
        "review_status": "classified-receipt",
        "status": "processed",
        "content_type": "receipt",
        "territory": "inbox",
        "sensitivity": "financial-record",
    },
    "event": {
        "aliases": {"event", "calendar", "calendar-event", "event-candidate", "appointment"},
        "review_status": "classified-calendar-event",
        "status": "processed",
        "content_type": "calendar-event",
        "territory": "inbox",
        "sensitivity": "time-sensitive",
    },
    "job-opportunity": {
        "aliases": {"job", "job-description", "job-opportunity", "career", "role"},
        "review_status": "classified-job-opportunity",
        "status": "processed",
        "content_type": "job-opportunity",
        "territory": "inbox",
        "sensitivity": "career-opportunity",
    },
    "github-repository": {
        "aliases": {"github", "github-repo", "github-repository", "repo", "repository", "codebase", "project-repo"},
        "review_status": "classified-github-repository",
        "status": "processed",
        "content_type": "github-repository",
        "territory": "second-brain",
        "sensitivity": "public-code-reference",
        "source_type": "github-repository",
        "source_platform": "github",
        "tags": ["github", "repository", "codebase"],
    },
    "negative-reference": {
        "aliases": {"negative-reference", "negative", "anti-reference", "anti-pattern", "bad-example"},
        "review_status": "classified-negative-reference",
        "status": "processed",
        "content_type": "negative-reference",
        "territory": "creative-studio",
        "sensitivity": "public-reference",
        "tags": ["negative-reference", "anti-pattern"],
    },
}

CONTENT_TYPE_ALIAS_TO_ACTION = {
    alias: action
    for action, metadata in CONTENT_TYPE_ACTIONS.items()
    for alias in metadata["aliases"]
}

REVIEW_ACTION_ALIASES = {
    "b": "brain",
    "brain": "brain",
    "knowledge": "brain",
    "knowledge-base": "brain",
    "kb": "brain",
    "k": "brain",
    "s": "studio",
    "studio": "studio",
    "creative": "studio",
    "creative-studio": "studio",
    "r": "receipt",
    "receipt": "receipt",
    "event": "event",
    "c": "event",
    "calendar": "event",
    "calendar-event": "event",
    "j": "job-opportunity",
    "job": "job-opportunity",
    "job-opportunity": "job-opportunity",
    "g": "github-repository",
    "github": "github-repository",
    "github-repo": "github-repository",
    "github-repository": "github-repository",
    "repo": "github-repository",
    "repository": "github-repository",
    "n": "negative-reference",
    "x": "negative-reference",
    "negative": "negative-reference",
    "negative-reference": "negative-reference",
    "anti-reference": "negative-reference",
    "anti-pattern": "negative-reference",
    "d": "delete",
    "delete": "delete",
    "delete-from-vault": "delete",
}


@dataclass(frozen=True)
class InboxReviewItem:
    note_path: Path
    title: str
    suggested_route: str
    confidence: str
    excerpt: str
    asset_path: Optional[Path] = None

    def to_embed_fields(self, index: int, total: int) -> Dict[str, str]:
        return {
            "title": f"Inbox Review {index}/{total} — {self.title}",
            "suggested": self.suggested_route or "needs-review",
            "confidence": self.confidence or "unknown",
            "excerpt": self.excerpt or "No excerpt available.",
            "note": str(self.note_path),
        }


class InboxReviewManager:
    def __init__(self, vault_path: Optional[Path | str] = None, hermes_home: Optional[Path | str] = None):
        self.vault_path = Path(vault_path) if vault_path else resolve_vault_path()
        self.hermes_home = Path(hermes_home) if hermes_home else resolve_hermes_home()
        self.cache_root = self.hermes_home / "cache" / "inbox-review"

    def discover_items(self, limit: int = 10) -> List[InboxReviewItem]:
        candidates: List[tuple[float, int, Path, Dict[str, Any]]] = []
        sequence = 0
        for folder in INBOX_FOLDERS:
            root = self.vault_path / folder
            if not root.exists():
                continue
            for note in sorted(root.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                parsed = parse_note(note)
                if should_skip_reviewed(parsed):
                    continue
                candidates.append((review_sort_timestamp(note, parsed), sequence, note, parsed))
                sequence += 1

        items: List[InboxReviewItem] = []
        seen_keys: set[str] = set()
        for _timestamp, _sequence, note, parsed in sorted(candidates, key=lambda row: (-row[0], row[1])):
            key = review_identity_key(self.vault_path, note, parsed)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append(self._build_item(note, parsed))
            if len(items) >= limit:
                return items
        return items

    def apply_decision(self, note_path: Path | str, action: str, *, user: str = "Discord review", allow_delete: bool = False) -> Dict[str, Any]:
        note = Path(note_path)
        if not is_within(note, self.vault_path):
            return {"success": False, "error": "Refusing to modify a note outside the configured vault."}
        if not note.exists():
            return {"success": False, "error": f"Note not found: {note}"}

        normalized = (action or "").strip().lower()
        normalized = normalize_review_action(normalized)
        if normalized in {"suggested", "accept-suggested", "accept suggested"}:
            parsed = parse_note(note)
            normalized = suggested_action_from_frontmatter(parsed.get("frontmatter", {}))
        if normalized == "delete":
            if not allow_delete:
                return {
                    "success": False,
                    "error": "Vault deletion requires an explicit Delete action from an authorized review flow.",
                }
            return self._delete_review_item(note, user=user)

        if normalized == "watch":
            # ACH-07 / O6: fire-and-forget watch-candidates queue append per BUILD-00 4A.
            # Returns EARLY, like the delete branch above: watch must not set
            # review_needed=false or rewrite the note. The item stays in the inbox for
            # later human review - that is what "watch this" means.
            try:
                import json as _json
                import secrets as _secrets
                from datetime import timezone as _timezone
        
                _ts = datetime.now(_timezone.utc).isoformat(timespec="seconds")
                _queue_dir = Path(self.vault_path) / "_meta" / "queues" / "watch-candidates"
                _queue_dir.mkdir(parents=True, exist_ok=True)
                _payload = {
                    "item_ref": str(note),
                    "proposed_by": "achilles",
                    "ts": _ts,
                }
                _safe = re.sub(r"[^0-9A-Za-z_.+-]+", "-", _ts)
                _target = _queue_dir / f"{_safe}-achilles-{_secrets.token_hex(2)}.json"
                _target.write_text(_json.dumps(_payload, sort_keys=True) + "\n", encoding="utf-8")
            except Exception as _exc:
                return {"success": False, "error": f"watch queue append failed: {_exc}"}
            return {
                "success": True,
                "action": normalized,
                "note_path": str(note),
                "queued": str(_target),
            }

        if normalized in CONTENT_TYPE_ACTIONS:
            metadata = CONTENT_TYPE_ACTIONS[normalized]
            review_status = str(metadata["review_status"])
            status = str(metadata["status"])
            text = note.read_text(encoding="utf-8")
            frontmatter_updates = {
                "review_needed": "false",
                "review_status": review_status,
                "status": status,
                "content_type": str(metadata["content_type"]),
                "territory": str(metadata["territory"]),
                "sensitivity": str(metadata["sensitivity"]),
                "reviewed_by": user,
                "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            }
            for optional_key in ("source_type", "source_platform"):
                if optional_key in metadata:
                    frontmatter_updates[optional_key] = str(metadata[optional_key])
            if "tags" in metadata:
                merged_tags = merge_frontmatter_list_values(text, "tags", metadata["tags"])
                frontmatter_updates["tags"] = format_inline_yaml_list(merged_tags)
        elif normalized in ACTION_TO_STATUS:
            review_status, status = ACTION_TO_STATUS[normalized]
            frontmatter_updates = {
                "review_needed": "false",
                "review_status": review_status,
                "status": status,
                "reviewed_by": user,
                "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            }
        else:
            return {"success": False, "error": f"Unsupported inbox review action: {action}"}

        text = note.read_text(encoding="utf-8")
        text = upsert_frontmatter(text, frontmatter_updates)
        content_type_line = ""
        if normalized in CONTENT_TYPE_ACTIONS:
            content_type_line = f"- Content type: `{frontmatter_updates['content_type']}`\n"
        decision_block = (
            "\n\n## Inbox Review Decision\n\n"
            f"- Action: `{normalized}`\n"
            f"- Review status: `{review_status}`\n"
            f"{content_type_line}"
            f"- Reviewed by: {user}\n"
            f"- Reviewed at: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        if "## Inbox Review Decision" not in text:
            text = text.rstrip() + decision_block + "\n"
        note.write_text(text, encoding="utf-8")
        result = {"success": True, "action": normalized, "review_status": review_status, "status": status, "note_path": str(note)}
        if normalized in CONTENT_TYPE_ACTIONS:
            result["content_type"] = str(frontmatter_updates["content_type"])
        return result

    def _delete_review_item(self, note: Path, *, user: str) -> Dict[str, Any]:
        """Delete one reviewed vault note and its unshared canonical asset.

        This is intentionally per-item and audit-logged. It refuses paths outside
        the configured vault and skips asset deletion if another Markdown note
        still references the asset.
        """
        parsed = parse_note(note)
        title = str(parsed.get("frontmatter", {}).get("title") or first_heading(parsed.get("body", "")) or note.stem)
        asset_path = resolve_asset_path(self.vault_path, str(parsed.get("frontmatter", {}).get("image") or ""))
        asset_deleted = False
        asset_skip_reason = "no canonical asset"
        asset_rel = ""
        if asset_path is not None and asset_path.exists():
            if not is_within(asset_path, self.vault_path):
                asset_skip_reason = "asset outside vault"
            elif asset_referenced_elsewhere(asset_path, self.vault_path, excluding_note=note):
                asset_skip_reason = "asset referenced by another vault note"
                try:
                    asset_rel = str(asset_path.resolve().relative_to(self.vault_path.resolve()))
                except Exception:
                    asset_rel = str(asset_path)
            else:
                try:
                    asset_rel = str(asset_path.resolve().relative_to(self.vault_path.resolve()))
                except Exception:
                    asset_rel = str(asset_path)
                asset_path.unlink()
                asset_deleted = True
                asset_skip_reason = ""

        rel_note = str(note.resolve().relative_to(self.vault_path.resolve()))
        note.unlink()
        self._append_delete_ledger(
            title=title,
            note_rel=rel_note,
            asset_rel=asset_rel,
            asset_deleted=asset_deleted,
            asset_skip_reason=asset_skip_reason,
            user=user,
        )
        return {
            "success": True,
            "action": "delete",
            "review_status": "deleted-from-vault",
            "status": "deleted",
            "note_path": str(note),
            "deleted_note": True,
            "deleted_asset": asset_deleted,
            "asset_skip_reason": asset_skip_reason,
        }

    def _append_delete_ledger(
        self,
        *,
        title: str,
        note_rel: str,
        asset_rel: str,
        asset_deleted: bool,
        asset_skip_reason: str,
        user: str,
    ) -> None:
        reports_dir = self.vault_path / "05 Agent Coordination" / "Inbox Review Reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        ledger = reports_dir / f"Inbox Delete Ledger — {datetime.now().strftime('%Y-%m-%d')}.md"
        now = datetime.now().isoformat(timespec="seconds")
        if not ledger.exists():
            ledger.write_text(
                "---\n"
                f"title: Inbox Delete Ledger — {datetime.now().strftime('%Y-%m-%d')}\n"
                f"created: {now}\n"
                "type: inbox-delete-ledger\n"
                "territory: agent-coordination\n"
                "status: active\n"
                "tags: [agent-coordination, inbox-review, deletion-ledger]\n"
                "---\n\n"
                f"# Inbox Delete Ledger — {datetime.now().strftime('%Y-%m-%d')}\n\n",
                encoding="utf-8",
            )
        entry = [
            f"## {now} — Delete from Vault",
            "",
            f"- Title: {title}",
            f"- Note deleted: `{note_rel}`",
            f"- Asset: `{asset_rel or 'none'}`",
            f"- Asset deleted: `{str(asset_deleted).lower()}`",
        ]
        if asset_skip_reason:
            entry.append(f"- Asset skip reason: {asset_skip_reason}")
        entry.extend([f"- Reviewed by: {user}", ""])
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry) + "\n")

    def create_review_report(
        self,
        *,
        session_id: str,
        decisions: List[Dict[str, Any]],
        total_items: int,
        cleanup_report: Optional[Dict[str, int]] = None,
        user: str = "Discord review",
    ) -> Dict[str, Any]:
        """Write an additive Inbox Review session report inside the vault."""
        reports_dir = self.vault_path / "05 Agent Coordination" / "Inbox Review Reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        date_slug = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        report_path = reports_dir / f"Inbox Review Report — {date_slug} — {safe_slug(session_id)}.md"
        action_counts = Counter(str(d.get("action") or "unknown") for d in decisions)
        status_counts = Counter(str(d.get("review_status") or "unknown") for d in decisions)
        cleanup = cleanup_report or {"removed_files": 0, "removed_dirs": 0, "skipped": 0}

        lines = [
            "---",
            f"title: Inbox Review Report — {date_slug}",
            f"created: {now}",
            f"updated: {now}",
            "type: inbox-review-report",
            "territory: agent-coordination",
            "status: promoted",
            f"session_id: {session_id}",
            f"reviewed_by: {user}",
            "tags: [agent-coordination, inbox-review, discord-review]",
            "---",
            "",
            f"# Inbox Review Report — {date_slug}",
            "",
            "## Summary",
            "",
            f"- Total queued items: {total_items}",
            f"- Decisions recorded: {len(decisions)}",
            f"- Reviewed by: {user}",
            f"- Session ID: `{session_id}`",
            "- Safety: no vault files or vault assets were deleted by this report.",
            "",
            "## Action Counts",
            "",
        ]
        lines.extend(f"- {action}: {count}" for action, count in sorted(action_counts.items()))
        lines.extend(["", "## Review Status Counts", ""])
        lines.extend(f"- {status}: {count}" for status, count in sorted(status_counts.items()))
        lines.extend([
            "",
            "## Temporary Preview Cache Cleanup",
            "",
            f"- removed_files: {cleanup.get('removed_files', 0)}",
            f"- removed_dirs: {cleanup.get('removed_dirs', 0)}",
            f"- skipped: {cleanup.get('skipped', 0)}",
            "",
            "## Decisions",
            "",
        ])
        for idx, decision in enumerate(decisions, 1):
            note_path = str(decision.get("note_path") or "")
            rel_note = note_path
            try:
                rel_note = str(Path(note_path).resolve().relative_to(self.vault_path.resolve()))
            except Exception:
                pass
            lines.extend([
                f"### {idx}. {decision.get('title') or Path(note_path).stem or 'Untitled'}",
                "",
                f"- Action: `{decision.get('action', 'unknown')}`",
                f"- Review status: `{decision.get('review_status', 'unknown')}`",
                f"- Note: `{rel_note}`",
                "",
            ])
        report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return {"success": True, "report_path": str(report_path), "action_counts": dict(action_counts)}

    def create_preview(self, item: InboxReviewItem, session_id: str, max_px: int = 768, quality: int = 70) -> Optional[Path]:
        if not item.asset_path or not item.asset_path.exists():
            return None
        if not is_within(item.asset_path, self.vault_path):
            return None
        out_dir = self.cache_root / safe_slug(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{safe_slug(item.note_path.stem)[:80]}.webp"
        if Image is not None and ImageOps is not None:
            try:
                with Image.open(item.asset_path) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((max_px, max_px))
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    img.save(out_path, "WEBP", quality=quality, method=4)
                return out_path
            except Exception:
                try:
                    if out_path.exists():
                        out_path.unlink()
                except Exception:
                    pass
        return create_preview_with_ffmpeg(item.asset_path, out_path, max_px=max_px, quality=quality)

    def cleanup_session_cache(self, session_id: str) -> Dict[str, int]:
        return cleanup_expired_cache(
            self.cache_root / safe_slug(session_id),
            hermes_home=self.hermes_home,
            vault_path=self.vault_path,
            ttl_seconds=0,
            now=time.time() + 1,
        )

    def _build_item(self, note: Path, parsed: Dict[str, Any]) -> InboxReviewItem:
        fm = parsed["frontmatter"]
        body = parsed["body"]
        title = str(fm.get("title") or first_heading(body) or note.stem)
        suggested = str(
            fm.get("candidate_content_type")
            or fm.get("content_type")
            or fm.get("candidate_territory")
            or fm.get("territory")
            or "needs-review"
        )
        confidence = str(fm.get("routing_confidence") or fm.get("confidence") or "unknown")
        image_value = str(fm.get("image") or fm.get("asset") or "").strip().strip('"')
        asset_path = resolve_asset_path(self.vault_path, image_value) if image_value else None
        return InboxReviewItem(
            note_path=note,
            title=title,
            suggested_route=suggested,
            confidence=confidence,
            excerpt=extract_excerpt(body),
            asset_path=asset_path,
        )


def resolve_hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def resolve_vault_path() -> Path:
    env = os.getenv("OBSIDIAN_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    android = Path.home() / "storage" / "shared" / "Documents" / "Second-Brain"
    if android.exists():
        return android
    return android


def parse_note(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    frontmatter: Dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            raw = text[4:end]
            body = text[end + len("\n---"):].lstrip("\n")
            for line in raw.splitlines():
                if ":" not in line or line.lstrip().startswith("-"):
                    continue
                key, value = line.split(":", 1)
                frontmatter[key.strip()] = value.strip().strip('"')
    return {"frontmatter": frontmatter, "body": body, "text": text}


def should_skip_reviewed(parsed: Dict[str, Any]) -> bool:
    fm = {str(k): str(v).strip().lower() for k, v in parsed.get("frontmatter", {}).items()}
    status = fm.get("status", "")
    review_status = fm.get("review_status", "")
    review_needed = fm.get("review_needed", "").lower()
    if status in REVIEWED_STATUSES:
        return True
    if review_needed in {"false", "no", "0"}:
        return True
    if review_status and review_status not in {"unreviewed", "needs-review", "pending"}:
        return True
    if review_status in REVIEWED_REVIEW_STATUSES:
        return True
    return False


def review_sort_timestamp(note: Path, parsed: Dict[str, Any]) -> float:
    fm = parsed.get("frontmatter", {})
    for key in ("source_mtime", "captured_at", "created", "updated", "review_created_at"):
        value = fm.get(key)
        timestamp = parse_frontmatter_timestamp(value)
        if timestamp is not None:
            return timestamp
    try:
        return note.stat().st_mtime
    except OSError:
        return 0.0


def parse_frontmatter_timestamp(value: Any) -> Optional[float]:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def review_identity_key(vault_path: Path, note: Path, parsed: Dict[str, Any]) -> str:
    fm = parsed.get("frontmatter", {})
    for key in ("sha256", "source_sha256", "content_sha256", "hash_short"):
        value = str(fm.get(key) or "").strip().strip('"')
        if value:
            return f"{key}:{value.lower()}"
    for key in ("image", "asset", "vault_asset", "asset_path"):
        value = str(fm.get(key) or "").strip().strip('"')
        if value:
            asset = resolve_asset_path(vault_path, value)
            if asset is not None:
                try:
                    return f"asset:{asset.resolve()}"
                except Exception:
                    pass
            return f"asset:{value}"
    source_path = str(fm.get("source_path") or "").strip().strip('"')
    source_filename = str(fm.get("source_filename") or "").strip().strip('"')
    source_size = str(fm.get("source_size_bytes") or "").strip().strip('"')
    if source_path and source_size:
        return f"source:{source_path.lower()}:{source_size}"
    if source_filename and source_size:
        return f"source-file:{source_filename.lower()}:{source_size}"
    try:
        return f"note:{note.resolve()}"
    except Exception:
        return f"note:{note}"


def normalize_review_action(action: str) -> str:
    normalized = (action or "").strip().lower().replace("_", "-")
    normalized = CONTENT_TYPE_ALIAS_TO_ACTION.get(normalized, normalized)
    return REVIEW_ACTION_ALIASES.get(normalized, normalized)


def suggested_action_from_frontmatter(frontmatter: Dict[str, Any]) -> str:
    content_type = str(
        frontmatter.get("candidate_content_type")
        or frontmatter.get("content_type")
        or frontmatter.get("object_type")
        or ""
    ).strip().lower().replace("_", "-")
    content_action = normalize_review_action(content_type)
    if content_action in CONTENT_TYPE_ACTIONS:
        return content_action

    route = str(
        frontmatter.get("candidate_territory")
        or frontmatter.get("suggested_route")
        or frontmatter.get("territory")
        or ""
    ).strip().lower()
    route = route.replace("_", "-")
    if route in {"creative-studio", "studio", "creative"}:
        return "studio"
    if route in {"second-brain", "brain", "knowledge", "knowledge-base"}:
        return "brain"
    return "needs-review"


def first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def extract_excerpt(body: str, max_chars: int = 500) -> str:
    preferred_sections = (
        "At a Glance",
        "Key Takeaways",
        "Useful Signals",
        "What This Is",
        "Extracted Text",
        "Visual Analysis",
    )
    for section in preferred_sections:
        match = re.search(rf"^##\s+{re.escape(section)}\s*$([\s\S]*?)(?=^##\s+|\Z)", body, flags=re.MULTILINE)
        if match:
            cleaned = compact_markdown(match.group(1))
            if cleaned:
                return cleaned[:max_chars]
    return compact_markdown(body)[:max_chars]


def compact_markdown(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("![["):
            continue
        if line.startswith("#"):
            continue
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def resolve_asset_path(vault_path: Path, image_value: str) -> Optional[Path]:
    if not image_value:
        return None
    cleaned = image_value.strip()
    if cleaned.startswith("![[") and cleaned.endswith("]]"):
        cleaned = cleaned[3:-2]
    cleaned = cleaned.strip("[]")
    candidate = Path(cleaned).expanduser()
    if candidate.is_absolute():
        return candidate
    path = vault_path / cleaned
    if path.exists():
        return path
    # Obsidian embeds often use only basename; check common asset dirs lightly.
    matches = list((vault_path / "_assets").glob(f"**/{cleaned}")) if (vault_path / "_assets").exists() else []
    return matches[0] if matches else path


def create_preview_with_ffmpeg(asset_path: Path, out_path: Path, *, max_px: int, quality: int) -> Optional[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        # WEBP quality in ffmpeg is 0-100. Scale preserves aspect ratio and
        # strips metadata; output is a disposable Discord preview only.
        vf = f"scale='min({max_px},iw)':-2"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(asset_path),
                "-vf",
                vf,
                "-metadata",
                "exif=",
                "-quality",
                str(max(1, min(100, quality))),
                str(out_path),
            ],
            check=True,
            timeout=20,
        )
        return out_path if out_path.exists() else None
    except Exception:
        try:
            if out_path.exists():
                out_path.unlink()
        except Exception:
            pass
        return None


def upsert_frontmatter(text: str, updates: Dict[str, str]) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            raw = text[4:end]
            body = text[end + len("\n---"):]
            lines = raw.splitlines()
            seen = set()
            new_lines = []
            index = 0
            while index < len(lines):
                line = lines[index]
                if ":" in line and not line.lstrip().startswith("-"):
                    key = line.split(":", 1)[0].strip()
                    if key in updates:
                        new_lines.append(f"{key}: {updates[key]}")
                        seen.add(key)
                        index += 1
                        # If the replaced key used a block-list value, drop its
                        # old list entries so e.g. `tags: [...]` does not leave
                        # orphaned `- old-tag` rows under the new scalar value.
                        while index < len(lines) and is_yaml_child_line(lines[index]):
                            index += 1
                        continue
                new_lines.append(line)
                index += 1
            for key, value in updates.items():
                if key not in seen:
                    new_lines.append(f"{key}: {value}")
            return "---\n" + "\n".join(new_lines).rstrip() + "\n---" + body
    front = "---\n" + "\n".join(f"{k}: {v}" for k, v in updates.items()) + "\n---\n"
    return front + text.lstrip("\n")


def is_yaml_child_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and (line.startswith(" ") or line.startswith("\t") or stripped.startswith("- "))


def extract_frontmatter_list_values(text: str, key: str) -> List[str]:
    if not text.startswith("---\n"):
        return []
    end = text.find("\n---", 4)
    if end == -1:
        return []
    lines = text[4:end].splitlines()
    values: List[str] = []
    for index, line in enumerate(lines):
        if ":" not in line or line.lstrip().startswith("-"):
            continue
        current_key, raw_value = line.split(":", 1)
        if current_key.strip() != key:
            continue
        raw_value = raw_value.strip()
        if raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            if inner:
                values.extend(part.strip().strip('"\'') for part in inner.split(",") if part.strip())
        elif raw_value:
            values.extend(part.strip().strip('"\'') for part in re.split(r"[,\s]+", raw_value) if part.strip())
        cursor = index + 1
        while cursor < len(lines) and is_yaml_child_line(lines[cursor]):
            child = lines[cursor].strip()
            if child.startswith("- "):
                values.append(child[2:].strip().strip('"\''))
            cursor += 1
        break
    return [value for value in values if value]


def merge_frontmatter_list_values(text: str, key: str, additions: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for value in [*extract_frontmatter_list_values(text, key), *[str(item) for item in additions]]:
        cleaned = value.strip().strip("#")
        if not cleaned:
            continue
        marker = cleaned.lower()
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(cleaned)
    return merged


def format_inline_yaml_list(values: Iterable[str]) -> str:
    cleaned = [str(value).strip().strip("#") for value in values if str(value).strip()]
    return "[" + ", ".join(cleaned) + "]"


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def asset_referenced_elsewhere(asset_path: Path, vault_path: Path, *, excluding_note: Path) -> bool:
    """Return True when another Markdown note appears to reference asset_path."""
    try:
        rel = str(asset_path.resolve().relative_to(vault_path.resolve()))
    except Exception:
        rel = str(asset_path)
    needles = {rel, rel.replace(os.sep, "/"), asset_path.name}
    for note in vault_path.glob("**/*.md"):
        try:
            if note.resolve() == excluding_note.resolve():
                continue
            text = note.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if any(needle and needle in text for needle in needles):
            return True
    return False


def is_safe_non_vault_cleanup_path(path: Path, hermes_home: Path, vault_path: Path) -> bool:
    path = Path(path)
    if is_within(path, vault_path):
        return False
    allowed_roots = [
        hermes_home / "cache" / "inbox-review",
        hermes_home / "cache" / "cron",
        hermes_home / "tmp",
        Path.home() / ".cache" / "hermes",
    ]
    return any(is_within(path, root) or path.resolve() == root.resolve() for root in allowed_roots)


def cleanup_expired_cache(root: Path, *, hermes_home: Path, vault_path: Path, ttl_seconds: int, now: Optional[float] = None) -> Dict[str, int]:
    root = Path(root)
    now = time.time() if now is None else now
    report = {"removed_files": 0, "removed_dirs": 0, "skipped": 0}
    if not root.exists():
        return report
    if not is_safe_non_vault_cleanup_path(root, Path(hermes_home), Path(vault_path)):
        report["skipped"] += 1
        return report

    for file_path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: len(p.parts), reverse=True):
        if not is_safe_non_vault_cleanup_path(file_path, hermes_home, vault_path):
            report["skipped"] += 1
            continue
        try:
            if now - file_path.stat().st_mtime >= ttl_seconds:
                file_path.unlink()
                report["removed_files"] += 1
        except Exception:
            report["skipped"] += 1

    for dir_path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not is_safe_non_vault_cleanup_path(dir_path, hermes_home, vault_path):
            report["skipped"] += 1
            continue
        try:
            dir_path.rmdir()
            report["removed_dirs"] += 1
        except OSError:
            pass
    try:
        if root.exists() and not any(root.iterdir()) and is_safe_non_vault_cleanup_path(root, hermes_home, vault_path):
            root.rmdir()
            report["removed_dirs"] += 1
    except OSError:
        pass
    return report


def safe_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return value.strip("-._") or "session"
