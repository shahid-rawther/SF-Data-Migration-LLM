"""The data readiness agent.

Runs before a migration starts. :mod:`agents.readiness` works out, without a
model, which required destination fields and parents the source cannot fill.
This agent then investigates each gap and suggests what to do: add the mapping
that loads a missing parent, change a mapping, make a Salesforce change the
load depends on, fill the missing values in the source system, or — where
none of that is possible — tell the person what they have to do themselves.

It proposes; it never applies. Every suggestion is validated and stored, and
:func:`apply_suggestion` executes one only when a person asks for it,
re-validating first. Source writes are journalled in ``agent_source_changes``,
Salesforce writes in ``agent_sf_changes`` and mapping writes in
``agent_changes``, so every part can be reviewed and, except created fields,
reverted.

When the model is unavailable the deterministic suggestions are stored alone,
so the check still says what is missing and what can be done about it.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from common.logger import logger

from agents import audit, readiness, readiness_store, sf_actions, source_actions, wiring
from agents import resolutions as resolution_rules
from agents.bedrock_client import AgentError, build_client, run_tool_loop
from agents.config import PURPOSE_RELATIONSHIP, AgentSettings, load_settings
from agents.context import MigrationContext
from agents.tools import SUBMIT_READINESS_TOOL, ToolDispatcher, build_readiness_tools

SYSTEM_PROMPT = """\
You check whether a data migration's source system can supply everything the \
destination Salesforce org requires, before anything is extracted. A \
deterministic pass has already classified every required destination field; \
read it with get_readiness_report. Investigate the gaps, then submit_readiness \
once.

Decide each gap by what the parent or the data actually is:

- A required lookup whose parent a mapping loads (parent_migratable): if that \
mapping is not in the run, suggest include_mappings with it, and a \
lookup_reference resolution when the field is unmapped or mapped as a raw id. \
The chain matters: adding User may itself need a Profile — say so.
- A required lookup onto an object nothing migrates but the API can create \
(parent_creatable): first look for a suitable existing record with \
query_destination_records and pin to it with constant_reference. Only if none \
fits, propose create_record for one default record (supplying every field the \
object requires — check with describe_destination_object filter='required') \
and a constant_reference whose constant_value is {{action:0}}.
- A required lookup onto an object the API cannot create \
(parent_not_creatable) — UserLicense, Profile, Organization, BusinessHours and \
the like: never propose creating it. Query the existing records. Use \
constant_reference to one of them, or record_value_map to translate source \
values into their ids (e.g. ServiceNow role -> Profile id). If nothing \
suitable exists, the suggestion has manual_steps only.
- User licences: when the report shows capacity is short, give manual_steps \
with concrete alternatives — migrate only active users, load people as \
Contacts instead of Users, assign records to one integration user with \
constant_reference, or have an administrator add licences.
- A required field that is partly empty (partial): count_source_empty to size \
it, then suggest a fallback — type_coercion with generic_default_if_empty \
or generic_rules. Several source columns go comma-separated in source_field, \
first choice first; transform_params only takes the keys that transform reads \
(generic_default_if_empty: default; generic_truncate: max_length, suffix).
- A required field with a likely source column (unmapped_with_candidate): \
propose mapping it with type_coercion or value_map as appropriate, after \
checking the column with describe_source_table and count_source_empty.
- Source data with no destination field: create_custom_field, then map it.
- Anything that needs a validation rule, a picklist value or a licence \
change is a manual step; you cannot make those changes.

Decide where each fix belongs — the mapping, Salesforce, or the source \
system — and say why:
- Fix the mapping when only Salesforce needs the value (a default Subject, \
a pinned owner). It leaves both systems as they are.
- Fix Salesforce (salesforce_actions) when the org lacks something the data \
needs: a parent record to point at, or a field for source data with no home.
- Fix the source (source_actions, fill_source_values) when the source record \
is itself incomplete and should be corrected there too — incidents with no \
caller, a category left blank — and one value is right for all of the empty \
rows. For a reference column find the record with query_source_records and \
write its sys_id. It writes only rows where the column is empty, is capped, \
and can be undone; still, it changes the live source, so prefer a mapping \
fix unless the data is genuinely wrong at source, and say so. A source fix \
only fills data: when the field is not mapped yet, put the resolution that \
maps it in the same suggestion, so applying it closes the gap.
Where two of these are reasonable, offer both, ranked.

