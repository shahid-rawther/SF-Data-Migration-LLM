"""The second pass that attaches references the first pass could not resolve.

A circular reference has no parent-first order. If an account points at a user
and that user points back at an account, one of them has to be created first,
and whichever it is cannot fill its lookup: the object on the other end has no
destination ids yet, so there is nothing to point at. A self-reference has the
same shape within one object — a parent row may simply not have been created when
its child is.

The pipeline's answer is to load the record anyway with that one field empty, and
come back for it. This module owns the coming back:

* the transform records every row whose deferred lookup resolved empty, into a
  ``deferred.csv`` beside the batch it belongs to;
* :func:`scan` reads those records, re-checks each one against the id map as it
  now stands, and reports what has become resolvable;
* :func:`execute` runs the fill as an ordinary update mapping — the same
  transform and load the pipeline already uses for ``operation: "update"``,
  narrowed to one field.

Nothing here runs by itself. :func:`scan` only reports, and :func:`execute`
refuses a unit nobody has approved, because a backfill issues updates against
records that already exist in the destination. That is a decision to put to a
person, not an inference to make on their behalf.

One unit per field rather than per mapping, which is what makes the fill
trustworthy: the transform skips a row whose lookup is still unresolvable, and
that decision has to be per field. Two deferred fields sharing one update pass
would mean a row that could fill only one of them fills neither.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path

from common.logger import logger
from config.settings import PATH_TO_TRANSFORMATION, SERVICE_NOW_ID_COLUMN
from services.external.db_client import db_client
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
    get_id_mapping_table_name,
)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

REASON_CYCLE = "dependency_cycle"
REASON_SELF_REFERENCE = "self_reference"
# Not a cycle at all: the object this field points at simply has not been
# migrated. The string matches what the mapping editor writes on the field, so a
# deferral wired in the UI arrives here already carrying its reason.
REASON_PARENT_PENDING = "parent_not_migrated"

DEFERRED_FILENAME = "deferred.csv"

# Beyond this, the update pass reads the whole staged table and lets the
# per-field skip drop what is still unresolvable, rather than naming every id in
# a WHERE clause.
MAX_IDS_IN_FILTER = 4000

_COLUMNS = (
    "id",
    "session_id",
    "mapping_name",
    "destination_object",
    "source_table",
    "field",
    "reason",
    "awaiting_object",
    "rows_deferred",
    "rows_resolvable",
    "status",
    "records_updated",
    "records_failed",
    "detail",
    "created_at",
    "decided_at",
    "completed_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeferredField:
    """One mapping field that was left empty, and what it is waiting on."""

    mapping_name: str
    destination_object: str
    source_table: str
    field: str
    reason: str = ""
    # The destination object whose ids are being waited for.
    awaiting_object: str = ""
    # ``(record id, referenced id)`` for every row whose reference did not
    # resolve on the first pass.
    deferred_rows: list[tuple[str, str]] = dataclass_field(default_factory=list)
    # The id-map tables the field resolves through, i.e. where the parent's
    # destination ids will appear once the parent has loaded.
    lookup_tables: list[str] = dataclass_field(default_factory=list)

    @property
    def record_ids(self) -> list[str]:
        """The rows to revisit, in first-seen order and without repeats."""
        return list(dict.fromkeys(record_id for record_id, _ in self.deferred_rows))

    @property
    def rows_deferred(self) -> int:
        return len(self.record_ids)


def deferred_fields(config: dict) -> list[dict]:
    """The fields in one mapping config marked for the backfill pass."""
    return [
        f
        for f in config.get("fields", [])
        if f.get("defer_to_backfill") and f.get("destination_field")
    ]


def _batch_dirs(config: dict) -> list[Path]:
    """The per-batch transform output directories for one mapping."""
    name = config.get("transformation_name") or cfg_destination_object(config)
    root = PATH_TO_TRANSFORMATION / "csv" / name
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def _read_deferred(config: dict) -> dict[str, list[tuple[str, str]]]:
    """``destination_field -> (record id, referenced id)`` across the batches.

    Both halves are needed and they are not interchangeable: the record id says
    which row to revisit, while the referenced id is what has to appear in the
    parent's id map before that row can be filled. They are kept paired so the
    count offered to a person is a number of records rather than a number of
    distinct references, which differ whenever rows share a parent.
    """
    import csv

    per_field: dict[str, list[tuple[str, str]]] = {}
    for batch in _batch_dirs(config):
        record = batch / DEFERRED_FILENAME
        if not record.exists():
            continue
        try:
            with open(record, newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    field_name = (row.get("destination_field") or "").strip()
                    record_id = (row.get(SERVICE_NOW_ID_COLUMN) or "").strip()
                    referenced = (row.get("source_value") or "").strip()
                    if field_name and record_id:
                        per_field.setdefault(field_name, []).append((record_id, referenced))
        except OSError as exc:
            logger.warning("[backfill] could not read %s: %s", record, exc)
    return per_field


def _field_entry(config: dict, field_name: str) -> dict:
    """The mapping's entry for one deferred field, or an empty dict."""
    return next(
        (f for f in deferred_fields(config) if f["destination_field"] == field_name),
        None,
    ) or {}


