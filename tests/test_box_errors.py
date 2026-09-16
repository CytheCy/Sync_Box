from types import SimpleNamespace
import unittest

from sync_box.box_errors import format_box_api_error, safe_error_detail


class BoxErrorTests(unittest.TestCase):
    def test_api_error_reports_only_selected_response_fields(self) -> None:
        error = RuntimeError("request included Authorization: Bearer secret-value")
        error.response_info = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=403,
            code="access_denied_insufficient_permissions",
            body={
                "message": "Access denied - insufficient permission",
                "access_token": "do-not-disclose",
            },
            request_id="abc123",
            headers={"Authorization": "Bearer do-not-disclose"},
        )

        detail = format_box_api_error(error)

        self.assertIn("status='403'", detail)
        self.assertIn("code='access_denied_insufficient_permissions'", detail)
        self.assertIn("request_id='abc123'", detail)
        self.assertNotIn("do-not-disclose", detail)
        self.assertNotIn("secret-value", detail)

    def test_sdk_error_redacts_common_credential_forms(self) -> None:
        error = RuntimeError()
        error.message = (  # type: ignore[attr-defined]
            "request failed Authorization: Bearer abc access_token=xyz"
        )

        detail = format_box_api_error(error)

        self.assertIn("[REDACTED]", detail)
        self.assertNotIn("abc", detail)
        self.assertNotIn("xyz", detail)

    def test_safe_detail_is_single_line_and_bounded(self) -> None:
        detail = safe_error_detail("first\nsecond " + "x" * 100, limit=20)
        self.assertEqual(len(detail), 20)
        self.assertNotIn("\n", detail)


if __name__ == "__main__":
    unittest.main()
