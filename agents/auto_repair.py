"""Deterministic auto-repair for common Salesforce load errors.

When the destination rejects rows for a reason that has exactly one correct fix,
this module applies it mechanically — no LLM call required.  Every fix is
journalled through :mod:`agents.audit` so it appears under "What the agent
reported" in the session, and the rationale explains which error it addressed.

The LLM repair agent is still invoked for anything this module does not cover.
A deterministic rule is cheaper, faster, and reproducible, so it handles the
cases where the error message is unambiguous:

* **INVALID_CROSS_REFERENCE_KEY** on a field that copies a raw source id into a
  lookup → wire ``generic_lookup`` against the id_mappings table the parent
  mapping already populated.

* **FIELD_CUSTOM_VALIDATION_EXCEPTION** mentioning a character-length rule on a
  field that has no length guard → wire ``generic_length_guard`` with the limits
  extracted from the error message.

* **STRING_TOO_LONG** on a field whose current transform does not truncate → wire
  ``generic_length_guard`` at the field's declared max length.

* **UNKNOWN_EXCEPTION** with a date-dependency message ("X must be after Y") →
  wire ``generic_rules`` on the dependent date field so it is cleared whenever
  its prerequisite date field is empty.

* **FIELD_CUSTOM_VALIDATION_EXCEPTION** / **REQUIRED_FIELD_MISSING** for a
  conditionally required field that is not mapped → add the mapping from the
  best-matching source field (with a fallback default), or a constant when no
  source match is found.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field as dataclass_field

from common.logger import logger
from agents import audit, wiring
from agents.audit import ChangeRecord, TARGET_FIELD
from agents.context import MigrationContext
from agents.error_digest import ErrorDigest, ErrorEntry
from services.transforms.generic import LENGTH_GUARD_PAD


AGENT_AUTO_REPAIR = "auto_repair"

# How far a length guard may be tightened inside a rule's stated limits before
# the failure is left to the model: past this the limits are not the cause.
_MAX_LIMIT_MARGIN = 3

# ── pattern matchers ───────────────────────────────────────────────────────────

# Matches "Min character limit is 30 and Max character limit is 255"
_LENGTH_RULE = re.compile(
    r"Min character limit is (\d+) and Max character limit is (\d+)",
    re.IGNORECASE,
)

# Matches Salesforce STRING_TOO_LONG messages that include the max length
_STRING_TOO_LONG = re.compile(
    r"value too long.*?max length[=:\s]+(\d+)",
    re.IGNORECASE,
)

# "Resolution Date must be after Start Date" — date-ordering dependency
_DATE_DEPENDENCY = re.compile(
    r"([\w\s]+?)\s+must be\s+(after|before)\s+([\w\s]+)",
    re.IGNORECASE,
)

# "Please enter a Cancelled Reason before saving" — conditionally required
_CONDITIONAL_REQUIRED = re.compile(
    r"(?:Please enter|is required|must not be (?:blank|empty|null)|"
    r"required field|cannot be (?:blank|empty))",
    re.IGNORECASE,
)

# Synonyms used when searching for source fields that match a destination name.
# Each token expands to a set of alternatives, so "Cancelled_Reason" finds
# source fields named "close_code" or "cancel_reason".
_FIELD_SYNONYMS: dict[str, set[str]] = {
    "cancel": {"close", "cancel", "cancelled"},
    "cancelled": {"close", "cancel", "cancelled"},
    "close": {"close", "cancel", "cancelled"},
    "reason": {"reason", "code", "cause"},
    "code": {"code", "reason", "category"},
    "description": {"description", "notes", "details", "summary"},
    "notes": {"notes", "description", "details", "comments"},
    "resolution": {"resolution", "close", "resolve", "resolved"},
    "start": {"start", "open", "begin", "created"},
    "end": {"end", "close", "finish", "resolved"},
    "date": {"date", "time", "datetime"},
}


@dataclass
class AutoRepairResult:
    """What the deterministic pass changed (if anything)."""

    changes: list[ChangeRecord] = dataclass_field(default_factory=list)
    change_ids: list[int] = dataclass_field(default_factory=list)
    notices: list[ChangeRecord] = dataclass_field(default_factory=list)
    handled_entries: list[ErrorEntry] = dataclass_field(default_factory=list)
    applied: bool = False

    @property
    def fully_resolved(self) -> bool:
        """True when every error entry was handled by a deterministic rule."""
        return self.applied and len(self.handled_entries) > 0


def auto_repair(
    context: MigrationContext,
    mapping_name: str,
    digest: ErrorDigest,
    session_id: str = "",
    run_id: str = "",
    apply: bool = True,
) -> AutoRepairResult:
    """Try to fix every error entry with a deterministic rule.

    Returns an :class:`AutoRepairResult` whose ``handled_entries`` lists the
    digest entries that were resolved.  Entries with no known rule are left for
    the LLM repair agent.
    """
    result = AutoRepairResult()
    config = context.configs.get(mapping_name)
    if not config:
        return result

    draft = context.draft()
    draft_config = draft.get(mapping_name)
    if not draft_config:
        return result

    object_to_mapping = {
        context.destination_object(n): n
        for n in context.mapping_names
        if context.destination_object(n)
    }

    destination_object = context.destination_object(mapping_name)
    destination_fields = context.destination_fields(destination_object) if destination_object else []
    meta_by_name = {f["name"]: f for f in destination_fields}

    # When the schema is unavailable (e.g. destination unreachable at startup),
    # fall back to field names from the mapping config so that error-message
    # parsing can still match "Subject field Min character limit..." to Subject.
    if not meta_by_name:
        for field_cfg in config.get("fields") or []:
            dest = field_cfg.get("destination_field", "")
            if dest and dest not in meta_by_name:
                meta_by_name[dest] = {"name": dest, "type": "string"}

    all_changes: list[ChangeRecord] = []

    for entry in digest.entries:
        repair_changes = _try_repair_entry(
            entry=entry,
            mapping_name=mapping_name,
            draft_config=draft_config,
            meta_by_name=meta_by_name,
            object_to_mapping=object_to_mapping,
            context=context,
        )
        if repair_changes is not None:
            all_changes.extend(repair_changes)
            result.handled_entries.append(entry)

    if all_changes and apply:
        touched = sorted({c.mapping_name for c in all_changes})
        wiring.persist(draft, touched)
        # The repair agent runs next on this same context and persists whole
        # configs from its own draft; left stale, it would erase these fixes.
        for name in touched:
            context.configs[name] = copy.deepcopy(draft[name])
        result.change_ids = audit.record_changes(
            run_id, all_changes, session_id=session_id,
        )
        result.applied = True
        result.changes = all_changes
        logger.info(
            "[auto_repair] deterministic pass fixed %d field(s) on %s "
            "for %d error pattern(s)",
            len(all_changes),
            ", ".join(touched),
            len(result.handled_entries),
        )
    elif all_changes:
        result.changes = all_changes

    return result


# ── per-entry dispatch ─────────────────────────────────────────────────────────


def _try_repair_entry(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
    object_to_mapping: dict[str, str],
    context: MigrationContext,
) -> list[ChangeRecord] | None:
    """Return changes for one error entry, or None if no rule matches."""

    code = entry.code

    if code == "INVALID_CROSS_REFERENCE_KEY":
        return _fix_cross_reference(
            entry, mapping_name, draft_config, meta_by_name,
            object_to_mapping, context,
        )

    if code == "FIELD_CUSTOM_VALIDATION_EXCEPTION":
        result = _fix_validation_exception(
            entry, mapping_name, draft_config, meta_by_name,
        )
        if result is not None:
            return result
        return _fix_conditional_required(
            entry, mapping_name, draft_config, meta_by_name, context,
        )

    if code == "STRING_TOO_LONG":
        return _fix_string_too_long(
            entry, mapping_name, draft_config, meta_by_name,
        )

    if code == "UNKNOWN_EXCEPTION":
        return _fix_date_dependency(
            entry, mapping_name, draft_config, meta_by_name, context,
        )

    if code == "REQUIRED_FIELD_MISSING":
        return _fix_conditional_required(
            entry, mapping_name, draft_config, meta_by_name, context,
        )

    return None


# ── INVALID_CROSS_REFERENCE_KEY ────────────────────────────────────────────────


def _fix_cross_reference(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
    object_to_mapping: dict[str, str],
    context: MigrationContext,
) -> list[ChangeRecord] | None:
    """Wire generic_lookup on fields that send raw source ids to a lookup."""
    changes: list[ChangeRecord] = []

    # When the error doesn't name specific fields, inspect all mapped fields
    # that target a reference without a lookup transform.
    suspect_fields = entry.fields or _reference_fields_without_lookup(
        draft_config, meta_by_name,
    )

    for field_name in suspect_fields:
        field_entry = wiring.field_entry(draft_config, field_name)
        if field_entry is None:
            continue

        current_transform = field_entry.get("transformation_function") or ""
        if "lookup" in current_transform.lower():
            continue

        meta = meta_by_name.get(field_name)
        if not meta:
            continue

        # Find the id_mapping table to resolve against.
        lookup_tables = _find_lookup_tables(
            meta, object_to_mapping, context, mapping_name,
        )
        if not lookup_tables:
            continue

        previous = copy.deepcopy(field_entry)

        field_entry["transformation_function"] = "generic_lookup"
        field_entry["transformation_params"] = {
            "lookup_tables": lookup_tables,
            "default": "",
        }
        field_entry.pop("constant_value", None)
        field_entry.pop("use_constant", None)

        changes.append(ChangeRecord(
            agent=AGENT_AUTO_REPAIR,
            kind="type_coercion",
            mapping_name=mapping_name,
            field_name=field_name,
            target=TARGET_FIELD,
            previous_value=previous,
            new_value=copy.deepcopy(field_entry),
            rationale=(
                f"INVALID_CROSS_REFERENCE_KEY: '{field_name}' was sending a raw "
                f"source id into a lookup field. Wired generic_lookup against "
                f"{lookup_tables} so ids are resolved from the migrated records."
            ),
            severity="warning",
        ))

    return changes if changes else None


def _reference_fields_without_lookup(
    config: dict,
    meta_by_name: dict[str, dict],
) -> list[str]:
    """Fields mapped to a reference destination without a lookup transform."""
    suspects: list[str] = []
    for field_cfg in config.get("fields") or []:
        dest = field_cfg.get("destination_field", "")
        meta = meta_by_name.get(dest)
        if not meta or meta.get("type") != "reference":
            continue
        transform = (field_cfg.get("transformation_function") or "").lower()
        if "lookup" not in transform and not field_cfg.get("constant_value"):
            suspects.append(dest)
    return suspects


def _find_lookup_tables(
    meta: dict,
    object_to_mapping: dict[str, str],
    context: MigrationContext,
    current_mapping: str,
) -> list[str]:
    """Derive the id_mapping table name(s) a lookup field should resolve against."""
    targets = meta.get("reference_to") or []
    tables: list[str] = []
    for target_object in targets:
        parent_mapping = object_to_mapping.get(target_object)
        if parent_mapping:
            parent_config = context.configs.get(parent_mapping) or {}
            table = wiring.lookup_table_name(parent_config)
            if table and table not in tables:
                tables.append(table)
        # Self-reference: the mapping's own table
        if target_object == context.destination_object(current_mapping):
            own_config = context.configs.get(current_mapping) or {}
            table = wiring.lookup_table_name(own_config)
            if table and table not in tables:
                tables.append(table)
    return tables


# ── FIELD_CUSTOM_VALIDATION_EXCEPTION ──────────────────────────────────────────


def _fix_validation_exception(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
) -> list[ChangeRecord] | None:
    """Extract min/max length from the error message and wire generic_length_guard."""
    message = entry.message
    changes: list[ChangeRecord] = []

    # Find all field-specific length rules in the message.  The Salesforce
    # validation rule typically names the field:
    #   "Subject field Min character limit is 30 and Max character limit is 255."
    for match in _LENGTH_RULE.finditer(message):
        min_len = int(match.group(1))
        max_len = int(match.group(2))

        # Walk backwards from the match to find the field name
        prefix = message[:match.start()].strip()
        field_name = _extract_field_name_from_prefix(prefix, meta_by_name)

        if not field_name:
            # Try the error's named fields
            for candidate in entry.fields:
                if candidate in meta_by_name:
                    field_name = candidate
                    break

        if not field_name:
            continue

        field_entry = wiring.field_entry(draft_config, field_name)
        if field_entry is None:
            continue

        current_transform = field_entry.get("transformation_function") or ""
        current_params = field_entry.get("transformation_params") or {}

        default_text = f"No {field_name.lower()} was provided in the source record."
        # If already has a length guard, update its limits rather than replacing
        if current_transform == "generic_length_guard":
            old_max = int(current_params.get("max_length", 0) or 0)
            old_min = int(current_params.get("min_length", 0) or 0)
            old_margin = int(current_params.get("limit_margin", 0) or 0)
            previous = copy.deepcopy(field_entry)
            current_params = copy.deepcopy(current_params)
            if old_max == max_len and old_min == min_len:
                # The guard already enforces these limits and rows still fail,
                # so the rule is stricter than its message: tighten each retry.
                if old_margin >= _MAX_LIMIT_MARGIN:
                    continue
                current_params["limit_margin"] = old_margin + 1
                note = (
                    f"Rows still failed with these limits already enforced, so the "
                    f"rule excludes its boundary; now staying "
                    f"{current_params['limit_margin']} character(s) inside both limits."
                )
            else:
                current_params["min_length"] = min_len
                current_params["max_length"] = max_len
                note = "Updated the length guard to these limits."
            current_params.setdefault("pad_with", LENGTH_GUARD_PAD)
            current_params.setdefault("default", default_text)
            field_entry["transformation_params"] = current_params
        else:
            previous = copy.deepcopy(field_entry)
            field_entry["transformation_function"] = "generic_length_guard"
            field_entry["transformation_params"] = {
                "min_length": min_len,
                "max_length": max_len,
                "limit_margin": 1,
                "pad_with": LENGTH_GUARD_PAD,
                "default": default_text,
                "truncation_suffix": "...",
            }
            note = (
                "Wired generic_length_guard to enforce the limits before loading, "
                "padding short values and filling empty ones."
            )

        changes.append(ChangeRecord(
            agent=AGENT_AUTO_REPAIR,
            kind="type_coercion",
            mapping_name=mapping_name,
            field_name=field_name,
            target=TARGET_FIELD,
            previous_value=previous,
            new_value=copy.deepcopy(field_entry),
            rationale=(
                f"FIELD_CUSTOM_VALIDATION_EXCEPTION: Salesforce validation requires "
                f"'{field_name}' to be {min_len}–{max_len} characters. {note}"
            ),
            severity="warning",
        ))

    return changes if changes else None


def _extract_field_name_from_prefix(
    prefix: str,
    meta_by_name: dict[str, dict],
) -> str:
    """Try to find a field name at the end of the text before the length rule.

    Typical patterns:
        "Subject field Min character..."
        "Description field Min character..."
    """
    # Try the last word(s) before "field"
    words = prefix.rstrip(". ").split()
    if not words:
        return ""

    # Pattern: "FieldName field"
    if len(words) >= 2 and words[-1].lower() == "field":
        candidate = words[-2]
        if candidate in meta_by_name:
            return candidate

    # Pattern: "FieldName"
    candidate = words[-1].rstrip(":")
    if candidate in meta_by_name:
        return candidate

    # Try matching any known field name in the prefix
    for field_name in meta_by_name:
        if field_name in prefix:
            return field_name

    return ""


# ── STRING_TOO_LONG ───────────────────────────────────────────────────────────


def _fix_string_too_long(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
) -> list[ChangeRecord] | None:
    """Wire generic_length_guard at the field's declared max length."""
    changes: list[ChangeRecord] = []

    # Try to get max from the error message itself
    match = _STRING_TOO_LONG.search(entry.message)
    error_max = int(match.group(1)) if match else 0

    for field_name in entry.fields:
        field_cfg = wiring.field_entry(draft_config, field_name)
        if field_cfg is None:
            continue

        current_transform = field_cfg.get("transformation_function") or ""
        if current_transform in ("generic_length_guard", "generic_truncate"):
            continue

        meta = meta_by_name.get(field_name, {})
        max_length = error_max or int(meta.get("length", 0) or 0)
        if not max_length:
            continue

        previous = copy.deepcopy(field_cfg)
        field_cfg["transformation_function"] = "generic_length_guard"
        field_cfg["transformation_params"] = {
            **(field_cfg.get("transformation_params") or {}),
            "max_length": max_length,
            "truncation_suffix": "...",
        }

        changes.append(ChangeRecord(
            agent=AGENT_AUTO_REPAIR,
            kind="type_coercion",
            mapping_name=mapping_name,
            field_name=field_name,
            target=TARGET_FIELD,
            previous_value=previous,
            new_value=copy.deepcopy(field_cfg),
            rationale=(
                f"STRING_TOO_LONG: '{field_name}' exceeded the destination's "
                f"{max_length}-character limit. Wired generic_length_guard to "
                f"truncate before loading."
            ),
            severity="warning",
        ))

    return changes if changes else None