def _awaited_object(entry: dict) -> str:
    """The destination object a deferred field is waiting on."""
    return str(entry.get("defer_object") or "").strip()


def _lookup_tables(entry: dict, awaiting_object: str = "") -> list[str]:
    """Every id-map table this field's reference could resolve through.

    The table written on the field at wiring time is kept, but it cannot be the
    only source. When the deferral exists *because* nothing has migrated the
    parent yet, there was no table to write down — the name is derived from the
    parent mapping's own driving table and transformation name, which do not
    exist until that mapping does. Resolving the object again on every check is
    what lets the wait end by itself: the moment something migrates the object,
    the table appears here without the unit having to be rewired.
    """
    tables = [
        t for t in ((entry.get("transformation_params") or {}).get("lookup_tables") or []) if t
    ]
    target = awaiting_object or _awaited_object(entry)
    if target:
        from services.internal.mappings_service import lookup_targets_for_object

        try:
            for candidate in lookup_targets_for_object(target):
                if candidate["lookup_table"] not in tables:
                    tables.append(candidate["lookup_table"])
        except Exception as exc:  # noqa: BLE001 — a bad catalog must not break the scan
            logger.warning("[backfill] could not resolve id maps for %s: %s", target, exc)
    return tables


def _known_ids(referenced: set[str], lookup_tables: list[str]) -> set[str]:
    """Which of these referenced ids the parent's id map already holds."""
    known: set[str] = set()
    unique = list(referenced)
    # Chunked to stay well inside SQLite's variable limit on a wide selection.
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        table_slots = ",".join("?" * len(lookup_tables))
        id_slots = ",".join("?" * len(chunk))
        rows = db_client.fetch_all(
            f"SELECT DISTINCT servicenow_id FROM id_mappings "
            f"WHERE table_name IN ({table_slots}) AND servicenow_id IN ({id_slots})",
            (*lookup_tables, *chunk),
        )
        known.update(str(row[0]) for row in rows)
    return known


def _resolvable(deferred_rows: list[tuple[str, str]], lookup_tables: list[str]) -> int:
    """How many deferred records could be filled right now.

    Counted against the referenced ids, not the records' own, because it is the
    parent having loaded that makes a row fillable. This is what decides whether
    a backfill is worth offering and it changes every time another object loads,
    so it is always recomputed rather than stored.
    """
    if not deferred_rows or not lookup_tables:
        return 0
    referenced = {ref for _, ref in deferred_rows if ref}
    if not referenced:
        return 0
    known = _known_ids(referenced, lookup_tables)
    return len({record_id for record_id, ref in deferred_rows if ref in known})


