from memory_mcp.domain.errors import MemoryError
from memory_mcp.domain.schemas import ProfilePatch


def apply_profile_patch(base: str, patch: ProfilePatch, *, current: str | None = None) -> str:
    """Rebase exact-text edits; preserve regions changed by a concurrent editor."""
    result = base if current is None else current
    spans: list[tuple[int, int]] = []
    for replacement in patch.replacements:
        if base.count(replacement.before) != 1:
            raise MemoryError("INVALID_PROFILE_PATCH", "Replacement must match exactly once.")
        start = base.index(replacement.before)
        end = start + len(replacement.before)
        if any(start < other_end and end > other_start for other_start, other_end in spans):
            raise MemoryError("INVALID_PROFILE_PATCH", "Profile replacements cannot overlap.")
        spans.append((start, end))
    # Work against the input snapshot, never text inserted by a previous edit.
    changes: list[tuple[int, int, str]] = []
    for replacement in patch.replacements:
        if result.count(replacement.before) == 1:
            start = result.index(replacement.before)
            changes.append((start, start + len(replacement.before), replacement.after))
        elif current is None:
            raise MemoryError("INVALID_PROFILE_PATCH", "Replacement target disappeared.")
        # A changed/ambiguous target after CAS conflict belongs to the newer writer.
    for start, end, text in sorted(changes, reverse=True):
        result = result[:start] + text + result[end:]
    addition = patch.append.strip()
    if addition and addition not in result:
        # A document created while we were preparing an initial profile wins.
        if not (current is not None and not base and current):
            result = f"{result.rstrip()}\n\n{addition}".lstrip() if result else addition
    if len(result) > 100000:
        raise MemoryError("PROFILE_TOO_LARGE", "Profile exceeds the supported size.")
    return result