# ── UNKNOWN_EXCEPTION — date dependency ────────────────────────────────────────


def _normalize_name(text: str) -> str:
    text = text.lower()
    if text.endswith(("__c", "__r")):
        text = text[:-3]
    return re.sub(r"[^a-z0-9]", "", text)


def _resolve_field_name(
    label: str,
    meta_by_name: dict[str, dict],
    hint_fields: list[str],
    mapped: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Resolve a human-readable label (e.g. "Start Date") to an API field name.

    Tries, in order: exact API-name match, hints from the error entry, exact
    label or normalised name, then partial matches. A partial match is ranked
    rather than taken first-come: "Start Date" is contained in both
    ``StartDateTime`` and ``SlaStartDate``, and only a field the mapping
    writes can be the one a validation rule is complaining about. Mapped
    fields win, then names that begin with the label, then the closest length.
    """
    if label in meta_by_name:
        return label

    for field_name in hint_fields:
        if field_name in meta_by_name:
            return field_name

    wanted = _normalize_name(label)
    if not wanted:
        return ""

    def candidates(name: str, meta: dict) -> list[str]:
        return [n for n in (_normalize_name(name), _normalize_name(meta.get("label") or "")) if n]

    exact = [
        name for name, meta in meta_by_name.items()
        if wanted in candidates(name, meta)
    ]
    if exact:
        return sorted(exact, key=lambda name: name not in mapped)[0]

    ranked: list[tuple[tuple, str]] = []
    for name, meta in meta_by_name.items():
        for norm in candidates(name, meta):
            if wanted in norm or norm in wanted:
                score = (
                    name not in mapped,
                    not norm.startswith(wanted),
                    abs(len(norm) - len(wanted)),
                )
                ranked.append((score, name))
    return min(ranked)[1] if ranked else ""


def _mapped_fields(config: dict) -> set[str]:
    return {
        entry.get("destination_field")
        for entry in config.get("fields") or []
        if entry.get("destination_field")
    }


def _fix_date_dependency(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
    context: MigrationContext,
) -> list[ChangeRecord] | None:
    """Wire ``generic_rules`` so a dependent date field is blank when its prerequisite is empty.

    Handles errors like "Resolution Date must be after Start Date" where the
    prerequisite (Start Date) is sometimes empty in the source data.  The fix
    makes the dependent field conditional: only populated when the prerequisite
    source field has a value, preventing the validation error without discarding
    the entire row.
    """
    match = _DATE_DEPENDENCY.search(entry.message)
    if not match:
        return None

    dependent_label = match.group(1).strip()
    relation = match.group(2).strip().lower()
    prerequisite_label = match.group(3).strip()

    mapped = _mapped_fields(draft_config)
    dependent_field = _resolve_field_name(dependent_label, meta_by_name, entry.fields, mapped)
    prerequisite_field = _resolve_field_name(prerequisite_label, meta_by_name, [], mapped)

    if not dependent_field or not prerequisite_field:
        return None

    dependent_entry = wiring.field_entry(draft_config, dependent_field)
    prerequisite_entry = wiring.field_entry(draft_config, prerequisite_field)

    if dependent_entry is None:
        return None

    dependent_source = (dependent_entry.get("source_field") or "").strip()
    prerequisite_source = (
        (prerequisite_entry.get("source_field") or "").strip()
        if prerequisite_entry else ""
    )

    if not dependent_source or not prerequisite_source:
        return None

    if dependent_entry.get("transformation_function") == "generic_rules":
        return None

    previous = copy.deepcopy(dependent_entry)

    source_fields = [s.strip() for s in dependent_source.split(",")]
    prereq_sources = [s.strip() for s in prerequisite_source.split(",")]
    for ps in prereq_sources:
        if ps not in source_fields:
            source_fields.append(ps)

    dependent_entry["source_field"] = ",".join(source_fields)
    dependent_entry["transformation_function"] = "generic_rules"
    dependent_entry["transformation_params"] = {
        "inputs": source_fields,
        "rules": [
            {
                "all": [
                    {"field": ps, "op": "is_not_empty"}
                    for ps in prereq_sources
                ],
                "then": {
                    "transform": "generic_default_if_empty",
                    "from": source_fields[0],
                    "params": {"default": ""},
                },
            },
        ],
        "otherwise": {"value": ""},
    }

    return [ChangeRecord(
        agent=AGENT_AUTO_REPAIR,
        kind="type_coercion",
        mapping_name=mapping_name,
        field_name=dependent_field,
        target=TARGET_FIELD,
        previous_value=previous,
        new_value=copy.deepcopy(dependent_entry),
        rationale=(
            f"UNKNOWN_EXCEPTION: '{dependent_field}' must be {relation} "
            f"'{prerequisite_field}', but the prerequisite source "
            f"({prerequisite_source}) is sometimes empty. Wired generic_rules "
            f"to clear '{dependent_field}' when the prerequisite is blank, "
            f"preventing the date-dependency validation error."
        ),
        severity="warning",
    )]


# ── Conditional required / REQUIRED_FIELD_MISSING ─────────────────────────────


def _find_best_source_field(
    destination_field: str,
    source_fields: list[dict],
    config: dict,
) -> str:
    """Try to find a source field that plausibly maps to a destination field.

    Tokenises the destination name, expands tokens via :data:`_FIELD_SYNONYMS`,
    then scores each unused source field by overlap.
    """
    if not source_fields:
        return ""

    used_sources = {
        (f.get("source_field") or "").split(".")[-1]
        for f in config.get("fields") or []
    }

    dest_stripped = re.sub(r"__[cr]$", "", destination_field)
    dest_tokens = {t.lower() for t in re.split(r"[_\s]+", dest_stripped) if t}

    expanded = set(dest_tokens)
    for token in dest_tokens:
        if token in _FIELD_SYNONYMS:
            expanded.update(_FIELD_SYNONYMS[token])

    # A column can feed several destination fields, so one already mapped
    # elsewhere is only passed over when another matches equally well.
    best_score: tuple[int, bool] = (0, False)
    best_field = ""

    for sf in source_fields:
        # Some providers already qualify names with the table ("incident.close_code").
        name = (sf.get("name") or "").split(".")[-1]
        if not name:
            continue
        src_tokens = {t.lower() for t in re.split(r"[_\s]+", name) if t}
        score = (len(expanded & src_tokens), name not in used_sources)
        if score > best_score:
            best_score = score
            best_field = name

    return best_field if best_score[0] >= 1 else ""


def _fix_conditional_required(
    entry: ErrorEntry,
    mapping_name: str,
    draft_config: dict,
    meta_by_name: dict[str, dict],
    context: MigrationContext,
) -> list[ChangeRecord] | None:
    """Add or wire a mapping for a field that a validation rule makes mandatory.

    Handles two scenarios:

    1. The field is **not mapped at all** → find the best source field via name
       heuristics and add a ``generic_default_if_empty`` mapping (or a constant
       fallback when no source match is found).
    2. The field **is mapped** but sometimes produces empty values → wrap it with
       ``generic_default_if_empty`` so a default fills the gap.

    Skips entries whose message matches the length-rule pattern (already handled
    by :func:`_fix_validation_exception`).
    """
    if _LENGTH_RULE.search(entry.message):
        return None

    if not entry.fields and not _CONDITIONAL_REQUIRED.search(entry.message):
        return None

    target_fields = list(entry.fields)
    if not target_fields:
        match = _CONDITIONAL_REQUIRED.search(entry.message)
        if match:
            remainder = entry.message[match.end():].strip().rstrip(".")
            label = remainder.split(":")[0].strip() if remainder else ""
            if label:
                resolved = _resolve_field_name(label, meta_by_name, [])
                if resolved:
                    target_fields = [resolved]

    if not target_fields:
        return None

    changes: list[ChangeRecord] = []
    driving_table = context.driving_table(mapping_name)
    source_fields = context.source_fields(driving_table) if driving_table else []

    for field_name in target_fields:
        existing = wiring.field_entry(draft_config, field_name)

        if existing is None:
            source_field = _find_best_source_field(
                field_name, source_fields, draft_config,
            )

            new_entry: dict = {"destination_field": field_name}

            if source_field:
                qualified = (
                    f"{driving_table}.{source_field}"
                    if driving_table else source_field
                )
                new_entry["source_field"] = qualified
                new_entry["transformation_function"] = "generic_default_if_empty"
                new_entry["transformation_params"] = {"default": "Not Specified"}
                rationale = (
                    f"{entry.code}: '{field_name}' is required by a validation "
                    f"rule but was not mapped. Added mapping from source field "
                    f"'{source_field}' with a fallback default."
                )
            else:
                new_entry["source_field"] = ""
                new_entry["constant_value"] = "Not Specified"
                new_entry["use_constant"] = True
                rationale = (
                    f"{entry.code}: '{field_name}' is required by a validation "
                    f"rule but was not mapped and no matching source field was "
                    f"found. Added constant default 'Not Specified' — review "
                    f"and map a source field if one is available."
                )

            draft_config.setdefault("fields", []).append(new_entry)

            changes.append(ChangeRecord(
                agent=AGENT_AUTO_REPAIR,
                kind="type_coercion",
                mapping_name=mapping_name,
                field_name=field_name,
                target=TARGET_FIELD,
                previous_value=None,
                new_value=copy.deepcopy(new_entry),
                rationale=rationale,
                severity="warning",
            ))
        else:
            current_transform = existing.get("transformation_function") or ""
            if current_transform == "generic_default_if_empty":
                if (existing.get("transformation_params") or {}).get("default"):
                    continue
            elif current_transform:
                continue

            previous = copy.deepcopy(existing)
            existing["transformation_function"] = "generic_default_if_empty"
            existing["transformation_params"] = {
                **(existing.get("transformation_params") or {}),
                "default": "Not Specified",
            }

            changes.append(ChangeRecord(
                agent=AGENT_AUTO_REPAIR,
                kind="type_coercion",
                mapping_name=mapping_name,
                field_name=field_name,
                target=TARGET_FIELD,
                previous_value=previous,
                new_value=copy.deepcopy(existing),
                rationale=(
                    f"{entry.code}: '{field_name}' is required by a validation "
                    f"rule but sometimes empty. Wired generic_default_if_empty "
                    f"with fallback 'Not Specified'."
                ),
                severity="warning",
            ))

    return changes if changes else None