def scan(session_id: str, mapping_names: list[str]) -> list[dict]:
    """Record what the first pass left unattached, and how much can now be filled.

    Called once a run's objects have loaded. Units are written as ``pending`` and
    left that way; the resolvable count is refreshed on every scan because
    loading another object is exactly what changes it.
    """
    from services.internal.mappings_service import get_mapping_config

    units: list[dict] = []
    for name in mapping_names:
        config = get_mapping_config(name)
        if not config:
            continue
        marked = deferred_fields(config)
        if not marked:
            continue

        recorded = _read_deferred(config)
        source_tables = cfg_source_tables(config)
        for entry in marked:
            field_name = entry["destination_field"]
            deferred_rows = recorded.get(field_name, [])
            if not deferred_rows:
                # The field was deferred but every row resolved anyway, which is
                # the good case and not something to ask anyone about.
                continue
            awaiting_object = _awaited_object(entry)
            unit = DeferredField(
                mapping_name=name,
                destination_object=cfg_destination_object(config),
                source_table=source_tables[0] if source_tables else "",
                field=field_name,
                reason=entry.get("defer_reason", "") or REASON_CYCLE,
                awaiting_object=awaiting_object,
                deferred_rows=deferred_rows,
                lookup_tables=_lookup_tables(entry, awaiting_object),
            )
            units.append(_upsert(session_id, unit))

    if units:
        logger.info(
            "[backfill] %d unit(s) pending approval for session %s: %s",
            len(units),
            session_id,
            ", ".join(f"{u['mapping_name']}.{u['field']}" for u in units),
        )
    return units


def newly_fillable(exclude_session: str = "") -> list[dict]:
    """Open units that this moment has just made fillable, across all sessions.

    A deferral is usually ended by a *different* run: the records waiting on
    ``User`` become attachable when something migrates users, which is its own
    session with its own objects and no knowledge of what is waiting on it.
    Nothing would otherwise connect the two, and the waiting units would sit at
    zero resolvable until someone happened to reopen the older session.

    Only the zero-to-something crossing is returned. A unit that was already
    partly fillable is not news, and repeating it every run would train people
    to ignore the one time it matters.
    """
    unblocked: list[dict] = []
    for unit in list_units(status=STATUS_PENDING) + list_units(status=STATUS_APPROVED):
        if exclude_session and unit["session_id"] == exclude_session:
            continue
        if unit["rows_resolvable"]:
            continue
        before = unit["rows_resolvable"]
        try:
            after = _refresh(unit)["rows_resolvable"]
        except Exception as exc:  # noqa: BLE001 — never fail a finished run
            logger.warning("[backfill] could not re-check unit %s: %s", unit["id"], exc)
            continue
        if after > before:
            unblocked.append(unit)
    if unblocked:
        logger.info(
            "[backfill] %d earlier deferral(s) became fillable: %s",
            len(unblocked),
            ", ".join(f"{u['mapping_name']}.{u['field']}" for u in unblocked),
        )
    return unblocked


