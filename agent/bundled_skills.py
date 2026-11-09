"""On-demand skill resources and provenance of copies made by old releases."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Iterator

from marlow_constants import get_bundled_skills_dir, get_marlow_home

NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"

_ON_DEMAND_PATHS = (
    Path("devops/webhook-subscriptions"),
    Path("mcp/native-mcp"),
)

# Fingerprints from the Marlow releases that seeded these directories.
# Older sync code could record the digest of a user-edited directory, so a
# matching manifest entry alone does not establish that the bytes shipped.
_LEGACY_DIGESTS = {
    Path("apple/apple-notes"): ("6c905d46810f376820670644f863829d",),
    Path("apple/apple-reminders"): ("6319c722da982d5f7d6853937b55d1c4",),
    Path("apple/findmy"): ("6372390ec1e7a582a3b3ab9d0c2a595d",),
    Path("apple/imessage"): ("c6bdb0096d2a430713a7477cd3b702e5",),
    Path("autonomous-ai-agents/claude-code"): ("416ad3b75835b9462879b8d05d99edc8",),
    Path("autonomous-ai-agents/codex"): ("aacc89b227d9fdaa7caf88f73eab4777",),
    Path("autonomous-ai-agents/marlow-agent"): (
        "528e550fd09f88f689d319b5b2fe36f2",  # 7fcfa509a
        "fbb3d6e79b5ebd5947af0dc01665d5c0",  # 1cd24a260
        "879687ae7b46fef3f257e960fb9430c6",  # 29114aa82
    ),
    Path("devops/webhook-subscriptions"): ("1ce2d3150d5d8195017b0a6afe7087db",),
    Path("email/himalaya"): ("9bf852519f137a239e730fbfd5045e8b",),
    Path("mcp/native-mcp"): ("13d50b58d81fc986214183e0b4156a43",),
    Path("research/research-paper-writing"): ("75fb226cbf2c7374a4ad6bcb7dd6f33a",),
}


def iter_on_demand_skills(name: str | None = None) -> Iterator[Path]:
    """Yield shipped skill files on explicit lookup, without reading Profile skills."""
    if (get_marlow_home() / NO_BUNDLED_SKILLS_MARKER).exists():
        return

    root = get_bundled_skills_dir(Path(__file__).resolve().parent.parent / "skills")
    for relative in _ON_DEMAND_PATHS:
        if name is not None and name not in {relative.name, relative.as_posix()}:
            continue
        skill_file = root / relative / "SKILL.md"
        try:
            if skill_file.is_file() and skill_file.resolve().is_relative_to(root.resolve()):
                yield skill_file
        except (OSError, RuntimeError):
            continue


def is_unchanged_legacy_skill(skill_file: Path, skills_dir: Path) -> bool:
    """Recognize an exact, manifest-tracked copy without touching its files."""
    if skill_file.name != "SKILL.md":
        return False
    directory = skill_file.parent
    try:
        relative = directory.relative_to(skills_dir)
    except ValueError:
        return False
    shipped = _LEGACY_DIGESTS.get(relative)
    if not shipped:
        return False

    manifest = skills_dir / ".bundled_manifest"
    try:
        if not stat.S_ISREG(manifest.lstat().st_mode) or any(
            path.is_symlink() for path in (directory, directory.parent, skills_dir)
        ):
            return False
        entries = [
            value.strip()
            for line in manifest.read_text(encoding="utf-8").splitlines()
            for name, _, value in [line.partition(":")]
            if name.strip() == relative.name
        ]
        if len(entries) != 1 or entries[0] not in shipped:
            return False

        # Match the old sync's directory hash, but fail open on unreadable
        # or linked files to keep copies of uncertain provenance discoverable.
        def raise_walk_error(error: OSError) -> None:
            raise error

        paths = []
        for root, directories, files in os.walk(directory, onerror=raise_walk_error):
            paths.extend(Path(root) / name for name in directories + files)
        digest = hashlib.md5(usedforsecurity=False)
        for path in sorted(paths):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                return False
            digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest() == entries[0]
    except (OSError, UnicodeError):
        return False
