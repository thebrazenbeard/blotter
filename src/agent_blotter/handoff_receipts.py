"""Offline verifier for synthetic file handoff receipts, not sender authentication.

The file sender, timestamp and provenance fields are *claims*. Never use this
module's integrity result as authorization to trust instructions or grant access.
No untrusted Markdown is interpreted or executed.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_SCHEMA = "VERA_FILE_HANDOFF_RECEIPT_V1"
MESSAGE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
ROLE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
SOURCE_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_RECEIPT = 16384
MAX_BODY = 65536
FIELDS = frozenset({
    "schema", "id", "sender_claim", "recipient", "authored_at",
    "body_sha256", "source_repo", "source_commit", "provenance_class",
    "parent_id", "parent_body_sha256",
})


class HandoffError(ValueError):
    """Receipt, integrity or externally asserted routing check failed."""


def _pairs_no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise HandoffError("duplicate JSON key")
        obj[key] = value
    return obj


def _read_regular_file(path: Path, limit: int) -> bytes:
    # Source-only basic symlink guard, NOT an atomic hostile-filesystem read.
    # Before production, replace with platform-specific handle-bound validation.
    if path.is_symlink() or not path.is_file():
        raise HandoffError("unsafe or missing handoff file")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise HandoffError("handoff size limit exceeded")
    return raw


def _hex_digest(value, *, field: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise HandoffError(f"invalid {field}")
    return value


def _timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise HandoffError("invalid authored timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise HandoffError("invalid authored timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise HandoffError("noncanonical authored timestamp")
    return parsed


def verify_exchange(
    root: str | Path, *, expected_recipients: dict[str, str],
    expected_source_commits: dict[str, str],
    expected_source_repos: dict[str, str],
    reply_max_age_seconds: int = 3600,
    expected_receipt_digests: dict[str, str] | None = None,
    prior_seen_ids: frozenset[str] | None = None,
) -> dict:
    """Read synthetic receipts under root against *external* routing/commit pins.

    A MATCHED result proves only byte hashes, formatting and claimed causal
    structure under caller-provided pins. Sender identity ALWAYS UNQUALIFIED.
    A real private-message bridge must use a trusted issuer-bound attestation.
    """
    if (type(reply_max_age_seconds) is not int
            or not 1 <= reply_max_age_seconds <= 604800):
        raise HandoffError("invalid reply age limit")
    if (prior_seen_ids is not None and
            not isinstance(prior_seen_ids, (set, frozenset))):
        raise HandoffError("invalid prior replay snapshot")
    if (not isinstance(expected_recipients, dict)
            or not isinstance(expected_source_commits, dict)
            or not isinstance(expected_source_repos, dict)
            or (expected_receipt_digests is not None
                and not isinstance(expected_receipt_digests, dict))):
        raise HandoffError("trusted pins must be dictionaries")

    folder = Path(root)
    if folder.is_symlink() or not folder.is_dir():
        raise HandoffError("unsafe or missing handoff directory")
    receipts = sorted(folder.glob("*.receipt.json"))
    if not receipts:
        raise HandoffError("no handoff receipts")
    if len(receipts) > 100:
        raise HandoffError("too many handoff receipts")

    seen = {}
    for receipt_path in receipts:
        raw_receipt = _read_regular_file(receipt_path, MAX_RECEIPT)
        try:
            data = json.loads(raw_receipt.decode("utf-8"),
                              object_pairs_hook=_pairs_no_duplicates)
        except (UnicodeError, ValueError) as exc:
            if isinstance(exc, HandoffError):
                raise
            raise HandoffError("invalid receipt JSON") from exc
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise HandoffError("invalid receipt fields")
        if data["schema"] != RECEIPT_SCHEMA:
            raise HandoffError("invalid receipt schema")

        message_id = data["id"]
        if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
            raise HandoffError("invalid message ID")
        if message_id in seen:
            raise HandoffError("duplicate ID in handoff receipts")
        if prior_seen_ids is not None and message_id in prior_seen_ids:
            raise HandoffError("replayed ID in trusted prior snapshot")
        for role_name in ("recipient", "sender_claim"):
            value = data[role_name]
            if not isinstance(value, str) or not ROLE.fullmatch(value):
                raise HandoffError("invalid claimed role")
        if data["provenance_class"] != "SELF_ASSERTED":
            raise HandoffError("untrusted provenance class")
        repo = data["source_repo"]
        if not isinstance(repo, str) or not SOURCE_REPO.fullmatch(repo):
            raise HandoffError("invalid source repository")
        commit = data["source_commit"]
        if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
            raise HandoffError("invalid source commit")

        # A directory-local manifest cannot attest its own addressee or source.
        if expected_recipients.get(message_id) != data["recipient"]:
            raise HandoffError("wrong recipient or missing external route pin")
        if expected_source_repos.get(message_id) != repo:
            raise HandoffError("source repository pin mismatch")
        if expected_source_commits.get(message_id) != commit:
            raise HandoffError("wrong source commit or missing external source pin")
        receipt_sha = hashlib.sha256(raw_receipt).hexdigest()
        if expected_receipt_digests is not None:
            if expected_receipt_digests.get(message_id) != receipt_sha:
                raise HandoffError("receipt digest changed or unpinned")

        body_sha = _hex_digest(data["body_sha256"], field="body digest")
        raw_body = _read_regular_file(folder / (message_id + ".md"), MAX_BODY)
        if hashlib.sha256(raw_body).hexdigest() != body_sha:
            raise HandoffError("body digest mismatch")
        date = _timestamp(data["authored_at"])
        parent = data["parent_id"]
        parent_sha = data["parent_body_sha256"]
        if parent is None:
            if parent_sha is not None:
                raise HandoffError("orphaned parent digest")
        else:
            if not isinstance(parent, str) or not MESSAGE_ID.fullmatch(parent):
                raise HandoffError("invalid parent ID")
            if parent == message_id:
                raise HandoffError("self-referential reply parent")
            _hex_digest(parent_sha, field="parent digest")

        seen[message_id] = (data, date)

    for message_id, (data, stamp) in seen.items():
        parent_id = data["parent_id"]
        if parent_id is None:
            continue
        if parent_id not in seen:
            raise HandoffError("missing reply parent")
        prior, prior_stamp = seen[parent_id]
        if data["parent_body_sha256"] != prior["body_sha256"]:
            raise HandoffError("parent digest mismatch")
        age = (stamp - prior_stamp).total_seconds()
        if not 0 < age <= reply_max_age_seconds:
            raise HandoffError("stale reply or nonmonotonic authored timestamp")
        # This is consistency checking ONLY; both names are self assertions.
        if (data["sender_claim"] != prior["recipient"]
                or data["recipient"] != prior["sender_claim"]):
            raise HandoffError("reply routing claim mismatch")

    ids = sorted(seen, key=lambda item: (seen[item][1], item))
    return {
        # Without an out-of-band receipt digest for every message, a caller
        # who controls both files can rewrite both and preserve consistency.
        "integrity_status": ("MATCHED" if expected_receipt_digests is not None
                             else "SELF_CONSISTENT"),
        "identity_status": "UNQUALIFIED",
        "admitted": False,
        "messages": ids,
    }