Best suggestion first; one to three per gap. Every mapping change must be a \
resolution; every Salesforce change a salesforce_action; every source change \
a source_action. Nothing you submit is applied until a person approves it, so \
state trade-offs plainly.\
"""


@dataclass
class ReadinessResult:
    id: int | None = None
    report: dict = field(default_factory=dict)
    summary: str = ""
    gaps: list[dict] = field(default_factory=list)
    model_invoked: bool = False
    error: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "report": self.report,
            "summary": self.summary,
            "gaps": self.gaps,
            "applied": {},
            "sf_changes": [],
            "source_changes": [],
            "model_invoked": self.model_invoked,
            "error": self.error,
            "usage": self.usage,
        }


# ── context ────────────────────────────────────────────────────────────────────


def _extend(context: MigrationContext, names: list[str]) -> MigrationContext:
    """The same context with more mappings in it, keeping the describes it has."""
    extra = [n for n in names if n and n not in context.mapping_names]
    if not extra:
        return context
    wider = MigrationContext.load(
        context.source_type,
        [*context.mapping_names, *extra],
        context.source_credentials,
        context.destination_credentials,
    )
    wider._destination_cache.update(context._destination_cache)
    wider._source_cache.update(context._source_cache)
    return wider


def build_context(
    source_type: str,
    mapping_names: list[str],
    source_credentials: dict,
    destination_credentials: dict,
    extra: list[str] | None = None,
) -> MigrationContext:
    context = MigrationContext.load(
        source_type, mapping_names, source_credentials, destination_credentials
    )
    if extra is None:
        extra = readiness.expand_chain(context)
    return _extend(context, extra)


# ── check ──────────────────────────────────────────────────────────────────────


def check(
    source_type: str,
    mapping_names: list[str],
    source_credentials: dict,
    destination_credentials: dict,
    settings: AgentSettings | None = None,
) -> ReadinessResult:
    """Classify, investigate and store a readiness check for a selection."""
    settings = settings or load_settings()
    context = build_context(
        source_type, mapping_names, source_credentials, destination_credentials
    )
    selected = [n for n in mapping_names if n in context.mapping_names]
    report = readiness.analyse(context, selected)
    report_dict = report.to_dict()

    result = ReadinessResult(report=report_dict)
    submitted: dict[str, dict] = {}
    if report.gaps or any(c.get("short") for c in report.capacity):
        try:
            loop = _run(context, report_dict, settings)
            result.model_invoked = True
            result.usage = loop.usage
            submission = loop.submission or {}
            result.summary = str(submission.get("summary") or loop.final_text or "").strip()
            for gap in submission.get("gaps") or []:
                if isinstance(gap, dict) and gap.get("key"):
                    submitted[str(gap["key"])] = gap
            if not submission:
                raise AgentError(
                    f"the model finished without submitting (stop reason: {loop.stop_reason})"
                )
        except AgentError as exc:
            logger.warning("[readiness] model pass skipped: %s", exc)
            result.error = str(exc)

    includes = [
        name
        for gap in submitted.values()
        for suggestion in gap.get("suggestions") or []
        if isinstance(suggestion, dict)
        for name in suggestion.get("include_mappings") or []
    ]
    context = _extend(context, _known_mappings(includes))
    result.gaps = _assemble_gaps(context, report, submitted)
    if not result.summary:
        result.summary = _fallback_summary(report)

    result.id = readiness_store.save_readiness(
        source_type=source_type,
        mapping_names=selected,
        report=report_dict,
        summary=result.summary,
        gaps=result.gaps,
        model_invoked=result.model_invoked,
        error=result.error,
        usage=result.usage,
    )
    return result


def _run(context: MigrationContext, report: dict, settings):
    client = build_client(PURPOSE_RELATIONSHIP, settings)
    dispatcher = ToolDispatcher(context, readiness=report)
    gaps = [r for r in report["requirements"] if r["is_gap"]]
    prompt = (
        f"Source system: {context.source_type}\n"
        f"Mappings selected for this run: {report['selected']}\n"
        f"Mappings outside the run that the selection depends on: "
        f"{report['chain_mappings']}\n\n"
        f"Gaps found by the deterministic check ({len(gaps)}):\n"
        + json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "key", "status", "object", "field", "type", "detail",
                        "reference_to", "parent_object", "parent_mappings",
                        "candidates", "via", "in_run",
                    )
                }
                for r in gaps
            ],
            indent=2,
        )
        + (
            f"\n\nUser licence capacity:\n{json.dumps(report['capacity'], indent=2)}"
            if report["capacity"]
            else ""
        )
        + "\n\nInvestigate, then call submit_readiness once. For a capacity problem use "
        "the key 'capacity:<mapping>'."
    )
    return run_tool_loop(
        client,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        tools=build_readiness_tools(),
        dispatch=dispatcher,
        submit_tool=SUBMIT_READINESS_TOOL,
    )


def _known_mappings(names: list[str]) -> list[str]:
    wanted = list(dict.fromkeys(n for n in names if n))
    return [n for n in wanted if n in wiring.load_configs(wanted)]


def _assemble_gaps(
    context: MigrationContext,
    report: readiness.ReadinessReport,
    submitted: dict[str, dict],
) -> list[dict]:
    gaps: list[dict] = []
    for requirement in report.gaps:
        model_gap = submitted.get(requirement.key) or {}
        raw = [s for s in model_gap.get("suggestions") or [] if isinstance(s, dict)]
        suggestions = [validate_suggestion(context, s) for s in raw]
        if not suggestions:
            suggestions = [
                validate_suggestion(context, s)
                for s in deterministic_suggestions(requirement, context)
            ]
        gaps.append(
            {
                "key": requirement.key,
                "mapping": requirement.mapping,
                "object": requirement.object,
                "field": requirement.field,
                "status": requirement.status,
                "in_run": requirement.in_run,
                "via": requirement.via,
                "detail": requirement.detail,
                "analysis": str(model_gap.get("analysis") or requirement.detail),
                "suggestions": suggestions,
                "source": "agent" if raw else "rules",
            }
        )
    for capacity in report.capacity:
        if not capacity.get("short"):
            continue
        key = f"capacity:{capacity['mapping']}"
        model_gap = submitted.get(key) or {}
        raw = [s for s in model_gap.get("suggestions") or [] if isinstance(s, dict)]
        suggestions = [validate_suggestion(context, s) for s in raw] or [
            validate_suggestion(context, s) for s in _capacity_suggestions(capacity)
        ]
        gaps.append(
            {
                "key": key,
                "mapping": capacity["mapping"],
                "object": "User",
                "field": "",
                "status": "capacity",
                "in_run": capacity["mapping"] in report.selected,
                "via": [],
                "detail": capacity["detail"],
                "analysis": str(model_gap.get("analysis") or capacity["detail"]),
                "suggestions": suggestions,
                "source": "agent" if raw else "rules",
            }
        )
    return gaps


# ── suggestions ────────────────────────────────────────────────────────────────


_TEXT_TYPES = {"string", "textarea"}
_DEFAULT_TEXT = "Not provided"


def _existing_parent(requirement: readiness.Requirement, context: MigrationContext | None) -> dict:
    """The first existing record a required lookup could be pinned to, if any."""
    if context is None or not requirement.parent_object:
        return {}
    from services.internal import sf_inventory

    try:
        page = sf_inventory.sample_records(
            requirement.parent_object, context.destination_credentials, limit=1
        )
    except Exception:  # noqa: BLE001 — the suggestion falls back to a manual step
        return {}
    records = page.get("records") or []
    return records[0] if records else {}


def deterministic_suggestions(
    requirement: readiness.Requirement, context: MigrationContext | None = None
) -> list[dict]:
    """What can be said about a gap without the model.

    With a context, the suggestions that can be made concrete without judgement
    are: a default for an empty text field, mapping the one obvious candidate
    column, and pinning a lookup to the only kind of record that can exist.
    """
    status = requirement.status
    target = f"{requirement.object}.{requirement.field}"
    text = requirement.type in _TEXT_TYPES
    if status in (readiness.STATUS_PARENT_NOT_CREATABLE, readiness.STATUS_PARENT_CREATABLE):
        record = _existing_parent(requirement, context)
        if record.get("id"):
            label = record.get("name") or record["id"]
            pinned = {
                "title": f"Pin {target} to the existing {requirement.parent_object} \"{label}\"",
                "explanation": (
                    f"Every {requirement.object} loaded by {requirement.mapping} will point at "
                    f"{label}. Pick a different {requirement.parent_object} in the field "
                    "mapper if this one is not right."
                ),
                "resolutions": [
                    {
                        "kind": wiring.KIND_CONSTANT_REFERENCE,
                        "mapping": requirement.mapping,
                        "field": requirement.field,
                        "constant_value": record["id"],
                        "rationale": f"{target} must point at an existing {requirement.parent_object}.",
                        "severity": "blocker",
                    }
                ],
            }
            return [pinned, *_static_suggestions(requirement, target)]
    if status == readiness.STATUS_PARTIAL and text:
        column = requirement.source_field.split(",")[0].strip()
        suggestions = [
            {
                "title": f"Default {target} to \"{_DEFAULT_TEXT}\" when the source is empty",
                "explanation": (
                    requirement.detail
                    + f" The mapping writes \"{_DEFAULT_TEXT}\" for those rows; ServiceNow is "
                    "not changed."
                ),
                "resolutions": [
                    {
                        "kind": wiring.KIND_TYPE_COERCION,
                        "mapping": requirement.mapping,
                        "field": requirement.field,
                        "source_field": requirement.source_field,
                        "transform": "generic_default_if_empty",
                        "transform_params": {"default": _DEFAULT_TEXT},
                        "rationale": f"{target} is required and some source rows are empty.",
                        "severity": "blocker",
                    }
                ],
            }
        ]
        if column and "," not in requirement.source_field and requirement.source_table:
            suggestions.append(
                {
                    "title": f"Fill the empty {column.split('.')[-1]} values in the source",
                    "explanation": (
                        f"Writes \"{_DEFAULT_TEXT}\" into {requirement.source_table}."
                        f"{column.split('.')[-1]} on the {max(requirement.empty_rows, 0)} "
                        "record(s) where it is empty, so the source and Salesforce agree. "
                        "Only empty values are written, and the change can be undone."
                    ),
                    "source_actions": [
                        {
                            "kind": source_actions.ACTION_FILL_SOURCE_VALUES,
                            "table": requirement.source_table,
                            "field": column,
                            "value": _DEFAULT_TEXT,
                            "rationale": f"{target} is required and these source rows are empty.",
                        }
                    ],
                }
            )
        return suggestions
    if status == readiness.STATUS_UNMAPPED_WITH_CANDIDATE and text and requirement.candidates:
        candidate = requirement.candidates[0]
        source = (
            candidate
            if "." in candidate or not requirement.source_table
            else f"{requirement.source_table}.{candidate}"
        )
        return [
            {
                "title": f"Map {target} from {candidate}",
                "explanation": (
                    requirement.detail
                    + f" Rows where {candidate} is empty get \"{_DEFAULT_TEXT}\"."
                ),
                "resolutions": [
                    {
                        "kind": wiring.KIND_TYPE_COERCION,
                        "mapping": requirement.mapping,
                        "field": requirement.field,
                        "source_field": source,
                        "transform": "generic_default_if_empty",
                        "transform_params": {"default": _DEFAULT_TEXT},
                        "rationale": f"{target} is required and {candidate} is the closest source column.",
                        "severity": "blocker",
                    }
                ],
            },
        ]
    return _static_suggestions(requirement, target)


def _static_suggestions(requirement: readiness.Requirement, target: str) -> list[dict]:
    status = requirement.status
    if status == readiness.STATUS_PARENT_MIGRATABLE and requirement.parent_mappings:
        parent = requirement.parent_mappings[0]
        resolutions = []
        # Without a source column there is no id to resolve; the model, or the
        # person, has to choose which column carries it.
        if requirement.value_source == "source" and requirement.source_field:
            resolutions.append(
                {
                    "kind": wiring.KIND_LOOKUP_REFERENCE,
                    "mapping": requirement.mapping,
                    "field": requirement.field,
                    "parent_mappings": [parent],
                    "source_field": requirement.source_field,
                    "rationale": (
                        f"{target} must resolve to a migrated "
                        f"{requirement.parent_object} id."
                    ),
                    "severity": "blocker",
                }
            )
        if requirement.parent_in_run:
            return [
                {
                    "title": f"Resolve {target} from {parent}",
                    "explanation": (
                        f"{parent} already loads {requirement.parent_object} in this run, "
                        "so the lookup can be resolved from its migrated ids."
                    ),
                    "resolutions": resolutions,
                }
            ]
        return [
            {
                "title": f"Add {parent} to this migration",
                "explanation": (
                    f"{target} requires a {requirement.parent_object}, and {parent} is the "
                    f"mapping that loads it. Adding it means {requirement.parent_object} "
                    "records load first; check its own requirements below."
                ),
                "include_mappings": [parent],
                "resolutions": resolutions,
            }
        ]
    if status == readiness.STATUS_PARENT_NOT_CREATABLE:
        return [
            {
                "title": f"Point {target} at an existing {requirement.parent_object}",
                "explanation": (
                    f"{requirement.parent_object} cannot be created through the API. Pick "
                    "an existing record in the field mapper and pin the field to it, or "
                    "map source values to existing records."
                ),
                "manual_steps": [
                    f"In the field mapper, set {requirement.field} on {requirement.mapping} "
                    f"to a constant {requirement.parent_object} record.",
                    f"If none is suitable, ask a Salesforce administrator to create the "
                    f"{requirement.parent_object} in Setup.",
                ],
            }
        ]
    if status == readiness.STATUS_PARENT_CREATABLE:
        return [
            {
                "title": f"Pin {target} to one {requirement.parent_object}",
                "explanation": (
                    f"Nothing migrates {requirement.parent_object}. Point the lookup at an "
                    "existing record, or run the check with the agent available to have "
                    "it propose creating a default one."
                ),
                "manual_steps": [
                    f"Choose a {requirement.parent_object} in the field mapper and set "
                    f"{requirement.field} to it as a constant.",
                ],
            }
        ]
    if status == readiness.STATUS_PARTIAL:
        return [
            {
                "title": f"Give {target} a fallback",
                "explanation": requirement.detail
                + " Add a default or a second source column so no row is left empty.",
                "manual_steps": [
                    f"Wrap {requirement.field} in 'default if empty' in the field mapper.",
                ],
            }
        ]
    if status == readiness.STATUS_UNMAPPED_WITH_CANDIDATE:
        return [
            {
                "title": f"Map {target}",
                "explanation": requirement.detail,
                "manual_steps": [
                    f"Map {requirement.field} from {requirement.candidates[0]} in the field "
                    "mapper, or set a constant.",
                ],
            }
        ]
    return [
        {
            "title": f"Supply a value for {target}",
            "explanation": requirement.detail,
            "manual_steps": [
                f"Set {requirement.field} to a constant, or add the data to "
                f"{requirement.source_table or 'the source'} before migrating.",
            ],
        }
    ]


def _capacity_suggestions(capacity: dict) -> list[dict]:
    return [
        {
            "title": "Reduce the users that need a licence",
            "explanation": capacity["detail"],
            "manual_steps": [
                "Migrate only active users (add an active=true filter to the user mapping).",
                "Load people who never log in as Contacts instead of Users.",
                "Assign migrated records to one integration user with a constant owner.",
                "Ask a Salesforce administrator to add licences.",
            ],
        }
    ]


def _created_fields(actions: list[dict]) -> set[tuple[str, str]]:
    return {
        (a["object"], a["field_api_name"])
        for a in actions
        if a.get("kind") == sf_actions.ACTION_CREATE_CUSTOM_FIELD
    }


def validate_suggestion(context: MigrationContext, raw: dict) -> dict:
    """Keep every suggestion's text; keep only the changes that validate.

    A suggestion is applicable only when everything in it survived — applying
    half of one could create a record and then fail to point anything at it.
    """
    include = [str(n) for n in raw.get("include_mappings") or [] if n]
    known = set(_known_mappings(include))
    rejected: list[dict] = [
        {"include_mapping": name, "reason": f"No mapping named '{name}' exists"}
        for name in include
        if name not in known
    ]

    source_outcome = source_actions.validate(context, raw.get("source_actions") or [])
    rejected.extend(source_outcome.rejected)
    outcome = sf_actions.validate(context, raw.get("salesforce_actions") or [])
    rejected.extend(outcome.rejected)
    record_positions = {
        i for i, a in enumerate(outcome.accepted) if a["kind"] == sf_actions.ACTION_CREATE_RECORD
    }
    new_fields = _created_fields(outcome.accepted)

    accepted_resolutions: list[dict] = []
    deferred: list[dict] = []
    for item in raw.get("resolutions") or []:
        if not isinstance(item, dict):
            continue
        item = _qualify_source(context, item)
        refs = sf_actions.placeholders_in([item])
        if refs - record_positions:
            rejected.append(
                {"resolution": item, "reason": "refers to a record no action in it creates"}
            )
            continue
        target = (context.destination_object(str(item.get("mapping") or "")), str(item.get("field") or ""))
        if target in new_fields:
            # The field does not exist until the action runs; it is checked then.
            deferred.append(item)
            continue
        check = resolution_rules.validate(context, [item])
        if check.accepted:
            accepted_resolutions.append(item)
        else:
            rejected.extend(check.rejected)

    resolutions = [*accepted_resolutions, *deferred]
    actions = outcome.accepted
    manual = [str(s).strip() for s in raw.get("manual_steps") or [] if str(s).strip()]
    changes = bool(known or actions or resolutions or source_outcome.accepted)
    return {
        "title": str(raw.get("title") or "").strip() or _fallback_title(raw),
        "explanation": str(raw.get("explanation") or "").strip(),
        "include_mappings": sorted(known),
        "source_actions": source_outcome.accepted,
        "salesforce_actions": actions,
        "resolutions": resolutions,
        "manual_steps": manual,
        "rejected": rejected,
        "applicable": changes and not rejected,
        "needs_person": bool(manual) and not changes,
    }


def _qualify_source(context: MigrationContext, item: dict) -> dict:
    """Write source columns as ``driving_table.column``, the form mappings use.

    The model names columns bare or by the table they are inherited from
    ("task.short_description" on incident). Extraction reads the prefix as the
    table to select from, so anything but the mapping's own table would miss.
    """
    from services.internal.mappings_service import cfg_source_tables

    source = str(item.get("source_field") or "")
    mapping = str(item.get("mapping") or "")
    table = context.driving_table(mapping) if source and mapping in context.configs else ""
    if not table:
        return item
    own = set(cfg_source_tables(context.configs[mapping])) | {table}

    def qualify(column: str) -> str:
        prefix, _, name = column.rpartition(".")
        return column if prefix in own else f"{table}.{name or column}"

    columns = [qualify(c.strip()) for c in source.split(",") if c.strip()]
    return {**item, "source_field": ",".join(columns)}


def _fallback_title(raw: dict) -> str:
    """A heading for a suggestion the model left untitled, from what it does."""
    explanation = str(raw.get("explanation") or "").strip()
    if explanation:
        first = explanation.split(". ")[0].rstrip(".")
        return first if len(first) <= 90 else first[:87].rstrip() + "..."
    for item in raw.get("resolutions") or []:
        if isinstance(item, dict) and item.get("field"):
            return f"{str(item.get('kind') or 'change').replace('_', ' ').capitalize()} on {item['field']}"
    if raw.get("include_mappings"):
        return f"Add {', '.join(map(str, raw['include_mappings']))} to the run"
    return "Suggested change"


def _fallback_summary(report: readiness.ReadinessReport) -> str:
    if not report.gaps:
        return "Every required destination field has a source, and every required parent can be loaded."
    counts: dict[str, int] = {}
    for gap in report.gaps:
        counts[gap.status] = counts.get(gap.status, 0) + 1
    parts = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in sorted(counts.items()))
    return f"{len(report.gaps)} required field(s) cannot be filled as mapped: {parts}."


# ── apply ──────────────────────────────────────────────────────────────────────


def apply_suggestion(
    readiness_id: int,
    gap_index: int,
    suggestion_index: int,
    source_credentials: dict,
    destination_credentials: dict,
) -> dict:
    """Run one approved suggestion: source writes, Salesforce actions, then mappings.

    Everything is re-validated against both systems as they are now. If a
    later step fails, the source values written and the Salesforce records
    created by the earlier ones are put back.
    """
    stored = readiness_store.get_readiness(readiness_id)
    if not stored:
        raise KeyError(f"No readiness check with id {readiness_id}")
    try:
        gap = stored["gaps"][gap_index]
        suggestion = gap["suggestions"][suggestion_index]
    except (IndexError, KeyError, TypeError) as exc:
        raise KeyError(f"Readiness check {readiness_id} has no suggestion {gap_index}:{suggestion_index}") from exc
    previous = stored["applied"].get(f"{gap_index}:{suggestion_index}")
    if previous and not previous.get("reverted"):
        raise ValueError("This suggestion has already been applied")
    if not suggestion.get("applicable"):
        raise ValueError("This suggestion needs a person to act on it; there is nothing to apply")

    names = list(
        dict.fromkeys(
            [
                *stored["mapping_names"],
                *(stored["report"].get("chain_mappings") or []),
                *suggestion.get("include_mappings", []),
            ]
        )
    )
    context = build_context(
        stored["source_type"], names, source_credentials, destination_credentials, extra=[]
    )

    source_outcome = source_actions.validate(context, suggestion.get("source_actions") or [])
    if source_outcome.rejected:
        reason = "; ".join(r["reason"] for r in source_outcome.rejected)
        raise ValueError(f"The source change no longer validates: {reason}")
    outcome = sf_actions.validate(context, suggestion.get("salesforce_actions") or [])
    if outcome.rejected:
        reason = "; ".join(r["reason"] for r in outcome.rejected)
        raise ValueError(f"The Salesforce change no longer validates: {reason}")
    new_fields = _created_fields(outcome.accepted)
    planned = [_qualify_source(context, item) for item in suggestion.get("resolutions") or []]
    for item in planned:
        target = (context.destination_object(str(item.get("mapping") or "")), str(item.get("field") or ""))
        if target in new_fields:
            continue
        check = resolution_rules.validate(context, [item])
        if check.rejected:
            reason = "; ".join(r["reason"] for r in check.rejected)
            raise ValueError(f"The mapping change no longer validates: {reason}")

    run_id = uuid.uuid4().hex
    source_results = source_actions.execute(source_outcome.accepted, context)
    source_change_ids = [
        readiness_store.record_source_change(
            readiness_id=readiness_id,
            run_id=run_id,
            source_type=context.source_type,
            kind=r["kind"],
            table_name=r["table"],
            field_name=r["field"],
            value=r["value"],
            record_ids=r["record_ids"],
            rationale=source_outcome.accepted[r["index"]]["rationale"],
        )
        for r in source_results
    ]

    def restore_source() -> None:
        for change_id, result in zip(source_change_ids, source_results):
            try:
                source_actions.restore(result, context.source_type, source_credentials)
                readiness_store.mark_source_reverted(change_id)
            except Exception:  # noqa: BLE001 — the original error is what matters
                logger.exception("[readiness] could not roll back %s.%s", result["table"], result["field"])

    try:
        results = sf_actions.execute(outcome.accepted, destination_credentials)
    except Exception:
        restore_source()
        raise
    sf_change_ids = [
        readiness_store.record_sf_change(
            readiness_id=readiness_id,
            run_id=run_id,
            kind=r["kind"],
            object_name=r["object"],
            record_id=r.get("record_id", ""),
            field_name=r.get("field", ""),
            payload=r.get("values") or {},
            rationale=outcome.accepted[r["index"]]["rationale"],
            revertible=r["revertible"],
        )
        for r in results
    ]

    try:
        if new_fields:
            # The describes cached before the field existed would hide it.
            context._destination_cache.clear()
        resolved = sf_actions.substitute(planned, sf_actions.created_ids(results))
        check = resolution_rules.validate(context, resolved)
        if check.rejected:
            reason = "; ".join(r["reason"] for r in check.rejected)
            hint = (
                " A new custom field is only visible once the integration user's "
                "profile has access to it."
                if new_fields
                else ""
            )
            raise ValueError(f"The mapping change could not be validated: {reason}.{hint}")
        draft = context.draft()
        changes, unapplied = wiring.apply_resolutions(
            draft, check.accepted, agent=wiring.AGENT_READINESS
        )
        if unapplied:
            raise ValueError("; ".join(item.get("reason", "") for item in unapplied))
        touched = sorted({c.mapping_name for c in changes if c.applied})
        if touched:
            wiring.persist(draft, touched)
        change_ids = audit.record_changes(run_id, changes)
    except Exception:
        for change_id, result in zip(sf_change_ids, results):
            if result.get("record_id"):
                try:
                    from services.internal import sf_inventory

                    sf_inventory.delete_record(
                        result["object"], result["record_id"], destination_credentials
                    )
                    readiness_store.mark_sf_reverted(change_id)
                except Exception:  # noqa: BLE001 — the original error is what matters
                    logger.exception("[readiness] could not roll back %s", result["record_id"])
        restore_source()
        raise

    outcome_record = {
        "run_id": run_id,
        "source_change_ids": source_change_ids,
        "sf_change_ids": sf_change_ids,
        "change_ids": change_ids,
        "include_mappings": suggestion.get("include_mappings", []),
    }
    readiness_store.mark_applied(readiness_id, gap_index, suggestion_index, outcome_record)
    logger.info(
        "[readiness] applied suggestion %d:%d of check %d: %d source write(s), "
        "%d Salesforce write(s), %d mapping change(s)",
        gap_index,
        suggestion_index,
        readiness_id,
        len(source_results),
        len(results),
        len(change_ids),
    )
    return {
        **outcome_record,
        "source": [
            {k: r[k] for k in ("kind", "table", "field", "value")} | {"records": len(r["record_ids"])}
            for r in source_results
        ],
        "salesforce": results,
        "changes": [c.to_dict() for c in changes],
    }


def revert_source_change(change_id: int, source_credentials: dict) -> dict:
    """Empty a column again on the records a readiness suggestion filled."""
    change = readiness_store.get_source_change(change_id)
    if not change:
        raise KeyError(f"No source change with id {change_id}")
    if change["reverted"]:
        return {"id": change_id, "reverted": True, "already": True, "restored": 0, "kept": 0}
    outcome = source_actions.restore(
        {
            "table": change["table_name"],
            "field": change["field_name"],
            "value": change["value"],
            "record_ids": change["record_ids"],
        },
        change["source_type"],
        source_credentials,
    )
    readiness_store.mark_source_reverted(change_id)
    return {"id": change_id, "reverted": True, "already": False, **outcome}


def revert_sf_change(change_id: int, destination_credentials: dict) -> dict:
    """Delete a record a readiness suggestion created."""
    from services.internal import sf_inventory

    change = readiness_store.get_sf_change(change_id)
    if not change:
        raise KeyError(f"No Salesforce change with id {change_id}")
    if change["reverted"]:
        return {"id": change_id, "reverted": True, "already": True}
    if not change["revertible"] or not change["record_id"]:
        raise ValueError(
            "Created fields are not removed automatically; delete it in Setup if it is "
            "not needed"
        )
    sf_inventory.delete_record(change["object_name"], change["record_id"], destination_credentials)
    readiness_store.mark_sf_reverted(change_id)
    return {"id": change_id, "reverted": True, "already": False}
