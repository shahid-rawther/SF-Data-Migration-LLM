"""Check mapped data against a Salesforce workflow template.

Field mapping only proves a source column can land in a destination field.
It does not prove the record will do what the org's automation expects once
it is saved. This walks each mapped object in the order a Salesforce
automation actually runs:

1. Object
2. Trigger
3. Entry Conditions
4. Decisions
5. Actions
6. Approvals
7. Notifications
8. Error Handling

The template is the description of that automation. One JSON file per
Salesforce object:

* ``data/workflow_templates/{Object}.json`` — the org's template, preferred
* ``integrations/salesforce/workflows/{Object}.json`` — a shipped template

Files whose names start with ``_`` are examples and are not applied.
A missing template does not block a load: there is nothing to check the
rows against. A template that the mapped fields or transformed rows would
break does block the load.

Before transformation the check can only see which fields are mapped.
After transformation it reads the transformed rows and applies the
conditions to those values. Live org metadata, when credentials are
supplied, is used to warn about active triggers, flows, approvals, and
validation rules the template does not describe. Flow entry criteria and
decision formulas are not readable from a simple query, so those still
come from the template.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from common.logger import logger
from config.settings import PATH_TO_TRANSFORMATION

STAGE_ORDER = (
    "object",
    "trigger",
    "entry_conditions",
    "decisions",
    "actions",
    "approvals",
    "notifications",
    "error_handling",
)

STAGE_LABELS = {
    "object": "Object",
    "trigger": "Trigger",
    "entry_conditions": "Entry Conditions",
    "decisions": "Decisions",
    "actions": "Actions",
    "approvals": "Approvals",
    "notifications": "Notifications",
    "error_handling": "Error Handling",
}

_WORKFLOW_DIR = Path(__file__).resolve().parent / "workflows"
_DATA_WORKFLOW_DIR = PATH_TO_TRANSFORMATION.parent / "workflow_templates"
_OBJECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MAX_FINDINGS = 20
_MAX_ROWS = 200

# Sentinels for a field that is mapped but has no value yet, and a field
# the mapping does not supply at all.
_UNKNOWN = object()
_MISSING = object()

_OPERATORS = {
    "equals": "equals",
    "eq": "equals",
    "=": "equals",
    "not_equals": "not_equals",
    "neq": "not_equals",
    "!=": "not_equals",
    "in": "in",
    "not_in": "not_in",
    "blank": "blank",
    "is_blank": "blank",
    "not_blank": "not_blank",
    "is_not_blank": "not_blank",
    "greater_than": "greater_than",
    "gt": "greater_than",
    "less_than": "less_than",
    "lt": "less_than",
    "contains": "contains",
}


@dataclass
class Finding:
    stage: str
    status: str  # pass | fail | warning | skipped
    name: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "status": self.status,
            "name": self.name,
            "detail": self.detail,
        }


@dataclass
class StageResult:
    stage: str
    status: str
    detail: str
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "label": STAGE_LABELS.get(self.stage, self.stage),
            "status": self.status,
            "detail": self.detail,
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass
class ObjectWorkflowResult:
    mapping: str
    salesforce_object: str
    template_found: bool
    ok: bool
    failing_rows: int
    stages: list[StageResult]
    summary: str

    def to_dict(self) -> dict:
        return {
            "mapping": self.mapping,
            "salesforce_object": self.salesforce_object,
            "template_found": self.template_found,
            "ok": self.ok,
            "failing_rows": self.failing_rows,
            "summary": self.summary,
            "stages": [stage.to_dict() for stage in self.stages],
        }


@dataclass
class WorkflowReport:
    ok: bool
    objects: list[ObjectWorkflowResult]
    summary: str

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "objects": [item.to_dict() for item in self.objects],
        }


@dataclass
class Discovery:
    """Active automation names read from the org. Empty when not queried."""

    ran: bool = False
    error: str = ""
    triggers: list[str] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    validation_rules: list[str] = field(default_factory=list)

    def known_triggers(self) -> set[str]:
        return {_norm(name) for name in (*self.triggers, *self.flows) if name}


def check_mappings(
    mapping_names: list[str],
    destination_credentials: dict | None = None,
    records_by_mapping: dict | None = None,
    use_transformed_rows: bool = False,
) -> WorkflowReport:
    """Check each mapping. Never raises; a broken mapping becomes a failed object."""
    from services.internal.mappings_service import cfg_destination_object, get_mapping_config

    records_by_mapping = records_by_mapping or {}
    objects: list[ObjectWorkflowResult] = []
    for name in mapping_names:
        try:
            config = get_mapping_config(name) or {}
            salesforce_object = cfg_destination_object(config) or name
            records = records_by_mapping.get(name)
            if records is None and use_transformed_rows:
                records = sample_transformed_rows(name)
            objects.append(
                check_mapping(
                    mapping_name=name,
                    config=config,
                    salesforce_object=salesforce_object,
                    records=records,
                    credentials=destination_credentials,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one mapping must not hide the others
            logger.warning("Workflow check failed for %s: %s", name, exc)
            objects.append(
                _empty_result(
                    name,
                    name,
                    status="fail",
                    detail=f"The workflow check could not be completed: {exc}",
                    template_found=False,
                    ok=False,
                )
            )
    blocked = [item for item in objects if not item.ok]
    if not objects:
        summary = "No mappings were selected, so no workflow was checked."
    elif blocked:
        summary = (
            f"{len(blocked)} object{'s' if len(blocked) != 1 else ''} would not follow "
            "the Salesforce workflow template."
        )
    else:
        summary = "Mapped data matches the Salesforce workflow templates that are defined."
    return WorkflowReport(ok=not blocked, objects=objects, summary=summary)


def check_mapping(
    mapping_name: str,
    config: dict,
    salesforce_object: str,
    records: list[dict] | None = None,
    credentials: dict | None = None,
    template: dict | None = None,
    discovery: Discovery | None = None,
) -> ObjectWorkflowResult:
    """Evaluate one mapping. ``template`` and ``discovery`` bypass disk and HTTP."""
    if template is None:
        loaded = load_template(salesforce_object)
        if loaded is None:
            return _empty_result(
                mapping_name,
                salesforce_object,
                status="warning",
                detail=(
                    f"No workflow template for {salesforce_object}. Add "
                    f"data/workflow_templates/{salesforce_object}.json describing the "
                    "object, trigger, entry conditions, decisions, actions, approvals, "
                    "notifications, and error handling. Mapped data was not blocked."
                ),
                template_found=False,
                ok=True,
            )
        template = loaded

    if discovery is None and _credentials_ready(credentials):
        discovery = discover_automation(credentials or {}, salesforce_object)
    discovery = discovery or Discovery()

    mapped, constants = _mapped_fields(config)
    buckets: dict[str, list[Finding]] = {stage: [] for stage in STAGE_ORDER}
    failing_rows = _evaluate(
        template=template,
        mapped=mapped,
        constants=constants,
        records=records,
        discovery=discovery,
        buckets=buckets,
    )
    stages = [_stage(stage, buckets[stage]) for stage in STAGE_ORDER]
    ok = all(stage.status != "fail" for stage in stages)
    failed = [STAGE_LABELS[stage.stage] for stage in stages if stage.status == "fail"]
    if ok:
        summary = f"{salesforce_object} follows the workflow template."
    else:
        summary = f"{salesforce_object} fails {', '.join(failed)}."
    return ObjectWorkflowResult(
        mapping=mapping_name,
        salesforce_object=salesforce_object,
        template_found=True,
        ok=ok,
        failing_rows=failing_rows,
        stages=stages,
        summary=summary,
    )


def load_template(salesforce_object: str) -> dict | None:
    """Return the org template, else the shipped one. Examples are ignored."""
    if not salesforce_object or not _OBJECT_NAME.fullmatch(salesforce_object):
        return None
    for directory in (_DATA_WORKFLOW_DIR, _WORKFLOW_DIR):
        path = directory / f"{salesforce_object}.json"
        if path.is_file() and not path.name.startswith("_"):
            return _read_template(path)
    return None


def sample_transformed_rows(mapping_name: str, limit: int = _MAX_ROWS) -> list[dict]:
    """Read up to ``limit`` transformed insert/update rows for a mapping."""
    root = PATH_TO_TRANSFORMATION / "csv" / mapping_name
    if not root.is_dir():
        return []
    paths = sorted(root.rglob("*_insert.csv")) + sorted(root.rglob("*_update.csv"))
    rows: list[dict] = []
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    rows.append(dict(row))
                    if len(rows) >= limit:
                        return rows
        except OSError as exc:
            logger.warning("Could not read transformed rows from %s: %s", path, exc)
    return rows


def discover_automation(credentials: dict, salesforce_object: str) -> Discovery:
    """Read active automation names for one object. Never raises."""
    found = Discovery(ran=True)
    if not _OBJECT_NAME.fullmatch(salesforce_object or ""):
        found.error = f"{salesforce_object!r} is not a Salesforce object name."
        return found
    try:
        token, instance, version = _salesforce_session(credentials)
    except Exception as exc:  # noqa: BLE001 — discovery is advisory
        found.error = str(exc)
        return found

    headers = {"Authorization": f"Bearer {token}"}
    queries = {
        "triggers": (
            "SELECT Name FROM ApexTrigger "
            f"WHERE Status = 'Active' AND TableEnumOrId = '{salesforce_object}'"
        ),
        "flows": (
            "SELECT ApiName FROM FlowDefinitionView "
            "WHERE IsActive = true AND "
            f"TriggerObjectOrEvent.QualifiedApiName = '{salesforce_object}'"
        ),
        "approvals": (
            "SELECT Name FROM ProcessDefinition "
            f"WHERE State = 'Active' AND TableEnumOrId = '{salesforce_object}'"
        ),
        "validation_rules": (
            "SELECT ValidationName FROM ValidationRule "
            "WHERE Active = true AND "
            f"EntityDefinition.QualifiedApiName = '{salesforce_object}'"
        ),
    }
    errors: list[str] = []
    for key, soql in queries.items():
        try:
            names = _tooling_names(instance, version, headers, soql)
        except Exception as exc:  # noqa: BLE001 — one query must not drop the rest
            errors.append(f"{key}: {exc}")
            continue
        setattr(found, key, names)
    if errors:
        found.error = "; ".join(errors)
    return found


def _evaluate(
    template: dict,
    mapped: set[str],
    constants: dict[str, str],
    records: list[dict] | None,
    discovery: Discovery,
    buckets: dict[str, list[Finding]],
) -> int:
    object_name = str(template.get("object") or "")
    required = [str(name) for name in template.get("required_fields") or []]
    triggers = list(template.get("triggers") or [])
    rules = list(template.get("error_handling") or [])
    for trigger in triggers:
        rules.extend(trigger.get("error_handling") or [])

    if records is not None and len(records) == 0:
        records = None
        buckets["object"].append(
            Finding(
                "object",
                "warning",
                "Rows",
                "No transformed rows were available, so only the mapping was checked.",
            )
        )

    _check_object(object_name, required, mapped, constants, records, buckets)
    _check_triggers(triggers, discovery, buckets)
    failing: set[int] = set()
    for trigger in triggers:
        if trigger.get("active", True) is False:
            continue
        failing.update(_check_trigger(trigger, mapped, constants, records, buckets))
    failing.update(_check_error_handling(rules, mapped, constants, records, discovery, buckets))
    _fill_quiet_stages(buckets)
    return len(failing)


def _check_object(object_name, required, mapped, constants, records, buckets) -> None:
    stage = "object"
    if not object_name:
        buckets[stage].append(Finding(stage, "fail", "Object", "The template does not name a Salesforce object."))
        return
    buckets[stage].append(
        Finding(stage, "pass", object_name, f"Records are checked as {object_name}.")
    )
    if not required:
        buckets[stage].append(
            Finding(stage, "pass", "Required fields", "The template does not require extra fields on the object.")
        )
        return
    for name in required:
        _require_field(
            stage,
            buckets,
            name,
            mapped,
            constants,
            records,
            missing=f"{name} is required on {object_name} and is not mapped.",
            blank=f"{name} is required on {object_name} and is blank.",
            unknown=f"{name} is mapped. Its value is checked on the transformed rows.",
            present=f"{name} is mapped.",
        )


def _check_triggers(triggers: list[dict], discovery: Discovery, buckets) -> None:
    stage = "trigger"
    active = [item for item in triggers if item.get("active", True) is not False]
    if not triggers:
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                "No trigger",
                "The template describes no trigger, flow, or workflow rule, so later stages have no automation to follow.",
            )
        )
    for trigger in triggers:
        name = str(trigger.get("name") or "Unnamed trigger")
        kind = str(trigger.get("type") or "automation")
        when = str(trigger.get("when") or "after_save")
        if trigger.get("active", True) is False:
            buckets[stage].append(
                Finding(stage, "skipped", name, f"{kind} is inactive, so it is not applied.")
            )
            continue
        buckets[stage].append(
            Finding(stage, "pass", name, f"Active {kind} runs {when.replace('_', ' ')}.")
        )
    if not discovery.ran:
        return
    if discovery.error:
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                "Org metadata",
                f"Salesforce automation could not be read completely: {discovery.error}",
            )
        )
    known = discovery.known_triggers()
    described = {_norm(str(item.get("name") or "")) for item in active}
    for name in (*discovery.triggers, *discovery.flows):
        if _norm(name) not in described:
            buckets[stage].append(
                Finding(
                    stage,
                    "warning",
                    name,
                    f"{name} is active in Salesforce and is not in the template, so this check cannot follow it.",
                )
            )
    if known:
        for trigger in active:
            name = str(trigger.get("name") or "")
            if name and _norm(name) not in known:
                buckets[stage].append(
                    Finding(
                        stage,
                        "warning",
                        name,
                        f"{name} is in the template but is not an active trigger or flow in this org.",
                    )
                )


def _check_trigger(trigger, mapped, constants, records, buckets) -> set[int]:
    name = str(trigger.get("name") or "Unnamed trigger")
    entry = list(trigger.get("entry_conditions") or [])
    decisions = list(trigger.get("decisions") or [])
    actions = list(trigger.get("actions") or [])
    approvals = list(trigger.get("approvals") or [])
    notifications = list(trigger.get("notifications") or [])

    if records is None:
        _check_entry_mapping(name, entry, mapped, constants, buckets)
        outcome = _check_decisions_mapping(name, decisions, mapped, constants, buckets)
        _check_gated_mapping("actions", name, actions, "requires", mapped, constants, records, buckets, outcome)
        _check_gated_mapping("approvals", name, approvals, "required_fields", mapped, constants, records, buckets, outcome)
        _check_notifications_mapping(name, notifications, mapped, constants, buckets, outcome)
        return set()

    failing: set[int] = set()
    entered = 0
    for index, row in enumerate(records):
        values = _row_values(row)
        entry_state, entry_fail = _entry_state(name, entry, values, mapped)
        if entry_fail:
            failing.add(index)
            buckets["entry_conditions"].append(
                Finding("entry_conditions", "fail", name, f"Row {index + 1}: {entry_fail}")
            )
            continue
        if entry_state is not True:
            continue
        entered += 1
        outcome, decision_fail = _decision_outcome(name, decisions, values, mapped)
        if decision_fail:
            failing.add(index)
            buckets["decisions"].append(
                Finding("decisions", "fail", name, f"Row {index + 1}: {decision_fail}")
            )
            continue
        for stage, items, key in (
            ("actions", actions, "requires"),
            ("approvals", approvals, "required_fields"),
        ):
            for item in items:
                if not _applies(item.get("when"), outcome):
                    continue
                for field_name in item.get(key) or []:
                    if _is_blank(values.get(field_name, _MISSING)):
                        failing.add(index)
                        label = str(item.get("name") or field_name)
                        buckets[stage].append(
                            Finding(
                                stage,
                                "fail",
                                label,
                                f"Row {index + 1} takes '{outcome}' on {name}, but {field_name} is blank.",
                            )
                        )
        for item in notifications:
            if not _applies(item.get("when"), outcome):
                continue
            recipient = str(item.get("recipient_field") or "")
            if recipient and _is_blank(values.get(recipient, _MISSING)):
                failing.add(index)
                buckets["notifications"].append(
                    Finding(
                        "notifications",
                        "fail",
                        str(item.get("name") or recipient),
                        f"Row {index + 1} takes '{outcome}' on {name}, but {recipient} is blank, so the notification has no recipient.",
                    )
                )
    _summarize_rows(name, entry, decisions, actions, approvals, notifications, records, entered, buckets)
    return failing


def _check_entry_mapping(name, conditions, mapped, constants, buckets) -> None:
    stage = "entry_conditions"
    if not conditions:
        buckets[stage].append(
            Finding(stage, "pass", name, f"{name} has no entry criteria, so every saved record enters it.")
        )
        return
    unknown = False
    for condition in conditions:
        field_name = str(condition.get("field") or "")
        state = _availability(field_name, mapped, constants, None)
        if state is _MISSING:
            buckets[stage].append(
                Finding(stage, "fail", name, f"{name} enters on {field_name}, which is not mapped.")
            )
        elif state is _UNKNOWN:
            unknown = True
        else:
            matched, problem = _matches(condition, state)
            if problem:
                buckets[stage].append(Finding(stage, "fail", name, f"{name}: {problem}"))
            elif not matched:
                buckets[stage].append(
                    Finding(
                        stage,
                        "warning",
                        name,
                        f"{name} does not enter when {field_name} is the mapped constant {state!r}.",
                    )
                )
    if unknown:
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                name,
                f"{name} entry depends on mapped fields whose values are checked on the transformed rows.",
            )
        )
    elif not any(item.name == name and item.status == "fail" for item in buckets[stage]):
        buckets[stage].append(
            Finding(stage, "pass", name, f"{name} entry fields are mapped.")
        )


def _check_decisions_mapping(name, decisions, mapped, constants, buckets) -> str:
    stage = "decisions"
    if not decisions:
        buckets[stage].append(Finding(stage, "pass", name, f"{name} has no decisions, so it has one path."))
        return "default"
    unknown = False
    for decision in decisions:
        label = str(decision.get("name") or decision.get("outcome") or "Decision")
        for condition in decision.get("when") or []:
            field_name = str(condition.get("field") or "")
            state = _availability(field_name, mapped, constants, None)
            if state is _MISSING:
                buckets[stage].append(
                    Finding(stage, "fail", label, f"{name} decides on {field_name}, which is not mapped.")
                )
            elif state is _UNKNOWN:
                unknown = True
    if unknown:
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                name,
                f"{name} chooses a path from mapped fields. The path is confirmed on the transformed rows.",
            )
        )
    elif not any(item.status == "fail" and item.stage == stage for item in buckets[stage] if item.name):
        buckets[stage].append(Finding(stage, "pass", name, f"{name} decision fields are mapped."))
    return "unknown"


def _check_gated_mapping(stage, trigger_name, items, key, mapped, constants, records, buckets, outcome) -> None:
    if not items:
        noun = "action" if stage == "actions" else "approval"
        buckets[stage].append(
            Finding(stage, "pass", trigger_name, f"{trigger_name} has no {noun}.")
        )
        return
    for item in items:
        label = str(item.get("name") or trigger_name)
        when = str(item.get("when") or "always")
        for field_name in item.get(key) or []:
            _require_field(
                stage,
                buckets,
                str(field_name),
                mapped,
                constants,
                records,
                missing=f"{label} ({when}) needs {field_name}, which is not mapped.",
                blank=f"{label} needs {field_name}, which is blank.",
                unknown=f"{label} needs {field_name}. The value is checked on the transformed rows.",
                present=f"{label} has {field_name} mapped.",
            )
        if not item.get(key):
            buckets[stage].append(
                Finding(stage, "pass", label, f"{label} runs when the path is '{when}'.")
            )
    if outcome == "unknown" and any(str(item.get("when") or "always") not in ("always", "entered", "") for item in items):
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                trigger_name,
                f"Which {stage.replace('_', ' ')} of {trigger_name} run depends on the decision path.",
            )
        )


def _check_notifications_mapping(trigger_name, items, mapped, constants, buckets, outcome) -> None:
    stage = "notifications"
    if not items:
        buckets[stage].append(
            Finding(stage, "pass", trigger_name, f"{trigger_name} has no notification.")
        )
        return
    for item in items:
        label = str(item.get("name") or "Notification")
        recipient = str(item.get("recipient_field") or "")
        if not recipient:
            buckets[stage].append(
                Finding(stage, "warning", label, f"{label} does not name a recipient field.")
            )
            continue
        _require_field(
            stage,
            buckets,
            recipient,
            mapped,
            constants,
            None,
            missing=f"{label} is sent to {recipient}, which is not mapped.",
            blank=f"{label} is sent to {recipient}, which is blank.",
            unknown=f"{label} is sent to {recipient}. The value is checked on the transformed rows.",
            present=f"{label} recipient {recipient} is mapped.",
        )
    if outcome == "unknown":
        buckets[stage].append(
            Finding(
                stage,
                "warning",
                trigger_name,
                f"Which notifications of {trigger_name} are sent depends on the decision path.",
            )
        )


def _check_error_handling(rules, mapped, constants, records, discovery, buckets) -> set[int]:
    """A rule rejects a row when its ``when`` conditions match and any ``when_missing`` field is blank.

    ``when`` on its own is the rejection. ``when_missing`` on its own rejects blank fields.
    Together they reject only the rows that enter the condition with a blank field.
    """
    stage = "error_handling"
    failing: set[int] = set()
    if not rules:
        buckets[stage].append(
            Finding(stage, "pass", "Error handling", "The template declares no validation rule or fault path.")
        )
    described = set()
    for rule in rules:
        label = str(rule.get("name") or "Validation rule")
        described.add(_norm(label))
        message = str(rule.get("message") or f"{label} would reject the record.")
        referenced = [str(condition.get("field") or "") for condition in rule.get("when") or []]
        referenced += [str(name) for name in rule.get("when_missing") or []]
        if records is None:
            states = {name: _availability(name, mapped, constants, None) for name in referenced if name}
            for field_name, state in states.items():
                if state is _MISSING:
                    buckets[stage].append(
                        Finding(stage, "fail", label, f"{label} depends on {field_name}, which is not mapped.")
                    )
            if any(item.name == label and item.status == "fail" for item in buckets[stage]):
                continue
            if any(state is _UNKNOWN for state in states.values()):
                buckets[stage].append(
                    Finding(
                        stage,
                        "warning",
                        label,
                        f"{label} is mapped. Transformed rows are checked against it before load.",
                    )
                )
                continue
            rejects, problem = _rule_rejects(rule, states)
            if problem or rejects:
                buckets[stage].append(
                    Finding(stage, "fail", label, problem or message)
                )
            else:
                buckets[stage].append(Finding(stage, "pass", label, f"No mapped value trips {label}."))
            continue
        for index, row in enumerate(records):
            rejects, problem = _rule_rejects(rule, _row_values(row))
            if problem:
                failing.add(index)
                buckets[stage].append(Finding(stage, "fail", label, f"Row {index + 1}: {problem}"))
            elif rejects:
                failing.add(index)
                buckets[stage].append(Finding(stage, "fail", label, f"Row {index + 1}: {message}"))
        if not any(item.name == label and item.status == "fail" for item in buckets[stage]):
            buckets[stage].append(Finding(stage, "pass", label, f"No checked row trips {label}."))
    if discovery.ran:
        for name in discovery.validation_rules:
            if _norm(name) not in described:
                buckets[stage].append(
                    Finding(
                        stage,
                        "warning",
                        name,
                        f"{name} is an active Salesforce validation rule and is not in the template, so this check cannot prove the mapped data satisfies it.",
                    )
                )
    return failing


def _rule_rejects(rule: dict, values: dict) -> tuple[bool, str]:
    for condition in rule.get("when") or []:
        field_name = str(condition.get("field") or "")
        matched, problem = _matches(condition, values.get(field_name, _MISSING))
        if problem:
            return False, problem
        if not matched:
            return False, ""
    missing = [str(name) for name in rule.get("when_missing") or []]
    if missing:
        return any(_is_blank(values.get(name, _MISSING)) for name in missing), ""
    return bool(rule.get("when")), ""


def _summarize_rows(name, entry, decisions, actions, approvals, notifications, records, entered, buckets) -> None:
    total = len(records)
    if not any(item.name == name and item.status == "fail" for item in buckets["entry_conditions"]):
        if not entry:
            buckets["entry_conditions"].append(
                Finding(
                    "entry_conditions",
                    "pass",
                    name,
                    f"All {total} row(s) enter {name}.",
                )
            )
        else:
            skipped = total - entered
            buckets["entry_conditions"].append(
                Finding(
                    "entry_conditions",
                    "pass",
                    name,
                    f"{entered} of {total} row(s) enter {name}. {skipped} do not, so their later steps are not applied.",
                )
            )
    if not decisions and not any(item.name == name for item in buckets["decisions"]):
        buckets["decisions"].append(
            Finding("decisions", "pass", name, f"{name} has no decisions, so entered rows take one path.")
        )
    elif decisions and not any(item.status == "fail" and item.name == name for item in buckets["decisions"]):
        detail = (
            f"No row entered {name}, so no decision was taken."
            if entered == 0
            else f"Decision fields on {name} could be evaluated."
        )
        buckets["decisions"].append(Finding("decisions", "pass", name, detail))
    for stage, items, noun in (
        ("actions", actions, "action"),
        ("approvals", approvals, "approval"),
        ("notifications", notifications, "notification"),
    ):
        if any(item.status == "fail" for item in buckets[stage] if name in item.detail or item.name == name):
            continue
        if entered == 0:
            buckets[stage].append(
                Finding(stage, "pass", name, f"No row entered {name}, so its {noun}s were not applied.")
            )
        elif items:
            buckets[stage].append(
                Finding(stage, "pass", name, f"{name} {noun}s have the fields their path requires.")
            )
        elif not any(item.name == name for item in buckets[stage]):
            buckets[stage].append(Finding(stage, "pass", name, f"{name} has no {noun}."))


def _require_field(stage, buckets, name, mapped, constants, records, missing, blank, unknown, present) -> None:
    if records is not None:
        blanks = [
            index + 1
            for index, row in enumerate(records)
            if _is_blank(_row_values(row).get(name, _MISSING))
        ]
        if blanks:
            shown = ", ".join(str(number) for number in blanks[:8])
            more = f" and {len(blanks) - 8} more" if len(blanks) > 8 else ""
            buckets[stage].append(Finding(stage, "fail", name, f"{blank} Rows {shown}{more}."))
            return
        buckets[stage].append(Finding(stage, "pass", name, present))
        return
    state = _availability(name, mapped, constants, None)
    if state is _MISSING:
        buckets[stage].append(Finding(stage, "fail", name, missing))
    elif state is _UNKNOWN:
        buckets[stage].append(Finding(stage, "warning", name, unknown))
    elif _is_blank(state):
        buckets[stage].append(Finding(stage, "fail", name, blank))
    else:
        buckets[stage].append(Finding(stage, "pass", name, present))


def _entry_state(trigger_name, conditions, values, mapped) -> tuple[bool, str]:
    if not conditions:
        return True, ""
    for condition in conditions:
        field_name = str(condition.get("field") or "")
        current = values.get(field_name, _MISSING)
        if current is _MISSING and field_name not in mapped:
            return False, f"{trigger_name} enters on {field_name}, which is not mapped."
        matched, problem = _matches(condition, current)
        if problem:
            return False, problem
        if not matched:
            return False, ""
    return True, ""


def _decision_outcome(trigger_name, decisions, values, mapped) -> tuple[str, str]:
    if not decisions:
        return "default", ""
    for decision in decisions:
        conditions = list(decision.get("when") or [])
        label = str(decision.get("name") or "Decision")
        matched = True
        for condition in conditions:
            field_name = str(condition.get("field") or "")
            current = values.get(field_name, _MISSING)
            if current is _MISSING and field_name not in mapped:
                return "", f"{trigger_name} decides on {field_name}, which is not mapped."
            ok, problem = _matches(condition, current)
            if problem:
                return "", f"{label}: {problem}"
            if not ok:
                matched = False
                break
        if matched:
            return str(decision.get("outcome") or "default"), ""
    return "default", ""


def _matches(condition: dict, current) -> tuple[bool, str]:
    operator = _OPERATORS.get(str(condition.get("operator") or "equals").strip().lower())
    if operator is None:
        return False, f"Condition operator {condition.get('operator')!r} is not supported."
    field_name = str(condition.get("field") or "")
    if current is _MISSING or current is _UNKNOWN:
        if operator == "blank":
            return True, ""
        if operator == "not_blank":
            return False, ""
        return False, ""
    text = "" if current is None else str(current).strip()
    expected = "" if condition.get("value") is None else str(condition.get("value")).strip()
    choices = [str(item).strip() for item in condition.get("values") or []]
    if operator == "blank":
        return text == "", ""
    if operator == "not_blank":
        return text != "", ""
    if operator == "equals":
        return text == expected, ""
    if operator == "not_equals":
        return text != expected, ""
    if operator == "in":
        return text in choices, ""
    if operator == "not_in":
        return text not in choices, ""
    if operator == "contains":
        return expected.lower() in text.lower(), ""
    left = _number(text)
    right = _number(expected)
    if left is None or right is None:
        return False, f"{field_name} value {text!r} cannot be compared with {operator.replace('_', ' ')}."
    if operator == "greater_than":
        return left > right, ""
    return left < right, ""


def _availability(name: str, mapped: set[str], constants: dict[str, str], records):
    if name in constants:
        return constants[name]
    if name in mapped:
        return _UNKNOWN
    return _MISSING


def _mapped_fields(config: dict) -> tuple[set[str], dict[str, str]]:
    mapped: set[str] = set()
    constants: dict[str, str] = {}
    for item in config.get("fields") or []:
        destination = str(item.get("destination_field") or "")
        if not destination or destination == "Id":
            continue
        has_source = bool(item.get("source_field"))
        has_transform = bool(item.get("transformation_function"))
        if "use_constant" in item and not has_source:
            constants[destination] = "" if item.get("use_constant") is None else str(item.get("use_constant"))
            mapped.add(destination)
        elif has_source or has_transform or "use_constant" in item:
            mapped.add(destination)
    return mapped, constants


def _row_values(row: dict) -> dict:
    values = {}
    for key, value in row.items():
        if not key or key in {"sys_id", "hash_key", "sf__Id", "sf__Created"}:
            continue
        values[key] = value
    return values


def _applies(when, outcome: str) -> bool:
    token = str(when or "always").strip()
    if token in ("", "always", "entered"):
        return True
    return token == outcome


def _is_blank(value) -> bool:
    if value is _MISSING or value is _UNKNOWN or value is None:
        return True
    return str(value).strip() == ""


def _number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm(value: str) -> str:
    return value.strip().casefold()


def _read_template(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a workflow template object.")
    return data


def _stage(stage: str, findings: list[Finding]) -> StageResult:
    kept = findings[:_MAX_FINDINGS]
    hidden = len(findings) - len(kept)
    if hidden > 0:
        kept.append(
            Finding(stage, findings[-1].status if findings else "warning", "More", f"{hidden} more finding(s) were omitted.")
        )
    statuses = [item.status for item in findings]
    if "fail" in statuses:
        status = "fail"
        detail = "This step would stop the record from following the template."
    elif "warning" in statuses:
        status = "warning"
        detail = "This step can be followed, with something the template or the org still leaves open."
    elif findings and all(item == "skipped" for item in statuses):
        status = "skipped"
        detail = "This step does not apply."
    elif not findings:
        status = "pass"
        detail = "Nothing in this step failed."
    else:
        status = "pass"
        detail = "This step matches the template."
    return StageResult(stage=stage, status=status, detail=detail, findings=kept)


def _fill_quiet_stages(buckets) -> None:
    for stage, findings in buckets.items():
        if findings:
            continue
        findings.append(Finding(stage, "pass", STAGE_LABELS[stage], "Nothing in this step failed."))


def _empty_result(mapping, salesforce_object, status, detail, template_found, ok) -> ObjectWorkflowResult:
    first = Finding("object", status, salesforce_object or mapping, detail)
    stages = []
    for stage in STAGE_ORDER:
        if stage == "object":
            stages.append(StageResult(stage, status, detail, [first]))
        else:
            stages.append(
                StageResult(
                    stage,
                    "skipped",
                    "Skipped because the template was not applied.",
                    [Finding(stage, "skipped", STAGE_LABELS[stage], "Skipped because the template was not applied.")],
                )
            )
    return ObjectWorkflowResult(
        mapping=mapping,
        salesforce_object=salesforce_object,
        template_found=template_found,
        ok=ok,
        failing_rows=0,
        stages=stages,
        summary=detail,
    )


def _credentials_ready(credentials: dict | None) -> bool:
    if not credentials:
        return False
    return bool(credentials.get("base_url") and credentials.get("client_id") and credentials.get("client_secret"))


def _salesforce_session(credentials: dict) -> tuple[str, str, str]:
    base_url = str(credentials.get("base_url") or "").rstrip("/")
    response = requests.post(
        f"{base_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": credentials.get("client_id") or "",
            "client_secret": credentials.get("client_secret") or "",
        },
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    token = body.get("access_token") or ""
    if not token:
        raise ValueError("Salesforce did not return an access token.")
    instance = str(body.get("instance_url") or base_url).rstrip("/")
    version = str(credentials.get("api_version") or "v60.0")
    return token, instance, version


def _tooling_names(instance: str, version: str, headers: dict, soql: str) -> list[str]:
    response = requests.get(
        f"{instance}/services/data/{version}/tooling/query",
        headers=headers,
        params={"q": soql},
        timeout=20,
    )
    response.raise_for_status()
    names = []
    for row in response.json().get("records") or []:
        name = row.get("Name") or row.get("ApiName") or row.get("ValidationName") or ""
        if name:
            names.append(str(name))
    return names
