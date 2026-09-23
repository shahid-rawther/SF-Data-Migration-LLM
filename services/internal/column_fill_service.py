"""Filling one column on records that have already been migrated.

Sometimes a column needs a value it was never mapped to receive: a destination
field turns out to be required, or the value it got was wrong, and the records
are already in the destination. Re-running the whole object to change one column
is a heavy answer, and for an insert-based mapping not even a safe one.

This module is the light answer. It writes a single column on the records a
session already loaded, deciding the value in one of three ways:

* a constant, the same for every record;
* a value map from a source column, which is what the picklist matcher produces;
* ordered rules over the source row, where the first matching rule wins.

The write itself is nothing new. :func:`build_config` derives an ordinary
``operation: "update"`` mapping holding just this one field, and the pipeline's
own transform and load carry it out — the same path a backfill takes. What is
new is :func:`preview`, which evaluates the same specification against the staged
source and reports what would be written before anything is sent. That matters
more here than for a backfill: a backfill fills a blank the loader itself left,
whereas this overwrites whatever is there on a person's say-so, so the intent has
to be visible first.

Distinct from :mod:`services.internal.backfill_service` on purpose. A backfill is
owed — the loader could not resolve a lookup and has to come back. Nothing here
is owed, so there is no approval gate and no deferred record to narrow; the
preview is the check instead.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from common.logger import logger
from config.settings import PATH_TO_DOWNLOADS, SERVICE_NOW_ID_COLUMN
from services.external.db_client import db_client
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
    get_id_mapping_table_name,
)

MODE_CONSTANT = "constant"
MODE_VALUE_MAP = "value_map"
MODE_RULES = "rules"
MODES = (MODE_CONSTANT, MODE_VALUE_MAP, MODE_RULES)

STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# What to do with a row no rule claimed and no map covered.
UNMATCHED_SKIP = "skip"
UNMATCHED_BLANK = "blank"
UNMATCHED_VALUE = "value"

# Rows shown alongside the counts, so the numbers can be sanity-checked against
# actual data rather than trusted on their own.
SAMPLE_LIMIT = 8

# Above this, the update pass reads the whole staged table and lets the per-field
# skip narrow it, rather than naming every id in a WHERE clause. Matches the
# backfill's cap for the same reason.
MAX_IDS_IN_FILTER = 4000

_COLUMNS = (
    "id",
    "session_id",
    "mapping_name",
    "destination_object",
    "source_table",
    "field",
    "mode",
    "spec",
    "status",
    "rows_targeted",
    "records_updated",
    "records_failed",
    "saved_to_mapping",
    "detail",
    "created_at",
    "completed_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shape(row) -> dict:
    """One job row as a dict, with the spec parsed back into structure."""
    job = dict(zip(_COLUMNS, row))
    try:
        job["spec"] = json.loads(job.get("spec") or "{}")
    except (TypeError, ValueError):
        job["spec"] = {}
    job["saved_to_mapping"] = bool(job.get("saved_to_mapping"))
    job["summary"] = _describe(job)
    return job


def _describe(job: dict) -> str:
    """One sentence about what a job did or would do."""
    where = f"{job['destination_object']}.{job['field']}"
    if job["status"] == STATUS_COMPLETED:
        failed = job["records_failed"]
        tail = f", {failed:,} rejected" if failed else ""
        return f"Filled {where} on {job['records_updated']:,} record(s){tail}."
    if job["status"] == STATUS_FAILED:
        return f"Filling {where} failed. {job['detail'][:200]}"
    if job["status"] == STATUS_RUNNING:
        return f"Filling {where} now."
    if job["status"] == STATUS_CANCELLED:
        return f"Filling {where} was cancelled."
    return f"Ready to fill {where} on {job['rows_targeted']:,} record(s)."


def _configure_destination(session_id: str) -> None:
    """Point the shared destination client at this session's org.

    The pipeline configures that client when a run starts, so anything that
    loads later in the life of the same process inherits whatever the last run
    left behind — and in a process where no run has happened, nothing at all.
    A fill is exactly that kind of later work, so it sets the endpoint itself
    rather than depending on a run having preceded it.

    Without this the failure is a ``TypeError`` from concatenating ``None``,
    which says nothing about the credentials being the problem.
    """
    from api import sources as plugin_registry

    config = _session_config(session_id)
    credentials = config.get("destination_credentials") or {}
    if not credentials or any(value == "***" for value in credentials.values()):
        raise ValueError(
            "This session has no usable destination credentials stored, so the "
            "fill cannot be sent. Link a saved Salesforce connection to the "
            "session and try again."
        )

    destination_type = config.get("destination_type") or "salesforce"
    plugin = plugin_registry.get_destination(destination_type)
    if plugin is None:
        raise ValueError(f"No destination integration registered for '{destination_type}'")
    plugin.configure_client(credentials)


def _session_config(session_id: str) -> dict:
    """The stored session config with real credentials.

    Read from the row directly because the session API masks secrets, and a
    preview has to describe the destination the update would actually go to.
    """
    raw = db_client.fetch_one(
        "SELECT config FROM migration_sessions WHERE id = ?", (session_id,)
    )
    if not raw:
        raise KeyError(f"No session with id {session_id}")
    try:
        return json.loads(raw[0])
    except (TypeError, ValueError):
        return {}


# ── The specification ──────────────────────────────────────────────────────────


def referenced_fields(spec: dict) -> list[str]:
    """Every source column the spec reads, in a stable order.

    The order is what lets a condition name the column it tests: the mapping
    lists these in ``source_field`` and the transform receives them positionally,
    so the same sequence has to be handed to ``inputs``.
    """
    mode = spec.get("mode")
    if mode == MODE_VALUE_MAP:
        field = (spec.get("source_field") or "").strip()
        return [field] if field else []
    if mode != MODE_RULES:
        return []

    seen: list[str] = []

    def note(name):
        name = (name or "").strip()
        if name and name not in seen:
            seen.append(name)

    for rule in spec.get("rules") or []:
        for group in ("all", "any"):
            for condition in rule.get(group) or []:
                note(condition.get("field"))
        outcome = rule.get("then") or {}
        if isinstance(outcome, dict):
            note(outcome.get("from"))
    otherwise = spec.get("otherwise") or {}
    if isinstance(otherwise, dict):
        note(otherwise.get("from"))
    return seen


def validate_spec(spec: dict) -> dict:
    """Check a fill specification and return it normalised.

    Raises ``ValueError`` with a message meant for the person who wrote the
    spec, because every one of these is a mistake in the request rather than a
    fault in the run.
    """
    from services.transforms.generic import (
        GENERIC_TRANSFORMS,
        RULE_OPERATORS,
        RULE_OPERATORS_WITHOUT_VALUE,
    )

    clean = dict(spec or {})
    mode = (clean.get("mode") or "").strip()
    if mode not in MODES:
        raise ValueError(
            f"Unknown fill mode '{mode or '(missing)'}'. "
            f"Expected one of: {', '.join(MODES)}."
        )
    if not (clean.get("mapping_name") or "").strip():
        raise ValueError("A fill needs the mapping whose records it updates.")
    if not (clean.get("field") or "").strip():
        raise ValueError("A fill needs the destination column to write.")

    unmatched = (clean.get("unmatched") or UNMATCHED_SKIP).strip()
    if unmatched not in (UNMATCHED_SKIP, UNMATCHED_BLANK, UNMATCHED_VALUE):
        raise ValueError(
            f"Unknown choice '{unmatched}' for rows nothing matched. Expected "
            f"{UNMATCHED_SKIP}, {UNMATCHED_BLANK} or {UNMATCHED_VALUE}."
        )
    clean["unmatched"] = unmatched
    if unmatched == UNMATCHED_VALUE and not str(clean.get("unmatched_value") or ""):
        raise ValueError(
            "Choosing a value for unmatched rows needs that value to be given."
        )

    if mode == MODE_CONSTANT:
        if clean.get("constant_value") in (None, ""):
            raise ValueError("A constant fill needs the value to write.")

    if mode == MODE_VALUE_MAP:
        if not (clean.get("source_field") or "").strip():
            raise ValueError("A value map needs the source column to read.")
        if not (clean.get("value_map") or {}):
            raise ValueError("A value map needs at least one pair.")

    if mode == MODE_RULES:
        rules = clean.get("rules") or []
        if not rules:
            raise ValueError("A conditional fill needs at least one rule.")
        for position, rule in enumerate(rules, start=1):
            conditions = (rule.get("all") or []) + (rule.get("any") or [])
            if not conditions and position < len(rules):
                raise ValueError(
                    f"Rule {position} has no conditions, so every row would stop "
                    "there and the rules below it could never apply. Move it last "
                    "or give it a condition."
                )
            for condition in conditions:
                operator = (condition.get("op") or "").strip()
                if operator not in RULE_OPERATORS:
                    raise ValueError(
                        f"Rule {position} uses an unknown test "
                        f"'{operator or '(missing)'}'."
                    )
                if not (condition.get("field") or "").strip():
                    raise ValueError(
                        f"Rule {position} does not say which source column to test."
                    )
                needs_value = operator not in RULE_OPERATORS_WITHOUT_VALUE
                if needs_value and condition.get("value") in (None, ""):
                    raise ValueError(
                        f"Rule {position} tests '{operator}' but gives nothing to "
                        "compare against."
                    )
            outcome = rule.get("then") or {}
            transform = (outcome.get("transform") or "").strip()
            if transform:
                if transform == "generic_rules":
                    raise ValueError(
                        "Rules cannot use rules as their output transform."
                    )
                if transform not in GENERIC_TRANSFORMS:
                    raise ValueError(
                        f"Rule {position} names an unknown transform '{transform}'."
                    )
                if not (outcome.get("from") or "").strip():
                    raise ValueError(
                        f"Rule {position} applies '{transform}' but does not say "
                        "which source column to apply it to."
                    )
            elif outcome.get("value") in (None, ""):
                raise ValueError(
                    f"Rule {position} does not say what value to set. Use blank "
                    "as the choice for unmatched rows instead if that is the intent."
                )
    return clean


def field_entry(spec: dict, *, for_update: bool = True) -> dict:
    """The mapping field entry a spec describes.

    ``for_update`` distinguishes the copy that runs now from the copy written
    into the mapping. The running copy sets ``skip_if_not_found`` so an unmatched
    row is left exactly as it is. On an insert that same flag would throw the
    whole record away, so the stored copy never carries it: a future run leaves
    the column blank instead of dropping the row.
    """
    mode = spec["mode"]
    field = spec["field"]
    unmatched = spec.get("unmatched", UNMATCHED_SKIP)
    entry: dict = {"destination_field": field}

    if mode == MODE_CONSTANT:
        entry["constant_value"] = spec["constant_value"]
        entry["value_source"] = "constant"
        return entry

    if mode == MODE_VALUE_MAP:
        entry["source_field"] = spec["source_field"]
        entry["transformation_function"] = "generic_value_map"
        entry["transformation_params"] = {
            "map": dict(spec.get("value_map") or {}),
            "default": (
                str(spec.get("unmatched_value") or "")
                if unmatched == UNMATCHED_VALUE
                else ""
            ),
            "case_insensitive": bool(spec.get("case_insensitive", True)),
        }
    else:
        inputs = referenced_fields(spec)
        entry["source_field"] = inputs
        entry["transformation_function"] = "generic_rules"
        entry["transformation_params"] = {
            "inputs": inputs,
            "rules": copy.deepcopy(spec.get("rules") or []),
            "otherwise": (
                {"value": str(spec.get("unmatched_value") or "")}
                if unmatched == UNMATCHED_VALUE
                else {"value": ""}
            ),
            "case_insensitive": bool(spec.get("case_insensitive", True)),
        }

    if for_update and unmatched == UNMATCHED_SKIP:
        # Right for an update and wrong for an insert, which is why the stored
        # copy is built without it.
        entry["skip_if_not_found"] = True
    return entry


# ── What can be filled ─────────────────────────────────────────────────────────


def targets(session: dict) -> list[dict]:
    """The objects in a session that have records to update.

    Only objects with loaded records are offered. A column fill issues updates
    against destination ids, so an object that never loaded has nothing to update
    and offering it would only produce a pass that wrote nothing.
    """
    from services.internal import session_status_service
    from services.internal.mappings_service import get_mapping_config

    report = session_status_service.build(session)
    available: list[dict] = []
    for entry in report.get("objects", []):
        if int(entry.get("loaded") or 0) <= 0:
            continue
        config = get_mapping_config(entry["object"]) or {}
        source_tables = cfg_source_tables(config)
        available.append(
            {
                "mapping_name": entry["object"],
                "destination_object": entry.get("destination_object") or "",
                "source_table": source_tables[0] if source_tables else "",
                "loaded": int(entry.get("loaded") or 0),
                "source_rows": int(entry.get("source_rows") or 0),
                "operation": config.get("operation") or "insert",
            }
        )
    return available


# ── Preview ────────────────────────────────────────────────────────────────────


def _staged_dir(source_type: str, table: str) -> Path:
    return PATH_TO_DOWNLOADS / (source_type or "servicenow") / table


def _migrated_ids(config: dict) -> set[str]:
    """The source ids this mapping has destination records for."""
    source_tables = cfg_source_tables(config)
    table_name = get_id_mapping_table_name(
        source_tables[0] if source_tables else "",
        cfg_destination_object(config),
        config.get("transformation_name"),
    )
    rows = db_client.fetch_all(
        "SELECT servicenow_id FROM id_mappings WHERE table_name = ?", (table_name,)
    )
    return {tuple(row)[0] for row in rows}


def _rule_label(rule: dict, position: int) -> str:
    """A short readable rendering of one rule's conditions."""
    parts = []
    for group, joiner in (("all", " and "), ("any", " or ")):
        rendered = [
            f"{(c.get('field') or '').split('.')[-1]} {c.get('op')} "
            f"{c.get('value') if c.get('value') not in (None, '') else ''}".strip()
            for c in rule.get(group) or []
        ]
        if rendered:
            parts.append(joiner.join(rendered))
    return " and ".join(parts) if parts else f"rule {position} (everything else)"


