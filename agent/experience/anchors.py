"""Repository-policy anchor validation for Decision Memory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_MAX_POLICY_ANCHOR_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class AnchorValidationResult:
    """Bounded result for one repository-policy anchor check."""

    valid: bool
    path: str
    reason: str | None = None
    file_size: int | None = None


def validate_repository_anchor(
    policy_anchor_path: str,
    policy_anchor_hash: str,
    *,
    repository_root: str | Path,
) -> AnchorValidationResult:
    """Validate a repository-policy Decision against live source bytes.

    The policy file body is deliberately not returned. Only metadata and the
    validation decision are exposed to callers that may later render context.
    """

    raw_path = str(policy_anchor_path).strip()
    raw_hash = str(policy_anchor_hash).strip().casefold()
    if not raw_path:
        return AnchorValidationResult(False, raw_path, "policy_anchor_path is required")
    if len(raw_hash) != 64 or any(char not in "0123456789abcdef" for char in raw_hash):
        return AnchorValidationResult(False, raw_path, "policy_anchor_hash must be SHA-256 hex")

    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or "\\" in raw_path:
        return AnchorValidationResult(False, raw_path, "policy_anchor_path must be repository-relative")
    normalized_path = path.as_posix() or "."

    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except OSError:
        return AnchorValidationResult(False, normalized_path, "repository_root is unavailable")
    if not root.is_dir():
        return AnchorValidationResult(False, normalized_path, "repository_root must be a directory")

    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return AnchorValidationResult(False, normalized_path, "policy_anchor_path must not be a symlink")
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return AnchorValidationResult(False, normalized_path, "policy_anchor_path escapes repository root")

    if not candidate.exists() or not candidate.is_file():
        return AnchorValidationResult(False, normalized_path, "policy anchor file is missing")

    try:
        data = candidate.read_bytes()
    except OSError:
        return AnchorValidationResult(False, normalized_path, "policy anchor file is unreadable")
    if len(data) > _MAX_POLICY_ANCHOR_BYTES:
        return AnchorValidationResult(False, normalized_path, "policy anchor file exceeds 1048576 bytes")

    observed = hashlib.sha256(data).hexdigest()
    if observed != raw_hash:
        return AnchorValidationResult(False, normalized_path, "policy anchor hash mismatch")
    return AnchorValidationResult(True, normalized_path, file_size=len(data))


__all__ = ["AnchorValidationResult", "validate_repository_anchor"]