def _upsert(session_id: str, unit: DeferredField) -> dict:
    """Create or refresh one unit, leaving a decision already made intact."""
    resolvable = _resolvable(unit.deferred_rows, unit.lookup_tables)
    existing = db_client.fetch_one(
        f"SELECT {', '.join(_COLUMNS)} FROM backfill_units "
        "WHERE session_id = ? AND mapping_name = ? AND field = ?",
        (session_id, unit.mapping_name, unit.field),
    )
    if existing:
        row = dict(zip(_COLUMNS, existing))
        # Reaching here means the object was transformed again and left this field
        # unattached again, so a fill that already ran has been undone by the new
        # load and the decision has to be put back to the person. An explicit
        # decline is left standing: they said no about this same field, and the
        # panel keeps showing it with its counts either way.
        reopen = row["status"] in (STATUS_COMPLETED, STATUS_FAILED) and unit.rows_deferred > 0
        if reopen:
            db_client.execute(
                "UPDATE backfill_units SET rows_deferred = ?, rows_resolvable = ?, "
                "awaiting_object = ?, status = ?, decided_at = NULL, completed_at = NULL, "
                "records_updated = 0, records_failed = 0, detail = ? WHERE id = ?",
                (
                    unit.rows_deferred,
                    resolvable,
                    unit.awaiting_object,
                    STATUS_PENDING,
                    f"Deferred again by a later run of {unit.destination_object}; "
                    f"the previous fill no longer covers these records.",
                    row["id"],
                ),
            )
            row.update(
                rows_deferred=unit.rows_deferred,
                rows_resolvable=resolvable,
                awaiting_object=unit.awaiting_object,
                status=STATUS_PENDING,
                decided_at=None,
                completed_at=None,
                records_updated=0,
                records_failed=0,
            )
            logger.info(
                "[backfill] %s.%s deferred again — reopening the decision",
                unit.mapping_name,
                unit.field,
            )
            return _shape(row)

        # awaiting_object is refreshed too: a unit recorded before the field
        # named its target still has it blank, and that is what the wait reads.
        db_client.execute(
            "UPDATE backfill_units SET rows_deferred = ?, rows_resolvable = ?, "
            "awaiting_object = ? WHERE id = ?",
            (unit.rows_deferred, resolvable, unit.awaiting_object, row["id"]),
        )
        row.update(
            rows_deferred=unit.rows_deferred,
            rows_resolvable=resolvable,
            awaiting_object=unit.awaiting_object,
        )
        return _shape(row)

    new_id = db_client.insert(
        """
        INSERT INTO backfill_units
            (session_id, mapping_name, destination_object, source_table, field,
             reason, awaiting_object, rows_deferred, rows_resolvable, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            unit.mapping_name,
            unit.destination_object,
            unit.source_table,
            unit.field,
            unit.reason,
            unit.awaiting_object,
            unit.rows_deferred,
            resolvable,
            STATUS_PENDING,
            _now(),
        ),
    )
    return _shape(
        {
            "id": new_id,
            "session_id": session_id,
            "mapping_name": unit.mapping_name,
            "destination_object": unit.destination_object,
            "source_table": unit.source_table,
            "field": unit.field,
            "reason": unit.reason,
            "awaiting_object": unit.awaiting_object,
            "rows_deferred": unit.rows_deferred,
            "rows_resolvable": resolvable,
            "status": STATUS_PENDING,
            "records_updated": 0,
            "records_failed": 0,
            "detail": "",
            "created_at": _now(),
            "decided_at": None,
            "completed_at": None,
        }
    )


def _shape(row: dict) -> dict:
    entry = dict(row)
    entry["pending"] = entry["status"] == STATUS_PENDING
    entry["awaiting"] = _awaiting(entry)
    entry["summary"] = _describe(entry)
    return entry


def _awaiting(unit: dict) -> list[str]:
    """What this reference is waiting on, named so a person can act on it.

    Prefers the mappings that would populate the object, since "run Users" is
    actionable in a way that "wait for User" is not. Falls back to the object
    itself when nothing in the catalog migrates it yet, because at that point
    naming the object *is* the whole of the instruction.
    """
    from services.internal.mappings_service import (
        get_mapping_config,
        lookup_targets_for_object,
    )

    config = get_mapping_config(unit["mapping_name"]) or {}
    entry = _field_entry(config, unit["field"])
    # Older units carry the parent mapping names directly on the field.
    legacy = [name for name in entry.get("defer_parents") or [] if name]
    if legacy:
        return legacy

    target = unit.get("awaiting_object") or _awaited_object(entry)
    if not target:
        return []
    try:
        names = [c["mapping_name"] for c in lookup_targets_for_object(target)]
    except Exception:  # noqa: BLE001 — naming is advisory, never fatal
        names = []
    return names or [target]


def _describe(unit: dict) -> str:
    """One sentence a person can act on without reading the schema."""
    where = f"{unit['destination_object']}.{unit['field']}"
    deferred, resolvable = unit["rows_deferred"], unit["rows_resolvable"]
    awaiting = unit.get("awaiting") or []
    target = unit.get("awaiting_object") or ""
    if unit["reason"] == REASON_PARENT_PENDING:
        cause = (
            f"nothing had migrated {target or 'the object it points at'} when "
            "these records loaded, so there were no ids to point at"
        )
    elif unit["reason"] == REASON_CYCLE:
        cause = (
            "the objects reference each other, so neither could be loaded with "
            "the other already in place"
        )
    else:
        cause = (
            "the field points back at its own object, so a parent row may not "
            "have existed yet"
        )
    if unit["status"] == STATUS_COMPLETED:
        return (
            f"{where} — filled {unit['records_updated']:,} record(s)"
            + (f", {unit['records_failed']:,} failed" if unit["records_failed"] else "")
        )
    if unit["status"] == STATUS_SKIPPED:
        return f"{where} — skipped, {deferred:,} record(s) left unattached"
    if not resolvable:
        instruction = (
            f"Add {', '.join(awaiting)} to this migration and run it."
            if awaiting
            else "Migrate the object it points at first."
        )
        return (
            f"{where} — {deferred:,} record(s) loaded without it, and none can be "
            f"resolved yet because {cause}. {instruction}"
        )
    return (
        f"{where} — {resolvable:,} of {deferred:,} record(s) can be attached now "
        f"({cause})"
    )


def list_units(
    session_id: str | None = None,
    status: str | None = None,
    refresh: bool = False,
) -> list[dict]:
    """The recorded units, newest first, optionally re-checked against the id map."""
    clauses: list[str] = []
    params: list[object] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_COLUMNS)} FROM backfill_units {where} ORDER BY id DESC",
        tuple(params),
    )
    units = [_shape(dict(zip(_COLUMNS, row))) for row in rows]
    if refresh:
        for unit in units:
            if unit["status"] in (STATUS_PENDING, STATUS_APPROVED):
                _refresh(unit)
    return units


def get_unit(unit_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_COLUMNS)} FROM backfill_units WHERE id = ?", (unit_id,)
    )
    return _shape(dict(zip(_COLUMNS, row))) if row else None


def _refresh(unit: dict) -> dict:
    """Re-count what is resolvable, since loading more objects changes it."""
    from services.internal.mappings_service import get_mapping_config

    config = get_mapping_config(unit["mapping_name"])
    if not config:
        return unit
    entry = _field_entry(config, unit["field"])
    # Units recorded before the field named its target have this blank, and it
    # is what the resolution reads, so it is caught up here rather than only on
    # the next run.
    awaiting_object = unit.get("awaiting_object") or _awaited_object(entry)
    if awaiting_object != unit.get("awaiting_object"):
        db_client.execute(
            "UPDATE backfill_units SET awaiting_object = ? WHERE id = ?",
            (awaiting_object, unit["id"]),
        )
        unit["awaiting_object"] = awaiting_object
        unit["awaiting"] = _awaiting(unit)

    deferred_rows = _read_deferred(config).get(unit["field"], [])
    if not deferred_rows and unit["rows_deferred"]:
        # A later run cleared the transform workspace, taking the per-record
        # detail with it. The unit is still valid and can still be filled — the
        # pass just reads the whole staged table instead of only these rows — so
        # the count recorded at the time is kept rather than reset to nothing.
        return unit
    resolvable = _resolvable(deferred_rows, _lookup_tables(entry, awaiting_object))
    if resolvable != unit["rows_resolvable"]:
        db_client.execute(
            "UPDATE backfill_units SET rows_resolvable = ? WHERE id = ?",
            (resolvable, unit["id"]),
        )
        unit["rows_resolvable"] = resolvable
    unit["summary"] = _describe(unit)
    return unit


def decide(unit_id: int, approved: bool, note: str = "") -> dict:
    """Record a person's decision. Approving does not itself run anything."""
    unit = get_unit(unit_id)
    if not unit:
        raise KeyError(f"No backfill unit with id {unit_id}")
    if unit["status"] not in (STATUS_PENDING, STATUS_APPROVED, STATUS_SKIPPED):
        raise ValueError(
            f"Backfill unit {unit_id} is {unit['status']}, which is not a decision "
            "that can be changed"
        )
    status = STATUS_APPROVED if approved else STATUS_SKIPPED
    db_client.execute(
        "UPDATE backfill_units SET status = ?, decided_at = ?, detail = ? WHERE id = ?",
        (status, _now(), note, unit_id),
    )
    logger.info(
        "[backfill] unit %d (%s.%s) %s",
        unit_id,
        unit["mapping_name"],
        unit["field"],
        "approved" if approved else "skipped",
    )
    return get_unit(unit_id) or unit


def build_config(unit: dict, base_config: dict) -> dict:
    """Derive the update mapping that fills one deferred field.

    The original mapping is left untouched. The derived config writes just this
    field plus the record id, under its own transformation name so its CSVs never
    land among the ones the insert pass produced.

    Two details make it correct. ``look_up_table_names`` stays pointed at the
    original mapping's id map, because that is where the record's own destination
    id lives and the update needs it. And ``skip_if_not_found`` is turned on,
    which is wrong on an insert — it would cost the whole record — but exactly
    right here: a row whose parent still cannot be resolved should be left alone
    rather than updated with a blank.
    """
    source_tables = cfg_source_tables(base_config)
    original_table = get_id_mapping_table_name(
        source_tables[0] if source_tables else "",
        cfg_destination_object(base_config),
        base_config.get("transformation_name"),
    )
    entry = next(
        (
            copy.deepcopy(f)
            for f in base_config.get("fields", [])
            if f.get("destination_field") == unit["field"]
        ),
        None,
    )
    if not entry:
        raise ValueError(
            f"'{unit['field']}' is no longer a field on mapping "
            f"'{unit['mapping_name']}', so there is nothing to backfill"
        )
    entry.pop("defer_to_backfill", None)
    entry.pop("defer_reason", None)
    entry.pop("defer_object", None)
    entry["skip_if_not_found"] = True
    # The field was wired when the parent had no id map, so the table list on it
    # is empty or stale. Resolving it now is the point of having waited: this is
    # the first moment the parent's table is known to exist.
    resolved = _lookup_tables(
        _field_entry(base_config, unit["field"]), unit.get("awaiting_object") or ""
    )
    if resolved:
        params = dict(entry.get("transformation_params") or {})
        params["lookup_tables"] = resolved
        entry["transformation_params"] = params

    derived = copy.deepcopy(base_config)
    derived["operation"] = "update"
    derived["fields"] = [entry]
    derived["look_up_table_names"] = [original_table]
    derived["transformation_name"] = f"{original_table}__backfill__{unit['field']}"
    # An insert-time concern only, and one that would re-read Salesforce for
    # every batch of a pass that needs nothing from it.
    derived.pop("salesforce_objects", None)
    derived.pop("load_batches", None)

    # The rows to revisit, which is the record's own id — not what it points at.
    unique = list(
        dict.fromkeys(
            record_id
            for record_id, _ in _read_deferred(base_config).get(unit["field"], [])
        )
    )
    if unique and len(unique) <= MAX_IDS_IN_FILTER:
        # Narrow the pass to the rows that actually need it. Above the cap the
        # whole table is read instead and the per-field skip does the narrowing,
        # which costs transform time but never correctness.
        source_id_column = base_config.get("source_id_column") or SERVICE_NOW_ID_COLUMN
        derived["filters"] = [
            *(base_config.get("filters") or []),
            {
                "table": source_tables[0] if source_tables else "",
                "field": source_id_column,
                "operator": "in",
                "value": unique,
            },
        ]
    return derived


def _narrow_deferred(config: dict, field_name: str, keep: set[str]) -> None:
    """Rewrite the deferred record so only the still-unattached rows remain.

    A fill that reaches some rows but not others has to leave a record of which
    ones it missed, or a later run of the object they point at has nothing to
    come back to. Rows belonging to other fields are copied through untouched.
    """
    import csv

    for batch in _batch_dirs(config):
        record = batch / DEFERRED_FILENAME
        if not record.exists():
            continue
        try:
            with open(record, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames or []
                rows = [
                    row
                    for row in reader
                    if (row.get("destination_field") or "").strip() != field_name
                    or (row.get(SERVICE_NOW_ID_COLUMN) or "").strip() in keep
                ]
            with open(record, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        except OSError as exc:
            logger.warning("[backfill] could not rewrite %s: %s", record, exc)


def _lookup_tables_for(config: dict, field_name: str) -> list[str]:
    return _lookup_tables(_field_entry(config, field_name))


def settle(
    unit: dict,
    base_config: dict,
    processed: int,
    failed: int,
    errors: list[str],
) -> dict:
    """Record a fill's outcome, and re-offer whatever it could not reach.

    A pass only reaches the rows whose parent had loaded by the time it ran, so
    "finished" and "nothing left" are different things. When rows are still
    waiting on an object that has not been migrated, the reference goes back to
    waiting on a decision with those rows recorded, so migrating that object later
    offers it again instead of leaving the records quietly unattached.
    """
    rows = _read_deferred(base_config).get(unit["field"], [])
    lookup_tables = _lookup_tables_for(base_config, unit["field"])
    known = (
        _known_ids({ref for _, ref in rows if ref}, lookup_tables)
        if rows and lookup_tables
        else set()
    )
    unreachable = [record_id for record_id, ref in rows if ref not in known]
    filled = unit["records_updated"] + processed
    detail = "; ".join(errors[:5])

    if failed:
        # Something the destination rejected. The record of what was deferred is
        # left whole so a retry covers everything, and the error is what to look
        # at rather than a row count.
        status, rows_deferred, resolvable = STATUS_FAILED, unit["rows_deferred"], 0
    elif unreachable:
        status = STATUS_PENDING
        rows_deferred, resolvable = len(dict.fromkeys(unreachable)), 0
        _narrow_deferred(base_config, unit["field"], set(unreachable))
        detail = (
            f"{filled:,} attached. {rows_deferred:,} still point at records that have "
            "not been migrated — migrate that object and this will be offered again."
        )
    else:
        status, rows_deferred, resolvable = STATUS_COMPLETED, 0, 0
        _narrow_deferred(base_config, unit["field"], set())

    db_client.execute(
        """
        UPDATE backfill_units
        SET status = ?, records_updated = ?, records_failed = ?, detail = ?,
            rows_deferred = ?, rows_resolvable = ?, completed_at = ?,
            decided_at = CASE WHEN ? = ? THEN NULL ELSE decided_at END
        WHERE id = ?
        """,
        (
            status,
            filled,
            failed,
            detail[:2000],
            rows_deferred,
            resolvable,
            _now() if status == STATUS_COMPLETED else None,
            status,
            STATUS_PENDING,
            unit["id"],
        ),
    )
    logger.info(
        "[backfill] %s.%s — %d attached, %d failed, %d still waiting (%s)",
        unit["destination_object"],
        unit["field"],
        processed,
        failed,
        rows_deferred if status == STATUS_PENDING else 0,
        status,
    )
    return get_unit(unit["id"]) or unit


def execute(unit_id: int, job_number: int) -> dict:
    """Run one approved backfill: transform the update, then load it.

    Refuses anything not explicitly approved. The transform and load are the
    pipeline's own, so a backfill behaves like any other update pass and its
    failures surface the same way.
    """
    from controllers import loading_controller
    from services.internal.mappings_service import get_mapping_config
    from services.internal.transformation_service import transform_table

    unit = get_unit(unit_id)
    if not unit:
        raise KeyError(f"No backfill unit with id {unit_id}")
    if unit["status"] != STATUS_APPROVED:
        raise PermissionError(
            f"Backfill unit {unit_id} is '{unit['status']}'. A backfill updates "
            "records that already exist in the destination, so it only runs once "
            "it has been approved."
        )

    base_config = get_mapping_config(unit["mapping_name"])
    if not base_config:
        raise KeyError(f"Mapping '{unit['mapping_name']}' no longer exists")

    db_client.execute(
        "UPDATE backfill_units SET status = ? WHERE id = ?", (STATUS_RUNNING, unit_id)
    )
    logger.info(
        "[backfill] filling %s.%s for %d record(s)",
        unit["destination_object"],
        unit["field"],
        unit["rows_resolvable"],
    )

    try:
        derived = build_config(unit, base_config)
        transform_table(job_number, derived)
        processed, failed, errors = loading_controller.load_table(job_number, derived)
    except Exception as exc:  # noqa: BLE001 — recorded against the unit and re-raised
        db_client.execute(
            "UPDATE backfill_units SET status = ?, detail = ?, completed_at = ? WHERE id = ?",
            (STATUS_FAILED, str(exc)[:2000], _now(), unit_id),
        )
        logger.exception("[backfill] unit %d failed", unit_id, exc_info=exc)
        raise

    return settle(unit, base_config, processed, failed, errors)


def run_approved(session_id: str, job_number: int) -> list[dict]:
    """Run every approved unit for a session. Pending ones are left alone."""
    results = []
    for unit in reversed(list_units(session_id=session_id, status=STATUS_APPROVED)):
        results.append(execute(unit["id"], job_number))
    return results


def pending_notice(mapping_names: list[str]) -> list[dict]:
    """What will be deferred if these mappings are launched as they stand.

    Read before a run rather than after, so the deferral is something a person
    was told about up front instead of discovering in a report.
    """
    from services.internal.mappings_service import get_mapping_config

    notices: list[dict] = []
    for name in mapping_names:
        config = get_mapping_config(name)
        if not config:
            continue
        for entry in deferred_fields(config):
            notices.append(
                {
                    "mapping": name,
                    "destination_object": cfg_destination_object(config),
                    "field": entry["destination_field"],
                    "reason": entry.get("defer_reason", "") or REASON_CYCLE,
                    "awaiting": [
                        parent
                        for parent in entry.get("defer_parents") or []
                        if parent and parent not in mapping_names
                    ],
                }
            )
    return notices
