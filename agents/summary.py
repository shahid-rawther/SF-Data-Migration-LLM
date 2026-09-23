"""Human-readable summaries of what the agent did or flagged.

The migration runner emits these at the moment wiring happens so a person sees
what changed before extraction starts, rather than hunting through a journal.
"""
from __future__ import annotations

from collections import defaultdict

from agents import wiring

# How each resolution kind reads in a one-line summary. Singular, plural.
_KIND_LABELS: dict[str, tuple[str, str]] = {
    wiring.KIND_LOOKUP_REFERENCE: ("lookup field", "lookup fields"),
    wiring.KIND_DEFERRED_LOOKUP: ("lookup", "lookups"),
    wiring.KIND_CONSTANT_REFERENCE: ("constant reference", "constant references"),
    wiring.KIND_VALUE_MAP: ("value map", "value maps"),
    wiring.KIND_TYPE_COERCION: ("type fix", "type fixes"),
    wiring.KIND_SELF_REFERENCE_GUARD: ("self-reference", "self-references"),
    wiring.KIND_EXTERNAL_ID_UPSERT: ("external-id upsert", "external-id upserts"),
}

# Past-tense verb for each kind when it appears in a sentence.
_KIND_VERBS: dict[str, str] = {
    wiring.KIND_LOOKUP_REFERENCE: "wired",
    wiring.KIND_DEFERRED_LOOKUP: "deferred",
    wiring.KIND_CONSTANT_REFERENCE: "set",
    wiring.KIND_VALUE_MAP: "added",
    wiring.KIND_TYPE_COERCION: "fixed",
    wiring.KIND_SELF_REFERENCE_GUARD: "guarded",
    wiring.KIND_EXTERNAL_ID_UPSERT: "switched",
}


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _mapping_phrase(mapping: str, kind_counts: dict[str, int]) -> str:
    """One mapping's worth: 'wired 2 lookup fields on Account'."""
    parts: list[str] = []
    for kind, count in sorted(kind_counts.items()):
        labels = _KIND_LABELS.get(kind)
        verb = _KIND_VERBS.get(kind, "changed")
        if not labels:
            noun = _plural(count, kind.replace("_", " "), kind.replace("_", " ") + "s")
            parts.append(f"{verb} {count} {noun}")
            continue
        noun = _plural(count, labels[0], labels[1])
        parts.append(f"{verb} {count} {noun}")
    if not parts:
        return ""
    joined = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0])
    return f"{joined} on {mapping}"


def summarize_wiring(changes: list[dict]) -> str:
    """One sentence for every config change the agent applied before extract.

    ``load_order`` priority tweaks are omitted — they are housekeeping, not the
    kind of thing a person needs to be told about at wiring time.
    """
    by_mapping: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for change in changes:
        if not change.get("applied"):
            continue
        kind = change.get("kind") or ""
        if kind in (wiring.KIND_NOTICE, "load_order"):
            continue
        mapping = change.get("mapping") or change.get("mapping_name") or ""
        if mapping:
            by_mapping[mapping][kind] += 1

    phrases = [
        _mapping_phrase(mapping, dict(kinds))
        for mapping, kinds in sorted(by_mapping.items())
        if _mapping_phrase(mapping, dict(kinds))
    ]
    if not phrases:
        return ""

    # Each mapping's clause already contains "and", so they are separated with
    # semicolons rather than chaining a second level of "and".
    return f"Agent {'; '.join(phrases)} before extraction started."


def summarize_notices(notices: list[dict]) -> str:
    """One sentence when the agent flagged concerns it could not fix itself."""
    actionable = [
        n
        for n in notices
        if n.get("kind") == wiring.KIND_NOTICE
        and (n.get("severity") or "info") != "info"
    ]
    if not actionable:
        actionable = [n for n in notices if n.get("kind") == wiring.KIND_NOTICE]
    if not actionable:
        return ""

    count = len(actionable)
    noun = "concern" if count == 1 else "concerns"
    verb = "needs" if count == 1 else "need"
    return (
        f"Agent flagged {count} {noun} that {verb} your decision — "
        "see what to do below."
    )
