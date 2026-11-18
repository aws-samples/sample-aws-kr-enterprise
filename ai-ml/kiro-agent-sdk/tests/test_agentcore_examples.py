import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "examples" / "agentcore" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("agentcore_deploy_example", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
INVOKE_PATH = PATH.with_name("invoke.py")
INVOKE_SPEC = importlib.util.spec_from_file_location("agentcore_invoke_example", INVOKE_PATH)
INVOKE = importlib.util.module_from_spec(INVOKE_SPEC)
INVOKE_SPEC.loader.exec_module(INVOKE)
INTERACT_SPEC = importlib.util.spec_from_file_location("agentcore_interact_example", PATH.with_name("interact.py"))
INTERACT = importlib.util.module_from_spec(INTERACT_SPEC)
INTERACT_SPEC.loader.exec_module(INTERACT)
AUTH_SPEC = importlib.util.spec_from_file_location("agentcore_auth_example", PATH.with_name("auth.py"))
AUTH = importlib.util.module_from_spec(AUTH_SPEC)
AUTH_SPEC.loader.exec_module(AUTH)


class AgentCoreDeployExampleTests(unittest.TestCase):
    def args(self, storage=None):
        return argparse.Namespace(name="test", image_uri="example/image:tag",
                                  role_arn="example-role", secret_id="secret-id",
                                  secret_json_key="kiro", session_storage=storage,
                                  subnet=[], security_group=[], allow_sdk_events=False, allow_interactions=False)

    def test_create_request_contains_secret_reference_and_optional_storage(self):
        request = MODULE.build_request(self.args("/mnt/kiro"))
        self.assertEqual(request["protocolConfiguration"], {"serverProtocol": "HTTP"})
        self.assertEqual(request["networkConfiguration"], {"networkMode": "PUBLIC"})
        self.assertEqual(request["environmentVariables"]["KIRO_AGENTCORE_SECRET_ID"], "secret-id")
        self.assertEqual(request["environmentVariables"]["KIRO_AGENTCORE_STATE_DIR"], "/mnt/kiro/kiro-agent-sdk")
        self.assertEqual(request["filesystemConfigurations"], [{"sessionStorage": {"mountPath": "/mnt/kiro"}}])
        self.assertNotIn("KIRO_API_KEY", request["environmentVariables"])
        self.assertNotIn("KIRO_AGENTCORE_ALLOW_SDK_EVENTS", request["environmentVariables"])
        self.assertNotIn("KIRO_AGENTCORE_ALLOW_INTERACTIONS", request["environmentVariables"])
        self.assertNotIn("KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS", request["environmentVariables"])

    def test_unauthorized_sessions_are_an_explicit_choice(self):
        args = self.args()
        args.allow_unauthorized_sessions = True
        environment = MODULE.build_request(args)["environmentVariables"]
        self.assertEqual(environment["KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS"], "true")

    def test_codezip_request_uses_python_entrypoint_and_optional_version(self):
        args = self.args()
        args.image_uri = None
        args.code_s3_uri = "s3://example-bucket/agent/code.zip"
        args.code_version_id = "version-1"
        artifact = MODULE.build_request(args)["agentRuntimeArtifact"]
        self.assertEqual(artifact, {"codeConfiguration": {
            "code": {"s3": {"bucket": "example-bucket", "prefix": "agent/code.zip", "versionId": "version-1"}},
            "runtime": "PYTHON_3_12", "entryPoint": ["agentcore_entry.py"]}})
        for uri in ("https://example.com/a.zip", "s3://bucket", "s3://bucket/a.zip?x=1", "s3://user@bucket/a.zip"):
            with self.subTest(uri=uri):
                args.code_s3_uri = uri
                with self.assertRaises(ValueError):
                    MODULE.build_request(args)

    def test_invalid_mount_path_is_rejected(self):
        for path in ("/tmp/kiro", "/mnt/one/two", "/mnt/../escape", "/mnt/..", "/mnt/.", "relative"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    MODULE.build_request(self.args(path))

    def test_vpc_requires_both_subnets_and_security_groups(self):
        args = self.args()
        args.subnet = ["subnet-one"]
        with self.assertRaises(ValueError):
            MODULE.build_request(args)
        args.security_group = ["sg-one"]
        request = MODULE.build_request(args)
        self.assertEqual(request["networkConfiguration"], {
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": ["subnet-one"], "securityGroups": ["sg-one"]}})

    def test_sdk_events_are_explicitly_enabled(self):
        args = self.args()
        args.allow_sdk_events = True
        self.assertEqual(MODULE.build_request(args)["environmentVariables"]["KIRO_AGENTCORE_ALLOW_SDK_EVENTS"], "true")
        args.allow_interactions = True
        self.assertEqual(MODULE.build_request(args)["environmentVariables"]["KIRO_AGENTCORE_ALLOW_INTERACTIONS"], "true")

    def test_device_login_deployment_does_not_require_a_secret(self):
        args = self.args()
        args.secret_id = None
        args.secret_json_key = None
        args.device_login = True
        args.device_license = "pro"
        args.device_start_url = "https://org.awsapps.com/start"
        args.device_region = "us-east-1"
        environment = MODULE.build_request(args)["environmentVariables"]
        self.assertEqual(environment, {"KIRO_AGENTCORE_AUTH_MODE": "device",
            "KIRO_AGENTCORE_DEVICE_LICENSE": "pro", "KIRO_AGENTCORE_DEVICE_START_URL": args.device_start_url,
            "KIRO_AGENTCORE_DEVICE_REGION": "us-east-1"})
        args.device_start_url = "http://unsafe.example"
        with self.assertRaises(ValueError):
            MODULE.build_request(args)
        args.device_start_url = "https://org.awsapps.com/start"
        args.secret_json_key = "key"
        with self.assertRaises(ValueError):
            MODULE.build_request(args)

    def test_device_auth_example_uses_session_and_private_endpoint(self):
        calls, clients = [], []
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"response": io.BytesIO(b'{"status":"pending","user_code":"ABCD-1234"}')}
        def client(*args, **kwargs):
            clients.append(kwargs)
            return types.SimpleNamespace(invoke_agent_runtime=invoke)
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(client=client))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session = "01234567-89ab-cdef-0123-456789abcdef"
        endpoint = "https://vpce-example.bedrock-agentcore.us-east-1.vpce.amazonaws.com"
        argv = ["auth.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                "--session-id", session, "--endpoint-url", endpoint, "start"]
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            AUTH.main()
        self.assertEqual(calls[0]["runtimeSessionId"], session)
        self.assertEqual(json.loads(calls[0]["payload"]), {"action": "auth_start"})
        self.assertEqual(clients[0]["endpoint_url"], endpoint)
        self.assertEqual(clients[0]["config"]["retries"]["total_max_attempts"], 1)

    def test_invoke_passes_session_and_reports_stream_failure(self):
        calls = []
        class Body:
            def iter_lines(self):
                return iter([b'data: {"type":"text_delta","text":"hello"}',
                             b'data: {"type":"error","message":"Agent invocation failed"}'])
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"contentType": "text/event-stream", "response": Body()}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session_id = "01234567-89ab-cdef-0123-456789abcdef"
        argv = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                "--session-id", session_id, "--stream", "hello"]
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                INVOKE.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(calls[0]["runtimeSessionId"], session_id)
        self.assertEqual(json.loads(calls[0]["payload"]), {"prompt": "hello", "stream": True})

    def test_invoke_reads_multimodal_prompt_file(self):
        calls = []
        blocks = [{"type": "text", "text": "Describe"},
                  {"type": "image", "mimeType": "image/png", "data": "AA=="}]
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"contentType": "application/json", "response": io.BytesIO(b'{"is_error":false}')}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "prompt.json"
            path.write_text(json.dumps(blocks), encoding="utf-8")
            argv = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                    "--prompt-file", str(path)]
            with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                    patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                INVOKE.main()
        self.assertEqual(json.loads(calls[0]["payload"])["prompt"], blocks)

    def test_invoke_reports_structured_error_with_http_success(self):
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(
                invoke_agent_runtime=lambda **kwargs: {
                    "contentType": "application/json",
                    "response": io.BytesIO(b'{"error":"Agent invocation failed","is_error":true}')})))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        argv = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime", "hello"]
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            INVOKE.main()
        self.assertEqual(error.exception.code, 1)

    def test_invoke_sends_control_with_existing_session(self):
        calls = []
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"contentType": "application/json", "response": io.BytesIO(b'{"status":"requested"}')}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session = "01234567-89ab-cdef-0123-456789abcdef"
        for args in (["--interrupt"], ["--steer", "adjust"], ["--cancel-mcp-tools", "example"],
                     ["--reconnect-mcp", "example"], ["--reauthorize-mcp", "example"],
                     ["--revoke-mcp-oauth", "example"], ["--prewarm"], ["--code-status"], ["--code-init"],
                     ["--code-overview"], ["--compact"]):
            argv = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                    "--session-id", session, *args]
            with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                    patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                INVOKE.main()
        self.assertEqual([json.loads(call["payload"]) for call in calls],
                         [{"action": "interrupt"}, {"action": "steer", "message": "adjust"},
                          {"action": "mcp_cancel_tools", "server_name": "example"},
                          {"action": "mcp_reconnect", "server_name": "example", "start_oauth": False},
                          {"action": "mcp_reconnect", "server_name": "example", "start_oauth": True},
                          {"action": "mcp_revoke_oauth", "server_name": "example"},
                          {"action": "prewarm"},
                          {"action": "code_status"}, {"action": "code_init"}, {"action": "code_overview"},
                          {"action": "compact"}])
        self.assertTrue(all(call["runtimeSessionId"] == session for call in calls))

    def test_cancel_mcp_tools_requires_session_and_valid_server_name(self):
        prefix = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime"]
        session = "01234567-89ab-cdef-0123-456789abcdef"
        cases = (["--cancel-mcp-tools", "example"],
                 ["--session-id", session, "--cancel-mcp-tools", ""],
                 ["--session-id", session, "--cancel-mcp-tools", "x" * 257],
                 ["--session-id", session, "--cancel-mcp-tools", "example", "--stream"],
                 ["--reauthorize-mcp", "example"],
                 ["--revoke-mcp-oauth", "example"],
                 ["--prewarm"],
                 ["--code-status"],
                 ["--compact"],
                 ["--session-id", session, "--code-init", "--stream"],
                 ["--session-id", session, "--reconnect-mcp", ""],
                 ["--session-id", session, "--revoke-mcp-oauth", ""],
                 ["--session-id", session, "--reauthorize-mcp", "example", "--events"])
        for args in cases:
            with self.subTest(args=args), patch.object(sys, "argv", [*prefix, *args]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                INVOKE.main()

    def test_session_history_cli_requires_explicit_control_and_sends_page_options(self):
        calls = []
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"contentType": "application/json", "response": io.BytesIO(b'{"is_error":false}')}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session = "01234567-89ab-cdef-0123-456789abcdef"
        prefix = ["invoke.py", "--region", "us-east-1", "--runtime-arn", "runtime"]
        valid = [*prefix, "--session-id", session, "--session-messages", "--history-limit", "12",
                 "--history-offset", "10", "--history-coalesce", "--history-exclude-subagents",
                 "--history-original-strings", "--history-tools"]
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", valid), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            INVOKE.main()
        self.assertEqual(json.loads(calls[0]["payload"]), {
            "action": "session_messages", "limit": 12, "offset": 10, "coalesce_chunks": True,
            "exclude_observed_subagents": True, "prefer_original_strings": True,
            "include_observed_tool_messages": True})
        cases = (["--session-messages"], ["--session-id", session, "--session-messages", "--stream"],
                 ["--session-id", session, "--session-messages", "--history-limit", "0"],
                 ["--session-id", session, "--session-messages", "--history-offset", "20001"],
                 ["--history-tools", "prompt"], ["--history-limit", "1", "prompt"])
        for args in cases:
            with self.subTest(args=args), patch.object(sys, "argv", [*prefix, *args]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                INVOKE.main()

    def test_examples_use_explicit_private_endpoint_and_reject_http(self):
        endpoint = "https://vpce-example.bedrock-agentcore.us-east-1.vpce.amazonaws.com"
        client_calls = []
        def invoke(**kwargs):
            return {"contentType": "application/json", "response": io.BytesIO(b'{"status":"accepted"}')}
        def client(*args, **kwargs):
            client_calls.append(kwargs)
            return types.SimpleNamespace(invoke_agent_runtime=invoke)
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(client=client))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session = "01234567-89ab-cdef-0123-456789abcdef"
        for module, tail in ((INVOKE, ["hello"]), (INTERACT, ["list"])):
            prefix = ["example.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                      "--session-id", session, "--endpoint-url"]
            with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                    patch.object(sys, "argv", [*prefix, endpoint, *tail]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                module.main()
            with patch.object(sys, "argv", [*prefix, "http://example.com", *tail]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.main()
        self.assertEqual([call["endpoint_url"] for call in client_calls], [endpoint, endpoint])
        self.assertTrue(all(call["config"]["retries"]["total_max_attempts"] == 1 for call in client_calls))

    def test_interaction_example_sends_session_scoped_decisions(self):
        calls = []
        def invoke(**kwargs):
            calls.append(kwargs)
            return {"response": io.BytesIO(b'{"status":"accepted"}')}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        session = "01234567-89ab-cdef-0123-456789abcdef"
        ident = "a" * 32
        for command in (["list"], ["allow", ident], ["answer", ident, "yes"], ["dismiss", ident],
                        ["oauth", ident]):
            argv = ["interact.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                    "--session-id", session, *command]
            with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                    patch.object(sys, "argv", argv), \
                    patch.object(INTERACT.getpass, "getpass", return_value="http://127.0.0.1/callback?code=test"), \
                    contextlib.redirect_stdout(io.StringIO()):
                INTERACT.main()
        self.assertEqual([json.loads(call["payload"]) for call in calls], [
            {"action": "interactions"},
            {"action": "respond", "interaction_id": ident, "decision": "allow"},
            {"action": "respond", "interaction_id": ident, "answer": "yes"},
            {"action": "respond", "interaction_id": ident, "answer": None},
            {"action": "respond", "interaction_id": ident,
             "redirect_url": "http://127.0.0.1/callback?code=test"},
        ])

    def test_interaction_example_reports_error_response(self):
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(
                invoke_agent_runtime=lambda **values: {"response": io.BytesIO(b'{"error":"not enabled"}')})))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        argv = ["interact.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                "--session-id", "01234567-89ab-cdef-0123-456789abcdef", "list"]
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                INTERACT.main()
        self.assertEqual(error.exception.code, 1)

    def test_interaction_example_sends_updates_and_rejects_invalid_files(self):
        calls = []
        def invoke(**kwargs):
            calls.append(json.loads(kwargs["payload"]))
            return {"response": io.BytesIO(b'{"status":"accepted"}')}
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
            client=lambda *args, **kwargs: types.SimpleNamespace(invoke_agent_runtime=invoke)))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        ident = "a" * 32
        update = {"type": "addRules", "destination": "session", "behavior": "deny",
                  "rules": [{"toolName": "write"}]}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "updates.json"
            argv = ["interact.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                    "--session-id", "01234567-89ab-cdef-0123-456789abcdef",
                    "allow", ident, "--permission-updates-file", str(path)]
            path.write_text(json.dumps([update]))
            with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                    patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                INTERACT.main()
            self.assertEqual(calls, [{"action": "respond", "interaction_id": ident, "decision": "allow",
                                      "updated_permissions": [update]}])
            for content in ("{}", "[]", "[NaN]", "x" * (256 * 1024 + 1)):
                with self.subTest(content=content[:10]):
                    path.write_text(content)
                    with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                            patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit):
                        INTERACT.main()
            self.assertEqual(len(calls), 1)

    @unittest.skipUnless(os.name == "posix", "requires a detached POSIX session")
    def test_oauth_example_rejects_input_when_terminal_cannot_hide_it(self):
        synthetic = "http://127.0.0.1/callback?code=synthetic-secret&state=synthetic\n"
        result = subprocess.run(
            [sys.executable, str(PATH.with_name("interact.py")), "--region", "us-east-1",
             "--runtime-arn", "runtime", "--session-id", "01234567-89ab-cdef-0123-456789abcdef",
             "oauth", "a" * 32],
            input=synthetic, text=True, capture_output=True, start_new_session=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("terminal that can hide input", result.stderr)
        self.assertNotIn("synthetic-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
