"""ServiceNow connection checks, without calling an instance."""
from __future__ import annotations

import unittest

from integrations.servicenow.provider import (
    _instance_origin,
    _interpret_servicenow_probe,
)


class _Response:
    def __init__(self, status_code, content_type="", text=""):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.text = text


class ServiceNowCredentialTests(unittest.TestCase):
    def test_ui_path_is_reduced_to_the_instance_host(self):
        self.assertEqual(
            _instance_origin("https://dev.service-now.com/now/nav/ui/classic"),
            "https://dev.service-now.com",
        )

    def test_json_reply_is_a_successful_connection(self):
        result = _interpret_servicenow_probe(
            _Response(200, "application/json;charset=UTF-8", '{"result":[]}')
        )
        self.assertTrue(result["valid"])

    def test_hibernate_page_is_not_reported_as_a_bad_password(self):
        result = _interpret_servicenow_probe(
            _Response(200, "text/html", "<html>Instance Hibernating</html>")
        )
        self.assertFalse(result["valid"])
        self.assertIn("hibernat", result["message"].lower())

    def test_gateway_failure_means_the_instance_is_down(self):
        result = _interpret_servicenow_probe(
            _Response(502, "text/html", "<html><h1>502 Bad Gateway</h1></html>")
        )
        self.assertFalse(result["valid"])
        self.assertIn("not responding", result["message"])
        self.assertNotIn("browser page", result["message"])

    def test_rejected_password_is_explicit(self):
        result = _interpret_servicenow_probe(_Response(401, "application/json", "{}"))
        self.assertIn("username or password", result["message"])


if __name__ == "__main__":
    unittest.main()
