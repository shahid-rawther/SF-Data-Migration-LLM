"""Unit tests for the data readiness agent's deterministic parts.

Every describe, count and catalog is mocked, so these run without a source
system, a Salesforce org or the model. Run with:

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from unittest import mock

from agents import readiness, readiness_agent, sf_actions, source_actions
from agents import resolutions as resolution_rules
from agents.context import MigrationContext


def field(name, *, type="string", required=False, createable=True, reference_to=None, **extra):
    return {
        "name": name,
        "label": extra.pop("label", name),
        "type": type,
        "required": required,
        "createable": createable,
        "reference_to": reference_to or [],
        "length": extra.pop("length", 255),
        **extra,
    }


INCIDENT_FIELDS = [
    field("Subject", required=True, label="Short Description"),
    field("Status", required=True),
    field("Priority", required=True),
    field("Origin", required=True, label="Reported method"),
    field("OwnerId", type="reference", required=True, reference_to=["User", "Group"]),
    field("AccountId", type="reference", required=True, reference_to=["Account"]),
    field("BusinessHoursId", type="reference", required=True, reference_to=["BusinessHours"]),
    field("CaseId", type="reference", required=True, reference_to=["Case"]),
    field("Description"),
]

SOURCE_FIELDS = [
    {"name": f"incident.{n}", "label": label, "type": "string", "required": False, "reference_table": ""}
    for n, label in [
        ("short_description", "Short description"),
        ("description", "Description"),
        ("state", "State"),
        ("priority", "Priority"),
        ("number", "Number"),
    ]
]

CATALOG = {
    "Incident": {"createable": True},
    "User": {"createable": True},
    "Group": {"createable": True},
    "Account": {"createable": True},
    "Case": {"createable": True},
    "BusinessHours": {"createable": False},
    "Profile": {"createable": False},
}


def make_context(configs: dict, destination: dict, catalog: dict | None = None) -> MigrationContext:
    """A context whose describes, source schema and mapping catalog are fixed."""
    context = MigrationContext(
        source_type="servicenow",
        mapping_names=list(configs),
        configs=configs,
    )
    for obj, fields in destination.items():
        context._destination_cache[(obj, "")] = fields
    context._source_cache["incident"] = SOURCE_FIELDS
    context._catalog = catalog or {}
    return context


def incident_config(fields: list[dict]) -> dict:
    return {
        "destination_object": "Incident",
        "source_tables": ["incident"],
        "operation": "insert",
        "fields": fields,
    }


class FakeCounter:
    """Source counts without a source: 100 rows, 7 empty short descriptions."""

    def __init__(self, context):
        self.provider = object()

    def total(self, table):
        return 100

    def empty(self, table, column):
        return 7 if column.endswith("short_description") else 0


# ── classification ─────────────────────────────────────────────────────────────


class ClassificationTests(unittest.TestCase):
    def analyse(self, fields: list[dict], extra_configs: dict | None = None, catalog=None):
        configs = {"Incident": incident_config(fields), **(extra_configs or {})}
        destination = {"Incident": INCIDENT_FIELDS, "User": [], "Account": []}
        context = make_context(configs, destination, catalog)
        with mock.patch.object(readiness, "_Counter", FakeCounter), mock.patch.object(
            readiness.sf_inventory, "object_catalog", return_value=CATALOG
        ), mock.patch.object(readiness.sf_inventory, "count_records", return_value=3):
            report = readiness.analyse(context, ["Incident"])
        return {r.field: r for r in report.requirements if r.mapping == "Incident"}

    def test_partly_empty_source_without_fallback_is_partial(self):
        result = self.analyse([{"destination_field": "Subject", "source_field": "incident.short_description"}])
        self.assertEqual(result["Subject"].status, readiness.STATUS_PARTIAL)
        self.assertEqual(result["Subject"].empty_rows, 7)
        self.assertIn("7 source row(s)", result["Subject"].detail)

    def test_fallback_transform_covers_empty_rows(self):
        result = self.analyse(
            [
                {
                    "destination_field": "Subject",
                    "source_field": "incident.short_description",
                    "transformation_function": "generic_default_if_empty",
                    "transformation_params": {"default": "No subject"},
                }
            ]
        )
        self.assertEqual(result["Subject"].status, readiness.STATUS_COVERED)

    def test_fully_populated_source_is_covered(self):
        result = self.analyse([{"destination_field": "Priority", "source_field": "incident.priority"}])
        self.assertEqual(result["Priority"].status, readiness.STATUS_COVERED)

    def test_constant_is_covered(self):
        result = self.analyse([{"destination_field": "Origin", "constant_value": "Email"}])
        self.assertEqual(result["Origin"].status, readiness.STATUS_COVERED)

    def test_unmapped_with_a_likely_column_ranks_the_direct_match_first(self):
        result = self.analyse([])
        subject = result["Subject"]
        self.assertEqual(subject.status, readiness.STATUS_UNMAPPED_WITH_CANDIDATE)
        self.assertEqual(subject.candidates[0], "short_description")

    def test_unmapped_with_nothing_similar_is_no_source(self):
        result = self.analyse([])
        self.assertEqual(result["Origin"].status, readiness.STATUS_NO_SOURCE)

    def test_mapped_column_missing_from_the_source_is_no_source(self):
        result = self.analyse([{"destination_field": "Status", "source_field": "incident.u_gone"}])
        self.assertEqual(result["Status"].status, readiness.STATUS_NO_SOURCE)
        self.assertIn("u_gone", result["Status"].detail)

    def test_lookup_onto_an_object_a_selected_mapping_loads(self):
        accounts = {
            "Companies": {
                "destination_object": "Account",
                "source_tables": ["core_company"],
                "fields": [],
            }
        }
        configs = {"Incident": incident_config([]), **accounts}
        context = make_context(configs, {"Incident": INCIDENT_FIELDS, "Account": []})
        with mock.patch.object(readiness, "_Counter", FakeCounter), mock.patch.object(
            readiness.sf_inventory, "object_catalog", return_value=CATALOG
        ), mock.patch.object(readiness.sf_inventory, "count_records", return_value=3):
            report = readiness.analyse(context, ["Incident", "Companies"])
        account = next(r for r in report.requirements if r.field == "AccountId")
        self.assertEqual(account.status, readiness.STATUS_PARENT_MIGRATABLE)
        self.assertTrue(account.parent_in_run)
        self.assertEqual(account.parent_mappings, ["Companies"])

    def test_lookup_onto_an_object_a_mapping_outside_the_run_loads(self):
        result = self.analyse([], catalog={"User": ["User migration"]})
        owner = result["OwnerId"]
        self.assertEqual(owner.status, readiness.STATUS_PARENT_MIGRATABLE)
        self.assertEqual(owner.parent_object, "User")
        self.assertFalse(owner.parent_in_run)
        self.assertEqual(owner.parent_mappings, ["User migration"])

    def test_lookup_onto_a_creatable_object_nothing_migrates(self):
        result = self.analyse([])
        case = result["CaseId"]
        self.assertEqual(case.status, readiness.STATUS_PARENT_CREATABLE)
        self.assertEqual(case.parent_existing_records, 3)

    def test_lookup_onto_an_object_the_api_cannot_create(self):
        result = self.analyse([])
        self.assertEqual(result["BusinessHoursId"].status, readiness.STATUS_PARENT_NOT_CREATABLE)

    def test_lookup_pinned_to_a_constant_is_covered(self):
        result = self.analyse([{"destination_field": "BusinessHoursId", "constant_value": "01m000000000001AAA"}])
        self.assertEqual(result["BusinessHoursId"].status, readiness.STATUS_COVERED)

    def test_optional_and_read_only_fields_are_not_requirements(self):
        result = self.analyse([])
        self.assertNotIn("Description", result)


# ── source field names ─────────────────────────────────────────────────────────


class SourceFieldTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context({"Incident": incident_config([])}, {"Incident": INCIDENT_FIELDS})

    def test_bare_qualified_and_inherited_names_all_match(self):
        for name in ("short_description", "incident.short_description", "task.short_description"):
            self.assertTrue(self.context.has_source_field("incident", name), name)

    def test_every_column_of_a_multi_column_source_must_exist(self):
        self.assertTrue(self.context.has_source_field("incident", "short_description, incident.number"))
        # The last column existing used to be enough.
        self.assertFalse(self.context.has_source_field("incident", "u_missing,number"))

    def test_unknown_column_is_rejected(self):
        self.assertFalse(self.context.has_source_field("incident", "u_missing"))

    def test_unintrospectable_source_is_not_a_rejection(self):
        self.assertTrue(self.context.has_source_field("sys_user", "anything"))

    def test_suggested_columns_are_written_against_the_driving_table(self):
        item = readiness_agent._qualify_source(
            self.context,
            {"mapping": "Incident", "field": "Subject", "source_field": "task.short_description, number"},
        )
        self.assertEqual(item["source_field"], "incident.short_description,incident.number")

    def test_joined_table_columns_keep_their_table(self):
        config = incident_config([])
        config["source_tables"] = ["incident", "sys_user"]
        context = make_context({"Incident": config}, {"Incident": INCIDENT_FIELDS})
        item = readiness_agent._qualify_source(
            context, {"mapping": "Incident", "source_field": "sys_user.name,short_description"}
        )
        self.assertEqual(item["source_field"], "sys_user.name,incident.short_description")


# ── resolution validation ──────────────────────────────────────────────────────


class TransformParamTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context({"Incident": incident_config([])}, {"Incident": INCIDENT_FIELDS})

    def validate(self, transform, params):
        return resolution_rules.validate(
            self.context,
            [
                {
                    "kind": "type_coercion",
                    "mapping": "Incident",
                    "field": "Subject",
                    "source_field": "incident.short_description,incident.description",
                    "transform": transform,
                    "transform_params": params,
                    "rationale": "Subject needs a value on every row.",
                }
            ],
        )

    def test_known_parameters_are_accepted(self):
        outcome = self.validate("generic_default_if_empty", {"default": "No subject"})
        self.assertEqual(len(outcome.accepted), 1, outcome.rejected)

    def test_parameters_a_transform_ignores_are_rejected(self):
        outcome = self.validate("generic_default_if_empty", {"fallback_fields": ["incident.description"]})
        self.assertFalse(outcome.accepted)
        self.assertIn("fallback_fields", outcome.rejected[0]["reason"])

    def test_misspelt_parameter_is_rejected(self):
        outcome = self.validate("generic_truncate", {"length": 255})
        self.assertFalse(outcome.accepted)
        self.assertIn("max_length", outcome.rejected[0]["reason"])


# ── Salesforce actions ─────────────────────────────────────────────────────────


GROUP_FIELDS = [
    field("Name", required=True),
    field("Type", required=True, picklist_values=["Regular", "Queue"], picklist_restricted=True),
    field("OwnerId", type="reference", reference_to=["User"]),
]
CASE_FIELDS = [
    field("Subject", required=True),
    field("QueueId__c", type="reference", reference_to=["Group"]),
]


class SalesforceActionTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context(
            {"Incident": incident_config([])},
            {"Incident": INCIDENT_FIELDS, "Group": GROUP_FIELDS, "Case": CASE_FIELDS},
        )
        patcher = mock.patch.object(sf_actions.sf_inventory, "object_catalog", return_value=CATALOG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, obj, values, rationale="A default is needed."):
        return {"kind": "create_record", "object": obj, "values": values, "rationale": rationale}

    def test_record_with_every_required_field_is_accepted(self):
        outcome = sf_actions.validate(self.context, [self.record("Group", {"Name": "Default", "Type": "Queue"})])
        self.assertEqual(len(outcome.accepted), 1, outcome.rejected)

    def test_record_missing_a_required_field_is_rejected(self):
        outcome = sf_actions.validate(self.context, [self.record("Group", {"Name": "Default"})])
        self.assertIn("requires Type", outcome.rejected[0]["reason"])

    def test_restricted_picklist_value_is_checked(self):
        outcome = sf_actions.validate(self.context, [self.record("Group", {"Name": "D", "Type": "Team"})])
        self.assertIn("not an allowed value", outcome.rejected[0]["reason"])

    def test_objects_the_agent_may_not_create_are_refused(self):
        outcome = sf_actions.validate(self.context, [self.record("User", {"LastName": "X"})])
        self.assertIn("licence seat", outcome.rejected[0]["reason"])

    def test_objects_the_api_cannot_create_are_refused(self):
        outcome = sf_actions.validate(self.context, [self.record("BusinessHours", {"Name": "X"})])
        self.assertIn("cannot be created", outcome.rejected[0]["reason"])

    def test_a_lookup_value_must_be_a_record_id(self):
        outcome = sf_actions.validate(
            self.context,
            [self.record("Group", {"Name": "D", "Type": "Queue", "OwnerId": "the admin"})],
        )
        self.assertIn("needs a record id", outcome.rejected[0]["reason"])

    def test_a_record_may_point_at_one_created_earlier(self):
        outcome = sf_actions.validate(
            self.context,
            [
                self.record("Group", {"Name": "D", "Type": "Queue"}),
                self.record("Case", {"Subject": "Placeholder", "QueueId__c": "{{action:0}}"}),
            ],
        )
        self.assertEqual(len(outcome.accepted), 2, outcome.rejected)

    def test_a_record_may_not_point_at_one_created_later(self):
        outcome = sf_actions.validate(
            self.context,
            [self.record("Case", {"Subject": "Placeholder", "QueueId__c": "{{action:1}}"})],
        )
        self.assertIn("not created before", outcome.rejected[0]["reason"])

    def test_a_rejection_rejects_everything_after_it(self):
        outcome = sf_actions.validate(
            self.context,
            [
                self.record("Group", {"Name": "D"}),
                self.record("Case", {"Subject": "Placeholder", "QueueId__c": "{{action:0}}"}),
            ],
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(len(outcome.rejected), 2)
        self.assertIn("earlier action", outcome.rejected[1]["reason"])

    def test_custom_field_rules(self):
        def custom(name, label="Source number", type_="Text"):
            return {
                "kind": "create_custom_field",
                "object": "Incident",
                "field_api_name": name,
                "field_label": label,
                "field_type": type_,
                "rationale": "The source number has no home.",
            }

        self.assertTrue(sf_actions.validate(self.context, [custom("Source_Number__c")]).accepted)
        self.assertIn("__c", sf_actions.validate(self.context, [custom("Source Number")]).rejected[0]["reason"])
        existing = make_context(
            {"Incident": incident_config([])},
            {"Incident": [*INCIDENT_FIELDS, field("Source_Number__c")]},
        )
        self.assertIn(
            "already has", sf_actions.validate(existing, [custom("Source_Number__c")]).rejected[0]["reason"]
        )
        self.assertIn(
            "not a supported field type",
            sf_actions.validate(self.context, [custom("Source_Number__c", type_="Formula")]).rejected[0]["reason"],
        )


class PlaceholderTests(unittest.TestCase):
    def test_placeholders_are_found_in_constants_and_value_maps(self):
        found = sf_actions.placeholders_in(
            [
                {"constant_value": "{{action:0}}"},
                {"value_map": {"a": "{{action:2}}", "b": "005000000000001AAA"}},
                {"value_map_default": "{{ action:1 }}"},
            ]
        )
        self.assertEqual(found, {0, 2})

    def test_substitute_swaps_in_created_ids_without_touching_the_input(self):
        original = [{"kind": "constant_reference", "constant_value": "{{action:0}}"}]
        resolved = sf_actions.substitute(original, {0: "00G000000000001AAA"})
        self.assertEqual(resolved[0]["constant_value"], "00G000000000001AAA")
        self.assertEqual(original[0]["constant_value"], "{{action:0}}")

    def test_substitute_refuses_a_record_that_was_not_created(self):
        with self.assertRaises(ValueError):
            sf_actions.substitute([{"constant_value": "{{action:3}}"}], {0: "00G000000000001AAA"})


class ExecuteTests(unittest.TestCase):
    def test_records_created_before_a_failure_are_deleted_again(self):
        actions = [
            {"kind": "create_record", "object": "Group", "values": {"Name": "A"}, "rationale": "r"},
            {"kind": "create_record", "object": "Case", "values": {"Subject": "B"}, "rationale": "r"},
        ]
        with mock.patch.object(
            sf_actions.sf_inventory,
            "insert_record",
            side_effect=["00G000000000001AAA", sf_actions.sf_inventory.SalesforceWriteError("refused")],
        ), mock.patch.object(sf_actions.sf_inventory, "delete_record") as delete:
            with self.assertRaises(sf_actions.sf_inventory.SalesforceWriteError):
                sf_actions.execute(actions, {})
        delete.assert_called_once_with("Group", "00G000000000001AAA", {})

    def test_a_later_record_receives_the_earlier_id(self):
        actions = [
            {"kind": "create_record", "object": "Group", "values": {"Name": "A"}, "rationale": "r"},
            {"kind": "create_record", "object": "Case", "values": {"QueueId__c": "{{action:0}}"}, "rationale": "r"},
        ]
        with mock.patch.object(
            sf_actions.sf_inventory, "insert_record", side_effect=["00G000000000001AAA", "500000000000001AAA"]
        ) as insert:
            results = sf_actions.execute(actions, {})
        self.assertEqual(insert.call_args_list[1].args[1], {"QueueId__c": "00G000000000001AAA"})
        self.assertEqual(sf_actions.created_ids(results), {0: "00G000000000001AAA", 1: "500000000000001AAA"})


# ── suggestions ────────────────────────────────────────────────────────────────


class SuggestionTests(unittest.TestCase):
    def test_parent_outside_the_run_suggests_adding_its_mapping(self):
        requirement = readiness.Requirement(
            mapping="Incident",
            object="Incident",
            field="OwnerId",
            status=readiness.STATUS_PARENT_MIGRATABLE,
            parent_object="User",
            parent_mappings=["User migration"],
            value_source="source",
            source_field="incident.assigned_to",
        )
        [suggestion] = readiness_agent.deterministic_suggestions(requirement)
        self.assertEqual(suggestion["include_mappings"], ["User migration"])
        self.assertEqual(suggestion["resolutions"][0]["kind"], "lookup_reference")

    def test_parent_the_api_cannot_create_is_a_manual_task(self):
        requirement = readiness.Requirement(
            mapping="Incident",
            object="Incident",
            field="BusinessHoursId",
            status=readiness.STATUS_PARENT_NOT_CREATABLE,
            parent_object="BusinessHours",
        )
        [suggestion] = readiness_agent.deterministic_suggestions(requirement)
        self.assertFalse(suggestion.get("salesforce_actions"))
        self.assertTrue(suggestion["manual_steps"])

    def test_untitled_suggestion_gets_a_title_from_its_explanation(self):
        title = readiness_agent._fallback_title(
            {"explanation": "Map Subject from short_description. It is 99% filled."}
        )
        self.assertEqual(title, "Map Subject from short_description")

    def test_partly_empty_text_gets_a_mapping_default_and_a_source_fix(self):
        requirement = readiness.Requirement(
            mapping="Incident",
            object="Incident",
            field="Subject",
            type="string",
            status=readiness.STATUS_PARTIAL,
            source_table="incident",
            source_field="incident.short_description",
            empty_rows=7,
        )
        mapping_fix, source_fix = readiness_agent.deterministic_suggestions(requirement)
        resolution = mapping_fix["resolutions"][0]
        self.assertEqual(resolution["transform"], "generic_default_if_empty")
        self.assertEqual(resolution["transform_params"], {"default": readiness_agent._DEFAULT_TEXT})
        action = source_fix["source_actions"][0]
        self.assertEqual((action["table"], action["field"]), ("incident", "incident.short_description"))

    def test_unmapped_text_with_a_candidate_is_mapped_from_it(self):
        requirement = readiness.Requirement(
            mapping="Incident",
            object="Incident",
            field="Subject",
            type="string",
            status=readiness.STATUS_UNMAPPED_WITH_CANDIDATE,
            source_table="incident",
            candidates=["short_description"],
        )
        [suggestion] = readiness_agent.deterministic_suggestions(requirement)
        self.assertEqual(suggestion["resolutions"][0]["source_field"], "incident.short_description")

    def test_lookup_is_pinned_to_an_existing_record_when_one_exists(self):
        requirement = readiness.Requirement(
            mapping="Incident",
            object="Incident",
            field="BusinessHoursId",
            status=readiness.STATUS_PARENT_NOT_CREATABLE,
            parent_object="BusinessHours",
        )
        context = make_context({"Incident": incident_config([])}, {"Incident": INCIDENT_FIELDS})
        page = {"records": [{"id": "01m000000000001AAA", "name": "Default"}], "total": 1, "name_field": "Name"}
        with mock.patch("services.internal.sf_inventory.sample_records", return_value=page):
            pinned, *rest = readiness_agent.deterministic_suggestions(requirement, context)
        self.assertEqual(pinned["resolutions"][0]["kind"], "constant_reference")
        self.assertEqual(pinned["resolutions"][0]["constant_value"], "01m000000000001AAA")
        self.assertTrue(rest and rest[0]["manual_steps"])


# ── source actions ─────────────────────────────────────────────────────────────


class FakeSource:
    """A writable source: 7 incidents with no short description, one known user."""

    def __init__(self, empty=7):
        self.empty = empty
        self.values: dict[str, dict] = {f"inc{i}": {} for i in range(empty)}
        self.writes = 0
        # The write with this number (1-based) fails; the rest succeed.
        self.fail_on: int | None = None

    def count_rows(self, table, credentials, query=""):
        if table == "sys_user":
            return 1 if query == "sys_id=" + "a" * 32 else 0
        return self.empty

    def find_record_ids(self, table, credentials, query, limit):
        if query.startswith("sys_idIN"):
            ids, _, condition = query[len("sys_idIN"):].partition("^")
            column, _, value = condition.partition("=")
            return [i for i in ids.split(",") if self.values[i].get(column) == value]
        return list(self.values)[:limit]

    def update_record(self, table, record_id, values, credentials):
        self.writes += 1
        if self.writes == self.fail_on:
            raise RuntimeError("refused")
        self.values[record_id].update(values)


class SourceActionTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context({"Incident": incident_config([])}, {"Incident": INCIDENT_FIELDS})
        self.context._source_cache["incident"] = [
            *SOURCE_FIELDS,
            {"name": "incident.caller_id", "label": "Caller", "type": "reference",
             "required": False, "reference_table": "sys_user"},
            {"name": "incident.sys_created_by", "label": "Created by", "type": "string",
             "required": False, "reference_table": ""},
        ]
        self.source = FakeSource()
        for target in ("agents.source_actions._provider", "api.sources.get"):
            patcher = mock.patch(target, return_value=self.source)
            patcher.start()
            self.addCleanup(patcher.stop)

    def action(self, **overrides):
        return {
            "kind": "fill_source_values",
            "table": "incident",
            "field": "short_description",
            "value": "Not provided",
            "rationale": "Subject is required.",
            **overrides,
        }

    def validate(self, **overrides):
        return source_actions.validate(self.context, [self.action(**overrides)])

    def test_fill_on_a_migrated_table_is_accepted_with_its_count(self):
        outcome = self.validate()
        self.assertEqual(len(outcome.accepted), 1, outcome.rejected)
        self.assertEqual(outcome.accepted[0]["affected"], 7)

    def test_qualified_column_names_are_reduced_to_the_column(self):
        outcome = self.validate(field="incident.short_description")
        self.assertEqual(outcome.accepted[0]["field"], "short_description")

    def test_tables_no_mapping_reads_are_refused(self):
        outcome = self.validate(table="sys_user")
        self.assertIn("not a table", outcome.rejected[0]["reason"])

    def test_system_and_unknown_columns_are_refused(self):
        self.assertIn("system column", self.validate(field="sys_created_by").rejected[0]["reason"])
        self.assertIn("not a column", self.validate(field="u_missing").rejected[0]["reason"])

    def test_filters_may_only_narrow(self):
        self.assertTrue(self.validate(filter="active=true").accepted)
        self.assertTrue(self.validate(filter="active=true^NQpriority=1").rejected)
        self.assertTrue(self.validate(filter="ORactive=true").rejected)

    def test_reference_columns_need_an_existing_sys_id(self):
        self.assertTrue(self.validate(field="caller_id", value="a" * 32).accepted)
        self.assertIn("needs a sys_id", self.validate(field="caller_id", value="Jane").rejected[0]["reason"])
        self.assertIn("No sys_user", self.validate(field="caller_id", value="b" * 32).rejected[0]["reason"])

    def test_nothing_to_fill_and_too_much_to_fill_are_refused(self):
        self.source.empty = 0
        self.assertIn("No incident", self.validate().rejected[0]["reason"])
        self.source.empty = source_actions.max_rows() + 1
        self.assertIn("more than", self.validate().rejected[0]["reason"])

    def test_the_selection_always_requires_the_column_to_be_empty(self):
        action = source_actions.normalise(self.action(filter="^active=true"))
        self.assertEqual(source_actions.selection_query(action), "active=true^short_descriptionISEMPTY")

    def test_execute_writes_every_empty_record_and_restore_puts_them_back(self):
        [action] = self.validate().accepted
        [result] = source_actions.execute([action], self.context)
        self.assertEqual(len(result["record_ids"]), 7)
        self.assertTrue(all(v["short_description"] == "Not provided" for v in self.source.values.values()))

        # Someone fixes one record by hand before the undo; it is kept.
        self.source.values["inc0"]["short_description"] = "Printer on fire"
        outcome = source_actions.restore(result, "servicenow", {})
        self.assertEqual(outcome, {"restored": 6, "kept": 1})
        self.assertEqual(self.source.values["inc0"]["short_description"], "Printer on fire")
        self.assertEqual(self.source.values["inc1"]["short_description"], "")

    def test_a_failed_write_rolls_back_the_records_already_written(self):
        [action] = self.validate().accepted
        self.source.fail_on = 4
        with self.assertRaises(source_actions.SourceWriteError):
            source_actions.execute([action], self.context)
        self.assertTrue(all(v.get("short_description", "") == "" for v in self.source.values.values()))

    def test_a_suggestion_with_a_valid_source_fix_is_applicable(self):
        with mock.patch.object(sf_actions.sf_inventory, "object_catalog", return_value=CATALOG):
            suggestion = readiness_agent.validate_suggestion(
                self.context, {"title": "Fill it", "source_actions": [self.action()]}
            )
        self.assertTrue(suggestion["applicable"], suggestion["rejected"])
        self.assertEqual(suggestion["source_actions"][0]["affected"], 7)


if __name__ == "__main__":
    unittest.main()