def preview(session_id: str, spec: dict, sample_limit: int = SAMPLE_LIMIT) -> dict:
    """Evaluate a fill against the staged source without writing anything.

    Runs the very transform the update would run, so what is reported is what
    would happen rather than an approximation of it. The counts separate three
    different things that are easy to conflate: rows in the staged extract, rows
    that actually have a destination record to update, and rows a value was
    produced for.
    """
    from services.external import duckdb_client
    from services.internal.mappings_service import get_mapping_config
    from services.transforms.generic import GENERIC_TRANSFORMS, rules_trace

    spec = validate_spec(spec)
    config = get_mapping_config(spec["mapping_name"])
    if not config:
        raise KeyError(f"Mapping '{spec['mapping_name']}' does not exist")

    session_config = _session_config(session_id)
    source_type = config.get("source_type") or session_config.get(
        "source_type", "servicenow"
    )
    source_tables = cfg_source_tables(config)
    driving_table = source_tables[0] if source_tables else ""
    destination_object = cfg_destination_object(config)
    field = spec["field"]
    entry = field_entry(spec)

    report: dict = {
        "mapping_name": spec["mapping_name"],
        "destination_object": destination_object,
        "source_table": driving_table,
        "field": field,
        "mode": spec["mode"],
        "rows_source": 0,
        "rows_migrated": 0,
        "rows_to_write": 0,
        "rows_untouched": 0,
        "untouched_means": (
            "left exactly as they are"
            if spec.get("unmatched") == UNMATCHED_SKIP
            else "set to blank"
        ),
        "rule_hits": [],
        "value_hits": [],
        "samples": [],
        "picklist_rejected": [],
        "warnings": [],
    }

    staged = _staged_dir(source_type, driving_table)
    if not staged.exists():
        report["warnings"].append(
            f"Nothing is staged for '{driving_table}', so the fill cannot be "
            "previewed. Its extract was cleared, or the object has not run."
        )
        return report

    source_id_column = config.get("source_id_column") or SERVICE_NOW_ID_COLUMN
    inputs = referenced_fields(spec)
    wanted = list(dict.fromkeys([f"{driving_table}.{source_id_column}", *inputs]))
    try:
        frame = duckdb_client.fetch_all(wanted, staged)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        report["warnings"].append(f"Could not read the staged extract: {exc}")
        return report

    rows = frame.to_dict("records")
    report["rows_source"] = len(rows)
    migrated = _migrated_ids(config)
    report["rows_migrated"] = len(migrated)

    params = entry.get("transformation_params") or {}
    func = GENERIC_TRANSFORMS.get(entry.get("transformation_function") or "")
    rule_counts: dict[int, int] = {}
    value_counts: dict[tuple[str, str], int] = {}
    produced_values: dict[str, int] = {}
    to_write = 0
    untouched = 0
    unmigrated = 0

    for row in rows:
        record_id = str(row.get(source_id_column) or "").strip()
        if record_id not in migrated:
            # Present in the extract but never loaded, so an update has no record
            # to address. Counted separately rather than as "untouched", which
            # would suggest a decision was made about it.
            unmigrated += 1
            continue

        values = [row.get(name.split(".")[-1], "") for name in inputs]

        if spec["mode"] == MODE_CONSTANT:
            value = str(spec["constant_value"])
        elif spec["mode"] == MODE_RULES:
            index, value = rules_trace(field, *values, params=params)
            rule_counts[index] = rule_counts.get(index, 0) + 1
        else:
            value = str(func(field, *values, params=params).get(field, ""))
            source_value = str(values[0] if values else "").strip()
            value_counts[(source_value, value)] = (
                value_counts.get((source_value, value), 0) + 1
            )

        if value:
            to_write += 1
            produced_values[value] = produced_values.get(value, 0) + 1
        else:
            untouched += 1

        if len(report["samples"]) < sample_limit:
            report["samples"].append(
                {
                    "record": record_id,
                    "source": {
                        name.split(".")[-1]: str(row.get(name.split(".")[-1], ""))
                        for name in inputs
                    },
                    "value": value,
                }
            )

    report["rows_to_write"] = to_write
    report["rows_untouched"] = untouched
    if unmigrated:
        report["warnings"].append(
            f"{unmigrated:,} staged row(s) were skipped because this session has "
            "no destination record for them."
        )

    if spec["mode"] == MODE_RULES:
        rules = spec.get("rules") or []
        report["rule_hits"] = [
            {
                "index": index,
                "label": _rule_label(rule, index + 1),
                "value": (rule.get("then") or {}).get("value")
                or (rule.get("then") or {}).get("transform")
                or "",
                "rows": rule_counts.get(index, 0),
            }
            for index, rule in enumerate(rules)
        ]
        if rule_counts.get(-1):
            report["rule_hits"].append(
                {
                    "index": -1,
                    "label": "nothing matched",
                    "value": "",
                    "rows": rule_counts[-1],
                }
            )
    elif spec["mode"] == MODE_VALUE_MAP:
        report["value_hits"] = sorted(
            (
                {
                    "source": source,
                    "destination": destination,
                    "rows": count,
                    "mapped": bool(destination),
                }
                for (source, destination), count in value_counts.items()
            ),
            key=lambda hit: -hit["rows"],
        )

    report["picklist_rejected"] = _picklist_violations(
        destination_object,
        field,
        produced_values,
        session_config,
        config,
        report["warnings"],
    )
    return report


