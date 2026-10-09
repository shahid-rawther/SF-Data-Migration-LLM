"""Can the source actually supply what the destination insists on?

A mapping can be complete on paper and still load nothing: the column it reads
is empty on half the rows, the lookup it needs points at an object nothing
migrates, or the parent is something Salesforce will not let an API create at
all — a licence, a profile, the organisation itself. The compatibility check
asks whether each field has a value source. This asks whether that source has
data, and whether every parent the load depends on can exist by the time it
runs.

It is deterministic and runs before any model call. Each requirement comes out
with one status:

* ``covered`` — mapped, and the source column has a value on every row (or a
  constant or fallback fills the gaps);
* ``partial`` — mapped, but some source rows are empty and nothing fills them;
* ``unmapped_with_candidate`` — not mapped, and source columns that look like
  they hold the value exist;
* ``no_source`` — not mapped, and nothing in the source looks like it;
* ``parent_migratable`` — a required lookup whose parent a mapping loads,
  either in this run or available to add to it;
* ``parent_creatable`` — a required lookup onto an object nothing migrates but
  which the API can create, so one record can be made and pinned;
* ``parent_not_creatable`` — a required lookup onto an object the API cannot
  create, so it can only point at records that already exist.

Parents that a mapping outside the run would load are followed, so the chain
behind a selection is visible: Incident needs User, and User needs a Profile.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from common.logger import logger
from services.external.db_client import db_client
from services.internal import sf_inventory

from agents import wiring
from agents.context import MigrationContext

STATUS_COVERED = "covered"
STATUS_PARTIAL = "partial"
STATUS_UNMAPPED_WITH_CANDIDATE = "unmapped_with_candidate"
STATUS_NO_SOURCE = "no_source"
STATUS_PARENT_MIGRATABLE = "parent_migratable"
STATUS_PARENT_CREATABLE = "parent_creatable"
STATUS_PARENT_NOT_CREATABLE = "parent_not_creatable"

GAP_STATUSES = frozenset(
    {
        STATUS_PARTIAL,
        STATUS_UNMAPPED_WITH_CANDIDATE,
        STATUS_NO_SOURCE,
        STATUS_PARENT_MIGRATABLE,
        STATUS_PARENT_CREATABLE,
        STATUS_PARENT_NOT_CREATABLE,
    }
)

# How far behind the selection the chain of needed parents is followed.
MAX_CHAIN_DEPTH = 2
MAX_CANDIDATES = 3

# Transforms that supply a value when the source column is empty, so an empty
# source row does not become an empty required field.
_FALLBACK_TRANSFORMS = {"generic_default_if_empty", "generic_rules", "generic_length_guard"}

_SYNONYMS: dict[str, set[str]] = {
    "subject": {"short", "description", "title", "subject", "summary"},
    "title": {"short", "description", "title", "name"},
    "name": {"name", "title", "number", "short"},
    "description": {"description", "notes", "details", "comments"},
    "status": {"state", "status", "stage"},
    "priority": {"priority", "urgency", "impact"},
    "owner": {"assigned", "owner", "assignee"},
    "email": {"email", "mail"},
    "phone": {"phone", "mobile", "telephone"},
    "last": {"last", "surname", "family"},
    "first": {"first", "given"},
    "user": {"user", "caller", "requested", "opened", "assigned"},
    "username": {"user", "name", "email"},
    "alias": {"user", "name", "initials"},
    "category": {"category", "type", "classification"},
    "reason": {"reason", "code", "cause"},
    "start": {"start", "opened", "begin", "created"},
    "end": {"end", "closed", "resolved"},
    "date": {"date", "time", "on", "at"},
    "time": {"time", "date", "zone"},
    "locale": {"locale", "language", "country"},
    "language": {"language", "locale"},
    "zone": {"zone", "timezone", "time"},
}


@dataclass
class Requirement:
    mapping: str
    object: str
    field: str
    label: str = ""
    type: str = ""
    status: str = STATUS_COVERED
    detail: str = ""
    # What currently fills it, if anything.
    source_table: str = ""
    source_field: str = ""
    value_source: str = ""
    total_rows: int = -1
    empty_rows: int = -1
    candidates: list[str] = field(default_factory=list)
    # For lookups.
    reference_to: list[str] = field(default_factory=list)
    parent_object: str = ""
    parent_createable: bool | None = None
    parent_mappings: list[str] = field(default_factory=list)
    parent_in_run: bool = False
    parent_existing_records: int = -1
    # The lookups that led here from the selection, e.g. ["Incident.CallerId"].
    via: list[str] = field(default_factory=list)
    in_run: bool = True

    @property
    def is_gap(self) -> bool:
        return self.status in GAP_STATUSES

    @property
    def key(self) -> str:
        return f"{self.mapping}.{self.field}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["key"] = self.key
        data["is_gap"] = self.is_gap
        return data


@dataclass
class ReadinessReport:
    selected: list[str]
    requirements: list[Requirement] = field(default_factory=list)
    # Mappings not in the run that the selection depends on.
    chain_mappings: list[str] = field(default_factory=list)
    capacity: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def gaps(self) -> list[Requirement]:
        return [r for r in self.requirements if r.is_gap]

    def to_dict(self) -> dict:
        counts: dict[str, int] = {}
        for requirement in self.requirements:
            counts[requirement.status] = counts.get(requirement.status, 0) + 1
        return {
            "selected": self.selected,
            "requirements": [r.to_dict() for r in self.requirements],
            "gap_count": len(self.gaps),
            "status_counts": counts,
            "chain_mappings": self.chain_mappings,
            "capacity": self.capacity,
            "warnings": self.warnings,
        }


# ── helpers ────────────────────────────────────────────────────────────────────


def _tokens(text: str) -> set[str]:
    text = re.sub(r"__[cr]$", "", text or "")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", text) if len(t) > 1}


def find_candidates(meta: dict, source_fields: list[dict], limit: int = MAX_CANDIDATES) -> list[str]:
    """Source columns whose name or label shares meaning with a destination field."""
    wanted = _tokens(meta.get("name", "")) | _tokens(meta.get("label", ""))
    wanted -= {"id", "c"}
    expanded = set(wanted)
    for token in wanted:
        expanded |= _SYNONYMS.get(token, set())
    scored: list[tuple[int, str]] = []
    for source in source_fields:
        name = (source.get("name") or "").split(".")[-1]
        if not name or name.startswith("sys_"):
            continue
        have = _tokens(name) | _tokens(source.get("label", ""))
        # A word the destination itself uses outranks one only a synonym links,
        # so Subject/"Short Description" prefers short_description to notes.
        score = 2 * len(wanted & have) + len(expanded & have)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:limit]]


def value_source(entry: dict | None) -> str:
    """How a field mapping produces its value: constant, env, transform or column."""
    if not entry:
        return ""
    if entry.get("constant_value") not in (None, ""):
        return "constant"
    if entry.get("use_constant"):
        return "env_constant"
    if entry.get("source_field"):
        return "source"
    return ""


def _has_fallback(entry: dict) -> bool:
    transform = entry.get("transformation_function") or ""
    params = entry.get("transformation_params") or {}
    if transform in _FALLBACK_TRANSFORMS:
        return True
    if transform == "generic_lookup" and params.get("default"):
        return True
    return False


def _id_rows(tables: list[str]) -> int:
    if not tables:
        return 0
    try:
        marks = ", ".join("?" for _ in tables)
        row = db_client.fetch_one(
            f"SELECT COUNT(*) FROM id_mappings WHERE table_name IN ({marks})", tuple(tables)
        )
        return int(row[0]) if row else 0
    except Exception:
        return 0


class _Counter:
    """Source counts, each asked of the source once per analysis."""

    def __init__(self, context: MigrationContext):
        from api import sources as source_registry

        self.provider = source_registry.get(context.source_type)
        self.credentials = context.source_credentials
        self._totals: dict[str, int] = {}
        self._empty: dict[tuple[str, str], int] = {}

    def total(self, table: str) -> int:
        if not table or not self.provider:
            return -1
        if table not in self._totals:
            self._totals[table] = self.provider.count_rows(table, self.credentials)
        return self._totals[table]

    def empty(self, table: str, column: str) -> int:
        if not table or not column or not self.provider:
            return -1
        key = (table, column)
        if key not in self._empty:
            self._empty[key] = self.provider.count_empty(table, column, self.credentials)
        return self._empty[key]


# ── the pass ───────────────────────────────────────────────────────────────────


def expand_chain(context: MigrationContext, depth: int = MAX_CHAIN_DEPTH) -> list[str]:
    """Mappings outside the run that load a parent the run requires.

    Only required lookups pull a mapping in. An optional lookup onto an object
    nobody migrates loads empty, which is the relationship check's business.
    """
    chain: list[str] = []
    frontier = list(context.mapping_names)
    seen = set(frontier)
    for _ in range(depth):
        found: list[str] = []
        for name in frontier:
            obj = context.destination_object(name) if name in context.configs else ""
            config = context.configs.get(name) or wiring.load_configs([name]).get(name) or {}
            obj = obj or wiring.cfg_destination_object(config)
            for meta in context.reference_fields(obj):
                if not meta.get("required") or not meta.get("createable"):
                    continue
                entry = wiring.field_entry(config, meta["name"])
                if value_source(entry) in ("constant", "env_constant"):
                    continue
                for target in meta.get("reference_to") or []:
                    if target == obj or context.mapping_for_destination_object(target):
                        continue
                    for candidate in context.available_mappings_for_object(target)[:1]:
                        if candidate not in seen:
                            seen.add(candidate)
                            found.append(candidate)
        chain.extend(found)
        frontier = found
        if not found:
            break
    return chain


def analyse(context: MigrationContext, selected: list[str] | None = None) -> ReadinessReport:
    """Classify every required destination field of every mapping in ``context``.

    ``selected`` is what the person chose; anything else in the context is a
    chain mapping the analysis pulled in, and is reported as not in the run.
    """
    selected = list(selected if selected is not None else context.mapping_names)
    report = ReadinessReport(
        selected=selected,
        chain_mappings=[n for n in context.mapping_names if n not in set(selected)],
    )
    counter = _Counter(context)
    catalog = sf_inventory.object_catalog(context.destination_credentials)
    if not catalog:
        report.warnings.append(
            "The Salesforce object list could not be read, so whether a parent can be "
            "created through the API is unknown."
        )
    if counter.provider is None:
        report.warnings.append(f"No source provider for '{context.source_type}'.")

    via_of = _via_map(context, selected)

    for name in context.mapping_names:
        config = context.configs[name]
        obj = context.destination_object(name)
        table = context.driving_table(name)
        fields = context.destination_fields(obj)
        if not fields:
            report.warnings.append(f"{obj} could not be described, so {name} was not checked.")
            continue
        source_fields = context.source_fields(table) if table else []
        for meta in fields:
            if not meta.get("required") or not meta.get("createable"):
                continue
            requirement = _classify(
                context, name, obj, table, meta, config, source_fields, counter, catalog,
                set(selected),
            )
            requirement.in_run = name in set(selected)
            requirement.via = via_of.get(name, [])
            report.requirements.append(requirement)

    report.capacity = _user_capacity(context, counter)
    logger.info(
        "[readiness] %d requirement(s) across %d mapping(s), %d gap(s)",
        len(report.requirements),
        len(context.mapping_names),
        len(report.gaps),
    )
    return report


def _via_map(context: MigrationContext, selected: list[str]) -> dict[str, list[str]]:
    """For each chain mapping, the required lookups that pull it in."""
    via: dict[str, list[str]] = {}
    for name in context.mapping_names:
        obj = context.destination_object(name)
        for meta in context.reference_fields(obj):
            if not meta.get("required"):
                continue
            for target in meta.get("reference_to") or []:
                owner = context.mapping_for_destination_object(target)
                if owner and owner not in set(selected) and owner != name:
                    via.setdefault(owner, []).append(f"{obj}.{meta['name']}")
    return via


def _classify(
    context: MigrationContext,
    mapping: str,
    obj: str,
    table: str,
    meta: dict,
    config: dict,
    source_fields: list[dict],
    counter: _Counter,
    catalog: dict[str, dict],
    selected: set[str],
) -> Requirement:
    entry = wiring.field_entry(config, meta["name"])
    requirement = Requirement(
        mapping=mapping,
        object=obj,
        field=meta["name"],
        label=meta.get("label", ""),
        type=meta.get("type", ""),
        source_table=table,
        source_field=str((entry or {}).get("source_field") or ""),
        value_source=value_source(entry),
        reference_to=list(meta.get("reference_to") or []),
    )
    if meta.get("type") == "reference":
        return _classify_lookup(context, requirement, entry, catalog, selected)

    if requirement.value_source in ("constant", "env_constant"):
        requirement.status = STATUS_COVERED
        requirement.detail = "Filled by a constant."
        return requirement

    if requirement.value_source == "source":
        columns = [c.strip() for c in requirement.source_field.split(",") if c.strip()]
        missing = [c for c in columns if table and not context.has_source_field(table, c)]
        if missing:
            requirement.status = STATUS_NO_SOURCE
            requirement.detail = f"Mapped from {', '.join(missing)}, which is not a column on {table}."
            requirement.candidates = find_candidates(meta, source_fields)
            return requirement
        requirement.total_rows = counter.total(table)
        if len(columns) == 1:
            requirement.empty_rows = counter.empty(table, columns[0])
        if _has_fallback(entry):
            requirement.status = STATUS_COVERED
            requirement.detail = "Mapped, with a fallback for empty source rows."
        elif requirement.empty_rows > 0:
            requirement.status = STATUS_PARTIAL
            share = (
                f" ({requirement.empty_rows * 100 // requirement.total_rows}% of rows)"
                if requirement.total_rows > 0
                else ""
            )
            requirement.detail = (
                f"{requirement.empty_rows} source row(s){share} have no "
                f"{columns[0]}, and Salesforce will reject each of them."
            )
        else:
            requirement.status = STATUS_COVERED
            requirement.detail = (
                "Mapped; every source row has a value."
                if requirement.empty_rows == 0
                else "Mapped; how many source rows are empty could not be counted."
            )
        return requirement

    requirement.candidates = find_candidates(meta, source_fields)
    requirement.total_rows = counter.total(table)
    if requirement.candidates:
        requirement.status = STATUS_UNMAPPED_WITH_CANDIDATE
        requirement.detail = (
            f"Required and not mapped. {table} has "
            f"{', '.join(requirement.candidates)} that may hold it."
        )
    else:
        requirement.status = STATUS_NO_SOURCE
        requirement.detail = (
            f"Required and not mapped, and nothing on {table or 'the source'} looks like it."
        )
    return requirement


def _classify_lookup(
    context: MigrationContext,
    requirement: Requirement,
    entry: dict | None,
    catalog: dict[str, dict],
    selected: set[str],
) -> Requirement:
    targets = [t for t in requirement.reference_to if t]
    creds = context.destination_credentials

    if requirement.value_source in ("constant", "env_constant"):
        requirement.status = STATUS_COVERED
        requirement.detail = "Pinned to one existing record."
        requirement.parent_object = targets[0] if targets else ""
        return requirement

    transform = (entry or {}).get("transformation_function") or ""
    params = (entry or {}).get("transformation_params") or {}
    if transform in ("generic_lookup", "generic_value_map") and requirement.value_source == "source":
        tables = params.get("lookup_tables") or []
        in_run = [t for t in targets if context.mapping_for_destination_object(t) in selected]
        if transform == "generic_value_map" or in_run or _id_rows(tables):
            requirement.status = STATUS_COVERED
            requirement.parent_object = (in_run or targets or [""])[0]
            requirement.parent_in_run = bool(in_run)
            requirement.detail = (
                "Mapped from source values to existing records."
                if transform == "generic_value_map"
                else "Resolved from migrated parent ids."
            )
            return requirement

    # Best parent first: one this run loads, one a mapping could load, one the
    # API can create, and only then one that must already exist.
    def rank(target: str) -> tuple[int, str]:
        if context.mapping_for_destination_object(target):
            return (0, target)
        if context.available_mappings_for_object(target):
            return (1, target)
        if catalog.get(target, {}).get("createable"):
            return (2, target)
        return (3, target)

    ordered = sorted(targets, key=rank)
    parent = ordered[0] if ordered else ""
    requirement.parent_object = parent
    info = catalog.get(parent)
    requirement.parent_createable = bool(info.get("createable")) if info else None
    owner = context.mapping_for_destination_object(parent)
    in_run_mapping = owner if owner in selected else ""
    # A chain mapping sits in the context so its own requirements can be
    # checked, but it is not part of the run until the person adds it.
    available = [owner] if owner and not in_run_mapping else []
    available += [m for m in context.available_mappings_for_object(parent) if m not in available]
    requirement.parent_mappings = [in_run_mapping] if in_run_mapping else available
    requirement.parent_in_run = bool(in_run_mapping)

    how = "is mapped as a raw source value" if requirement.value_source == "source" else "is not mapped"
    if in_run_mapping or available:
        requirement.status = STATUS_PARENT_MIGRATABLE
        where = (
            f"{in_run_mapping} loads it in this run"
            if in_run_mapping
            else f"{', '.join(available)} could load it but is not in this run"
        )
        requirement.detail = (
            f"Required lookup onto {parent} that {how}. {where}, so it should resolve "
            "from the migrated ids."
        )
        return requirement

    requirement.parent_existing_records = sf_inventory.count_records(parent, creds)
    existing = requirement.parent_existing_records
    existing_text = (
        f"{existing} {parent} record(s) already exist"
        if existing >= 0
        else f"existing {parent} records could not be counted"
    )
    if requirement.parent_createable:
        requirement.status = STATUS_PARENT_CREATABLE
        requirement.detail = (
            f"Required lookup onto {parent} that {how}, and nothing migrates {parent}. "
            f"It can be created through the API; {existing_text}."
        )
    else:
        requirement.status = STATUS_PARENT_NOT_CREATABLE
        requirement.detail = (
            f"Required lookup onto {parent} that {how}. {parent} cannot be created "
            f"through the API, so it has to point at a record that exists; {existing_text}."
        )
    return requirement


def _user_capacity(context: MigrationContext, counter: _Counter) -> list[dict]:
    """Whether the org has the seats for the users this migration would create."""
    capacity: list[dict] = []
    for name in context.mapping_names:
        if context.destination_object(name) != "User":
            continue
        config = context.configs[name]
        if (config.get("operation") or "insert") not in ("insert", "upsert"):
            continue
        table = context.driving_table(name)
        source_rows = counter.total(table)
        licences = sf_inventory.user_license_capacity(context.destination_credentials)
        if not licences:
            capacity.append(
                {
                    "mapping": name,
                    "source_rows": source_rows,
                    "licence": "",
                    "available": -1,
                    "short": False,
                    "detail": "User licences could not be read, so capacity is unknown.",
                }
            )
            continue
        profile = wiring.field_entry(config, "ProfileId") or {}
        profile_id = str(profile.get("constant_value") or "")
        licence_name = ""
        if profile_id:
            by_profile = sf_inventory.profile_licenses(context.destination_credentials)
            licence_name = next(
                (value for key, value in by_profile.items() if key[:15] == profile_id[:15]),
                "",
            )
        if not licence_name:
            # Without a pinned profile the licence is unknown. A full Salesforce
            # seat is what an ordinary user takes; the large Chatter pools would
            # make any number of users look affordable.
            licence_name = next(
                (l["name"] for l in licences if l["name"] == "Salesforce"), ""
            )
        chosen = [l for l in licences if l["name"] == licence_name]
        available = chosen[0]["available"] if chosen else -1
        short = source_rows > available >= 0
        capacity.append(
            {
                "mapping": name,
                "source_rows": source_rows,
                "licence": licence_name or "any",
                "available": available,
                "licences": licences,
                "short": short,
                "detail": (
                    f"{source_rows} source user(s) but only {available} "
                    f"{licence_name or 'licence'} seat(s) are free."
                    if short
                    else f"{available} {licence_name or 'licence'} seat(s) free for "
                    f"{source_rows} source user(s)."
                ),
            }
        )
    return capacity
