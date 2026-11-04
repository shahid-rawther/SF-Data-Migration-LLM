"""The Salesforce workflow check, without an org or a model.

    python -m unittest tests.test_salesforce_workflow
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from integrations.salesforce.workflow import (
    STAGE_ORDER,
    Discovery,
    check_mapping,
    load_template,
)

EXAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "integrations" / "salesforce" / "workflows" / "_example_ChangeRequest.json").read_text(
        encoding="utf-8"
    )
)

MAPPED = {
    "fields": [
        {"source_field": "change_request.short_description", "destination_field": "Subject"},
        {"source_field": "change_request.state", "destination_field": "Status"},
        {"source_field": "change_request.priority", "destination_field": "Priority"},
        {"source_field": "change_request.risk", "destination_field": "RiskLevel"},
        {"source_field": "change_request.justification", "destination_field": "BusinessJustification"},
        {"source_field": "change_request.requested_by", "destination_field": "RequestedById"},
        {"source_field": "change_request.cab_delegate", "destination_field": "ReviewerId"},
        {"source_field": "change_request.start_date", "destination_field": "EstimatedStartTime"},
    ]
}


def _stage(result, name):
    return next(stage for stage in result.stages if stage.stage == name)


class TemplateTests(unittest.TestCase):
    def test_example_file_is_not_applied_by_name(self):
        self.assertIsNone(load_template("ChangeRequest"))

    def test_stages_follow_the_workflow_order(self):
        result = check_mapping("ChangeRequest", MAPPED, "ChangeRequest", template=EXAMPLE)
        self.assertEqual([stage.stage for stage in result.stages], list(STAGE_ORDER))


class MappingTests(unittest.TestCase):
    def test_complete_mapping_is_not_blocked_before_rows_exist(self):
        result = check_mapping("ChangeRequest", MAPPED, "ChangeRequest", template=EXAMPLE)
        self.assertTrue(result.ok)
        self.assertEqual(_stage(result, "decisions").status, "warning")
        self.assertEqual(_stage(result, "error_handling").status, "warning")

    def test_unmapped_decision_field_fails_that_step(self):
        fields = [item for item in MAPPED["fields"] if item["destination_field"] != "RiskLevel"]
        result = check_mapping(
            "ChangeRequest", {"fields": fields}, "ChangeRequest", template=EXAMPLE
        )
        self.assertFalse(result.ok)
        self.assertEqual(_stage(result, "object").status, "fail")
        self.assertEqual(_stage(result, "decisions").status, "fail")
        self.assertIn("RiskLevel", _stage(result, "decisions").findings[0].detail)

    def test_missing_template_does_not_block(self):
        result = check_mapping("Account", {"fields": []}, "Account", template=None)
        # template=None loads from disk; Account has no template.
        self.assertTrue(result.ok)
        self.assertFalse(result.template_found)
        self.assertEqual(_stage(result, "object").status, "warning")
        self.assertTrue(all(stage.status == "skipped" for stage in result.stages[1:]))


class RowTests(unittest.TestCase):
    def check(self, rows):
        return check_mapping(
            "ChangeRequest", MAPPED, "ChangeRequest", records=rows, template=EXAMPLE
        )

    def test_standard_change_follows_the_template(self):
        result = self.check(
            [
                {
                    "Subject": "Router upgrade",
                    "Status": "New",
                    "Priority": "Low",
                    "RiskLevel": "Low",
                    "BusinessJustification": "",
                    "RequestedById": "005000000000001",
                    "ReviewerId": "",
                    "EstimatedStartTime": "2026-10-20T09:00:00.000Z",
                }
            ]
        )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(_stage(result, "approvals").status, "pass")
        self.assertIn("1 of 1", _stage(result, "entry_conditions").findings[0].detail)

    def test_high_risk_without_justification_fails_approval_and_error_handling(self):
        result = self.check(
            [
                {
                    "Subject": "Core cutover",
                    "Status": "New",
                    "Priority": "High",
                    "RiskLevel": "High",
                    "BusinessJustification": "",
                    "RequestedById": "005000000000001",
                    "ReviewerId": "",
                    "EstimatedStartTime": "",
                }
            ]
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failing_rows, 1)
        self.assertEqual(_stage(result, "approvals").status, "fail")
        self.assertEqual(_stage(result, "notifications").status, "fail")
        self.assertEqual(_stage(result, "error_handling").status, "fail")
        self.assertEqual(_stage(result, "actions").status, "pass")

    def test_row_that_does_not_enter_is_not_a_failure(self):
        result = self.check(
            [
                {
                    "Subject": "Already closed",
                    "Status": "Closed",
                    "Priority": "Low",
                    "RiskLevel": "Low",
                    "BusinessJustification": "",
                    "RequestedById": "",
                    "ReviewerId": "",
                    "EstimatedStartTime": "",
                }
            ]
        )
        self.assertTrue(result.ok, [stage.detail for stage in result.stages])
        self.assertIn("do not", _stage(result, "entry_conditions").findings[0].detail)

    def test_one_bad_row_blocks_a_good_one(self):
        good = {
            "Subject": "Router upgrade",
            "Status": "New",
            "Priority": "Low",
            "RiskLevel": "Low",
            "BusinessJustification": "",
            "RequestedById": "005000000000001",
            "ReviewerId": "",
            "EstimatedStartTime": "2026-10-20T09:00:00.000Z",
        }
        bad = dict(good, RiskLevel="Critical", EstimatedStartTime="")
        result = self.check([good, bad])
        self.assertFalse(result.ok)
        self.assertEqual(result.failing_rows, 1)


class DiscoveryTests(unittest.TestCase):
    def test_active_org_automation_missing_from_the_template_is_a_warning(self):
        discovery = Discovery(
            ran=True,
            triggers=["Change_Request_After_Save"],
            validation_rules=["Close code required"],
        )
        result = check_mapping(
            "ChangeRequest",
            MAPPED,
            "ChangeRequest",
            template=EXAMPLE,
            discovery=discovery,
        )
        self.assertTrue(result.ok)
        trigger_text = " ".join(item.detail for item in _stage(result, "trigger").findings)
        self.assertNotIn("not an active trigger", trigger_text)
        error_text = " ".join(item.detail for item in _stage(result, "error_handling").findings)
        self.assertIn("Close code required", error_text)
        self.assertEqual(_stage(result, "error_handling").status, "warning")


if __name__ == "__main__":
    unittest.main()