def _picklist_violations(
    destination_object: str,
    field: str,
    produced: dict[str, int],
    session_config: dict,
    config: dict,
    warnings: list,
) -> list[dict]:
    """Values the destination would refuse because the picklist is restricted.

    Checked here rather than left to the load, because a restricted picklist
    rejects the row outright: without this the first sign of a wrong value is a
    pile of load errors after the fact.
    """
    if not produced:
        return []
    credentials = session_config.get("destination_credentials") or {}
    if not credentials or any(v == "***" for v in credentials.values()):
        warnings.append(
            "The destination's allowed values were not checked, because this "
            "session has no usable Salesforce credentials stored."
        )
        return []

    try:
        from api.compat_engine import get_destination_fields, record_type_id_of

        fields = get_destination_fields(
            destination_object, credentials, record_type_id_of(config)
        )
    except Exception as exc:  # noqa: BLE001 — advisory only
        warnings.append(f"Could not read the destination's allowed values: {exc}")
        return []

    meta = next((f for f in fields if f.get("name") == field), None)
    if not meta or not meta.get("picklist_values"):
        return []
    if not meta.get("picklist_restricted"):
        # An unrestricted picklist accepts anything, so a value outside the list
        # is a choice rather than a failure waiting to happen.
        return []

    allowed = {str(v) for v in meta.get("picklist_values") or []}
    return [
        {"value": value, "rows": count}
        for value, count in sorted(produced.items(), key=lambda kv: -kv[1])
        if value and value not in allowed
    ]


