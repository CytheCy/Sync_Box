from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import Mock, patch

from sync_box.box_auth import (
    AuthenticationError,
    _box_cli_executable,
    _parse_version,
    _read_only_access_token,
    authorize,
    build_authenticated_client,
    build_write_authenticated_client,
)


class AuthenticationTests(unittest.TestCase):
    def test_version_parser_accepts_current_cli_formats(self) -> None:
        self.assertEqual(_parse_version("@box/cli/4.10.0 linux-x64"), (4, 10, 0))
        self.assertEqual(_parse_version("box-cli/4.6.0 linux-x64"), (4, 6, 0))

    @patch("sync_box.box_auth._run_captured")
    @patch("sync_box.box_auth.shutil.which", return_value="/usr/bin/box")
    def test_rejects_cli_before_official_app_support(
        self, _which: Mock, run: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["box", "--version"], 0, "@box/cli/4.5.0 linux-x64\n", ""
        )
        with self.assertRaisesRegex(AuthenticationError, "too old"):
            _box_cli_executable()

    @patch("sync_box.box_auth._box_cli_executable", return_value="/usr/bin/box")
    @patch("sync_box.box_auth._run_captured")
    def test_requests_only_a_downscoped_read_only_token(
        self, run: Mock, _executable: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 0, "short-lived-secret-token\n", ""
        )

        token = _read_only_access_token()

        self.assertEqual(token, "short-lived-secret-token")
        run.assert_called_once_with(
            [
                "/usr/bin/box",
                "tokens:exchange",
                "root_readonly,item_download",
                "--no-color",
            ]
        )

    @patch("sync_box.box_auth._box_cli_executable", return_value="/usr/bin/box")
    @patch("sync_box.box_auth._run_captured")
    def test_success_without_token_is_rejected(
        self, run: Mock, _executable: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "warning only")

        with self.assertRaisesRegex(AuthenticationError, "returned no read-only token"):
            _read_only_access_token()

        arguments = run.call_args.args[0]
        self.assertNotIn("--quiet", arguments)

    @patch("sync_box.box_auth._box_cli_executable", return_value="/usr/bin/box")
    @patch("sync_box.box_auth._run_captured")
    def test_token_is_not_in_failure_message(
        self, run: Mock, _executable: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 1, "do-not-disclose", "authorization expired"
        )

        with self.assertRaises(AuthenticationError) as raised:
            _read_only_access_token()

        self.assertNotIn("do-not-disclose", str(raised.exception))
        self.assertIn("authorization expired", str(raised.exception))
        self.assertIn("--reauthorize", str(raised.exception))

    @patch("sync_box.box_auth._box_cli_executable", return_value="/usr/bin/box")
    @patch("sync_box.box_auth.subprocess.run")
    def test_login_uses_the_official_cli_application(
        self, run: Mock, _executable: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0)

        authorize()

        run.assert_called_once_with(
            [
                "/usr/bin/box",
                "login",
                "--default-box-app",
                "--name",
                "sync-box",
            ],
            check=False,
        )

    @patch("sync_box.box_auth._box_cli_executable", return_value="/usr/bin/box")
    @patch("sync_box.box_auth.subprocess.run")
    def test_reauthorize_and_headless_flags_are_forwarded(
        self, run: Mock, _executable: Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0)

        authorize(reauthorize=True, code=True)

        arguments = run.call_args.args[0]
        self.assertIn("--default-box-app", arguments)
        self.assertIn("--reauthorize", arguments)
        self.assertIn("--code", arguments)

    @patch("sync_box.box_auth._read_only_access_token", return_value="token")
    @patch("sync_box.box_auth._sdk_components")
    def test_authenticated_client_uses_only_the_exchanged_token(
        self, components: Mock, _token: Mock
    ) -> None:
        auth = object()
        client = object()
        auth_factory = Mock(return_value=auth)
        client_factory = Mock(return_value=client)
        components.return_value = client_factory, auth_factory

        result = build_authenticated_client(SimpleNamespace())

        self.assertIs(result, client)
        auth_factory.assert_called_once_with("token")
        client_factory.assert_called_once_with(auth=auth)

    @patch("sync_box.box_auth._access_token", return_value="write-token")
    @patch("sync_box.box_auth._sdk_components")
    def test_write_client_requests_separate_content_scope(
        self, components: Mock, token: Mock
    ) -> None:
        auth_factory = Mock(return_value="auth")
        client_factory = Mock(return_value="client")
        components.return_value = client_factory, auth_factory

        self.assertEqual(build_write_authenticated_client(SimpleNamespace()), "client")

        token.assert_called_once_with("root_readwrite,item_download", "read/write")
        auth_factory.assert_called_once_with("write-token")

    @patch("sync_box.box_auth.shutil.which", return_value=None)
    def test_missing_cli_has_actionable_error(self, _which: Mock) -> None:
        with self.assertRaisesRegex(AuthenticationError, "not installed"):
            _box_cli_executable()


if __name__ == "__main__":
    unittest.main()