# ── Jobs ───────────────────────────────────────────────────────────────────────


def create(session_id: str, spec: dict, rows_targeted: int = 0) -> dict:
    """Record a fill as a draft. Nothing is sent until it is run."""
    from services.internal.mappings_service import get_mapping_config

    spec = validate_spec(spec)
    config = get_mapping_config(spec["mapping_name"])
    if not config:
        raise KeyError(f"Mapping '{spec['mapping_name']}' does not exist")

    source_tables = cfg_source_tables(config)
    db_client.execute(
        """
        INSERT INTO column_fill_jobs (
            session_id, mapping_name, destination_object, source_table, field,
            mode, spec, status, rows_targeted, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            spec["mapping_name"],
            cfg_destination_object(config),
            source_tables[0] if source_tables else "",
            spec["field"],
            spec["mode"],
            json.dumps(spec),
            STATUS_DRAFT,
            int(rows_targeted or 0),
            _now(),
        ),
    )
    row = db_client.fetch_one(
        f"SELECT {', '.join(_COLUMNS)} FROM column_fill_jobs "
        "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    job = _shape(row)
    logger.info(
        "[column-fill] queued %s.%s (%s) for session %s",
        job["destination_object"],
        job["field"],
        job["mode"],
        session_id,
    )
    return job


def get_job(job_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_COLUMNS)} FROM column_fill_jobs WHERE id = ?", (job_id,)
    )
    return _shape(row) if row else None


def list_jobs(session_id: str = "", status: str = "") -> list[dict]:
    clauses, params = [], []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_COLUMNS)} FROM column_fill_jobs{where} ORDER BY id DESC",
        tuple(params),
    )
    return [_shape(row) for row in rows]


def cancel(job_id: int) -> dict:
    """Abandon a draft, keeping it readable as a record of what was considered."""
    job = get_job(job_id)
    if not job:
        raise KeyError(f"No column fill with id {job_id}")
    if job["status"] != STATUS_DRAFT:
        raise ValueError(
            f"Column fill {job_id} is '{job['status']}', which cannot be cancelled."
        )
    db_client.execute(
        "UPDATE column_fill_jobs SET status = ?, completed_at = ? WHERE id = ?",
        (STATUS_CANCELLED, _now(), job_id),
    )
    return get_job(job_id) or job


# ── Running ────────────────────────────────────────────────────────────────────


def build_config(job: dict, base_config: dict) -> dict:
    """Derive the update mapping that writes this one column.

    The original mapping is untouched. The derived config carries only this field,
    under its own transformation name so its CSVs never land among the ones the
    original pass produced.

    ``look_up_table_names`` stays pointed at the original mapping's id map,
    because that is where each record's own destination id lives and an update
    cannot address a record without it.
    """
    source_tables = cfg_source_tables(base_config)
    original_table = get_id_mapping_table_name(
        source_tables[0] if source_tables else "",
        cfg_destination_object(base_config),
        base_config.get("transformation_name"),
    )
    spec = job["spec"]

    derived = copy.deepcopy(base_config)
    derived["operation"] = "update"
    derived["fields"] = [field_entry(spec)]
    derived["look_up_table_names"] = [original_table]
    derived["transformation_name"] = f"{original_table}__fill__{job['field']}"
    # Insert-time concerns that would have this pass re-read the destination for
    # every batch while needing nothing from it.
    derived.pop("salesforce_objects", None)
    derived.pop("load_batches", None)
    # An external id turns the load into an upsert. This pass addresses records
    # by their destination id and must never create one.
    derived.pop("external_id_field", None)
    derived.pop("external_id", None)
    return derived


def execute(job_id: int, job_number: int) -> dict:
    """Run one fill: transform the update, load it, record what happened."""
    from controllers import loading_controller
    from services.internal.mappings_service import get_mapping_config
    from services.internal.transformation_service import transform_table

    job = get_job(job_id)
    if not job:
        raise KeyError(f"No column fill with id {job_id}")
    if job["status"] not in (STATUS_DRAFT, STATUS_FAILED):
        raise ValueError(
            f"Column fill {job_id} is '{job['status']}', so there is nothing to run."
        )

    base_config = get_mapping_config(job["mapping_name"])
    if not base_config:
        raise KeyError(f"Mapping '{job['mapping_name']}' no longer exists")

    # Before the status moves off draft, so a missing connection leaves the job
    # exactly as it was rather than stranding it in 'running'.
    _configure_destination(job["session_id"])

    db_client.execute(
        "UPDATE column_fill_jobs SET status = ? WHERE id = ?", (STATUS_RUNNING, job_id)
    )
    logger.info(
        "[column-fill] writing %s.%s for up to %d record(s)",
        job["destination_object"],
        job["field"],
        job["rows_targeted"],
    )

    try:
        derived = build_config(job, base_config)
        transform_table(job_number, derived)
        processed, failed, errors = loading_controller.load_table(job_number, derived)
    except Exception as exc:  # noqa: BLE001 — recorded against the job and re-raised
        db_client.execute(
            "UPDATE column_fill_jobs SET status = ?, detail = ?, completed_at = ? "
            "WHERE id = ?",
            (STATUS_FAILED, str(exc)[:2000], _now(), job_id),
        )
        logger.exception("[column-fill] job %d failed", job_id, exc_info=exc)
        raise

    saved = False
    if job["spec"].get("save_to_mapping"):
        try:
            save_to_mapping(job, base_config)
            saved = True
        except Exception as exc:  # noqa: BLE001 — the fill itself already ran
            errors = [*errors, f"the fill ran, but the mapping was not updated: {exc}"]
            logger.warning("[column-fill] job %d: mapping not saved: %s", job_id, exc)

    status = STATUS_FAILED if failed and not processed else STATUS_COMPLETED
    db_client.execute(
        """
        UPDATE column_fill_jobs
        SET status = ?, records_updated = ?, records_failed = ?, detail = ?,
            saved_to_mapping = ?, completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            processed,
            failed,
            "; ".join(errors[:5])[:2000],
            1 if saved else 0,
            _now(),
            job_id,
        ),
    )
    logger.info(
        "[column-fill] %s.%s — %d written, %d rejected (%s)",
        job["destination_object"],
        job["field"],
        processed,
        failed,
        status,
    )
    return get_job(job_id) or job


def save_to_mapping(job: dict, base_config: dict | None = None) -> dict:
    """Write the field into the mapping so later runs of the object apply it.

    Stored without ``skip_if_not_found``: on an update that flag protects a row
    the fill does not cover, but on an insert it would discard the whole record.
    A future run therefore leaves the column blank rather than losing the row.
    """
    from services.internal.mappings_service import get_mapping_config, save_mapping_config

    config = copy.deepcopy(base_config or get_mapping_config(job["mapping_name"]) or {})
    if not config:
        raise KeyError(f"Mapping '{job['mapping_name']}' no longer exists")

    entry = field_entry(job["spec"], for_update=False)
    fields = [
        f for f in config.get("fields") or [] if f.get("destination_field") != job["field"]
    ]
    fields.append(entry)
    config["fields"] = fields
    return save_mapping_config(
        job["mapping_name"],
        config,
        source_type=config.get("source_type") or "servicenow",
        priority=config.get("priority"),
    )
