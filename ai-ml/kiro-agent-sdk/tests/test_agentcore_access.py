import fnmatch
import json
import unittest

from kiro_agent_sdk.agentcore_access import AgentCorePrivateAccess, AgentCorePolicyApplyError, apply_private_access

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example-A1b2c3d4e5"
ROLE = "arn:aws:iam::123456789012:role/private-invoker"
VPCE = "vpce-0123456789abcdef0"


class PolicyClient:
    def __init__(self):
        self.policies = {}
        self.writes = []
        self.fail_on = None

    def get_resource_policy(self, resourceArn):
        return {"policy": json.dumps(self.policies[resourceArn])} if resourceArn in self.policies else {}

    def put_resource_policy(self, resourceArn, policy):
        if resourceArn == self.fail_on:
            raise RuntimeError("simulated AWS failure")
        self.writes.append(resourceArn)
        self.policies[resourceArn] = json.loads(policy)
        return {"policy": policy}


class AgentCorePrivateAccessTests(unittest.TestCase):
    def access(self):
        return AgentCorePrivateAccess(RUNTIME, (ROLE,), (VPCE,), ("DEFAULT", "production"))

    def test_every_resource_has_external_and_other_principal_denies(self):
        access = self.access()
        self.assertEqual(len(access.resource_policies()), 3)
        for arn, policy in access.resource_policies().items():
            for source, principal, denied in (
                (VPCE, ROLE, False), (None, ROLE, True), ("vpce-00000000", ROLE, True),
                (VPCE, "arn:aws:iam::123456789012:role/other", True), (None, None, True),
            ):
                for action in ("InvokeAgentRuntime", "InvokeAgentRuntimeForUser",
                               "InvokeAgentRuntimeCommand", "InvokeAgentRuntimeWithWebSocketStream"):
                    # Evaluate the generated deny subset. Explicit deny remains
                    # effective even if an identity policy grants invocation.
                    context = {"aws:SourceVpce": source, "aws:PrincipalArn": principal}
                    matches = [statement for statement in policy["Statement"]
                        if statement["Effect"] == "Deny" and statement["Resource"] == arn and
                        fnmatch.fnmatchcase("bedrock-agentcore:" + action, statement["Action"]) and
                        all(context.get(key) not in values
                            for condition in statement["Condition"].values()
                            for key, values in condition.items())]
                    self.assertEqual(bool(matches), denied)
            self.assertFalse(any(s["Resource"] == "*" for s in policy["Statement"]))
        self.assertEqual(access.caller_policy()["Statement"][0]["Resource"], list(access.resource_arns))
        plan = access.plan()
        plan["resource_policies"].clear()
        self.assertEqual(len(access.resource_policies()), 3)

    def test_invalid_and_ambiguous_identifiers_are_rejected(self):
        for changes in (
            {"runtime_arn": RUNTIME + "/*"}, {"caller_role_arns": (ROLE + "*",)},
            {"caller_role_arns": ("arn:aws-cn:iam::123456789012:role/other",)},
            {"caller_role_arns": (ROLE, ROLE)}, {"vpce_ids": ()},
            {"vpce_ids": ("vpce-*",)}, {"endpoint_names": ("DEFAULT/other",)},
            {"endpoint_names": ("DEFAULT", "DEFAULT")},
        ):
            values = dict(runtime_arn=RUNTIME, caller_role_arns=(ROLE,), vpce_ids=(VPCE,))
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AgentCorePrivateAccess(**values)

    def test_runtime_first_application_and_idempotence(self):
        access, client = self.access(), PolicyClient()
        self.assertEqual(apply_private_access(client, access), access.resource_arns)
        self.assertEqual(client.writes[0], RUNTIME)
        self.assertEqual(apply_private_access(client, access), ())
        self.assertEqual(len(client.writes), 3)

    def test_aws_single_value_policy_normalization_is_idempotent(self):
        access, client = self.access(), PolicyClient()
        original = client.put_resource_policy
        def normalized(**kwargs):
            original(**kwargs)
            policy = client.policies[kwargs["resourceArn"]]
            for statement in policy["Statement"]:
                if isinstance(statement["Principal"], dict):
                    statement["Principal"]["AWS"] = statement["Principal"]["AWS"][0]
                for condition in statement["Condition"].values():
                    for key in condition:
                        condition[key] = condition[key][0]
            return {"policy": json.dumps(policy)}
        client.put_resource_policy = normalized
        self.assertEqual(apply_private_access(client, access), access.resource_arns)
        self.assertEqual(apply_private_access(client, access), ())
        self.assertEqual(len(client.writes), 3)

    def test_normalization_does_not_hide_a_different_principal_or_condition(self):
        from kiro_agent_sdk.agentcore_access import _same_policy
        import copy
        expected = self.access().resource_policies()[RUNTIME]
        for part in ("principal", "operator", "endpoint", "deny", "action"):
            changed = copy.deepcopy(expected)
            if part == "principal":
                changed["Statement"][0]["Principal"]["AWS"] = ROLE + "-other"
            elif part == "operator":
                changed["Statement"][1]["Condition"] = {"StringEquals": {"aws:SourceVpce": VPCE}}
            elif part == "endpoint":
                changed["Statement"][1]["Condition"]["StringNotEquals"]["aws:SourceVpce"] = "vpce-00000000"
            elif part == "deny":
                changed["Statement"].pop()
            else:
                changed["Statement"][1]["Action"] = "bedrock-agentcore:InvokeAgentRuntime"
            with self.subTest(part=part):
                self.assertFalse(_same_policy(expected, changed))
        self.assertFalse(_same_policy({"Unknown": ["x"]}, {"Unknown": "x"}))
        self.assertFalse(_same_policy({"Unknown": True}, {"Unknown": 1}))

    def test_conflict_preflight_makes_no_changes(self):
        access, client = self.access(), PolicyClient()
        client.policies[access.resource_arns[-1]] = {"Version": "2012-10-17", "Statement": []}
        with self.assertRaisesRegex(ValueError, "existing resource policy"):
            apply_private_access(client, access)
        self.assertEqual(client.writes, [])
        self.assertEqual(apply_private_access(client, access, replace_existing=True), access.resource_arns)

    def test_failed_endpoint_keeps_runtime_policy_and_reports_partial_application(self):
        access, client = self.access(), PolicyClient()
        client.fail_on = access.resource_arns[1]
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.applied_resources, (RUNTIME,))
        self.assertEqual(caught.exception.failed_resource, access.resource_arns[1])
        self.assertEqual(caught.exception.verified_resources, (RUNTIME,))
        self.assertEqual(caught.exception.uncertain_resources, (access.resource_arns[1],))
        self.assertEqual(client.policies[RUNTIME], access.resource_policies()[RUNTIME])

    def test_concurrent_policy_change_is_not_overwritten(self):
        access, client = self.access(), PolicyClient()
        original = client.get_resource_policy
        reads = 0
        def changed(resourceArn):
            nonlocal reads
            reads += 1
            if reads == len(access.resource_arns) + 1:
                client.policies[RUNTIME] = {"Version": "2012-10-17", "Statement": []}
            return original(resourceArn)
        client.get_resource_policy = changed
        with self.assertRaises(AgentCorePolicyApplyError):
            apply_private_access(client, access, replace_existing=True)
        self.assertEqual(client.writes, [])

    def test_unconfirmed_write_is_retained_and_reported(self):
        access, client = self.access(), PolicyClient()
        original = client.put_resource_policy
        def changed(**kwargs):
            response = original(**kwargs)
            response["policy"] = json.dumps({"Version": "2012-10-17", "Statement": []})
            return response
        client.put_resource_policy = changed
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.applied_resources, (RUNTIME,))
        self.assertEqual(caught.exception.verified_resources, (RUNTIME,))
        self.assertEqual(caught.exception.uncertain_resources, ())
        self.assertEqual(client.policies[RUNTIME], access.resource_policies()[RUNTIME])

    def test_lost_write_response_is_read_back_before_stopping_without_retry(self):
        access, client = self.access(), PolicyClient()
        original = client.put_resource_policy
        def lost_response(**kwargs):
            original(**kwargs)
            raise TimeoutError("simulated lost AWS response")
        client.put_resource_policy = lost_response
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(client.writes, [RUNTIME])
        self.assertEqual(caught.exception.applied_resources, (RUNTIME,))
        self.assertEqual(caught.exception.verified_resources, (RUNTIME,))
        self.assertEqual(caught.exception.uncertain_resources, ())
        self.assertEqual(caught.exception.phase, "apply")
        self.assertEqual(client.policies[RUNTIME], access.resource_policies()[RUNTIME])

    def test_lost_response_and_failed_readback_report_possible_write(self):
        access, client = self.access(), PolicyClient()
        original_put, original_get = client.put_resource_policy, client.get_resource_policy
        def lost_response(**kwargs):
            original_put(**kwargs)
            raise TimeoutError("simulated lost AWS response")
        def unavailable(resourceArn):
            if client.writes:
                raise TimeoutError("simulated unavailable policy read")
            return original_get(resourceArn)
        client.put_resource_policy, client.get_resource_policy = lost_response, unavailable
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.applied_resources, ())
        self.assertEqual(caught.exception.verified_resources, ())
        self.assertEqual(caught.exception.uncertain_resources, (RUNTIME,))
        self.assertEqual(client.writes, [RUNTIME])
        self.assertEqual(client.policies[RUNTIME], access.resource_policies()[RUNTIME])

    def test_acknowledged_write_is_uncertain_when_readback_differs(self):
        access, client = self.access(), PolicyClient()
        original = client.put_resource_policy
        def changed(**kwargs):
            result = original(**kwargs)
            client.policies[RUNTIME] = {"Version": "2012-10-17", "Statement": []}
            return result
        client.put_resource_policy = changed
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.applied_resources, (RUNTIME,))
        self.assertEqual(caught.exception.verified_resources, ())
        self.assertEqual(caught.exception.uncertain_resources, (RUNTIME,))
        self.assertEqual(client.writes, [RUNTIME])

    def test_already_correct_runtime_is_reported_verified_on_endpoint_failure(self):
        access, client = self.access(), PolicyClient()
        client.policies[RUNTIME] = access.resource_policies()[RUNTIME]
        client.fail_on = access.resource_arns[1]
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.applied_resources, ())
        self.assertEqual(caught.exception.verified_resources, (RUNTIME,))
        self.assertEqual(caught.exception.uncertain_resources, (access.resource_arns[1],))

    def test_preflight_failure_is_structured_and_hides_aws_response_details(self):
        import traceback
        access, client = self.access(), PolicyClient()
        secret = "synthetic-sensitive-aws-response"
        def failed(resourceArn):
            raise RuntimeError(secret)
        client.get_resource_policy = failed
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        error = caught.exception
        self.assertEqual(error.phase, "preflight")
        self.assertEqual(error.failed_resource, RUNTIME)
        self.assertEqual(error.applied_resources, ())
        self.assertEqual(error.uncertain_resources, ())
        self.assertEqual(client.writes, [])
        self.assertNotIn(secret, "".join(traceback.format_exception(type(error), error, error.__traceback__)))

    def test_invalid_policy_documents_are_never_treated_as_missing(self):
        for policy in ("null", "true", "[]", '{"Statement":[],"Statement":[]}',
                       '{"extra":NaN}', '{"padding":"' + "\uac00" * 8000 + '"}'):
            with self.subTest(policy_length=len(policy)):
                access, client = self.access(), PolicyClient()
                client.get_resource_policy = lambda resourceArn: {"policy": policy}
                with self.assertRaises(AgentCorePolicyApplyError) as caught:
                    apply_private_access(client, access)
                self.assertEqual(caught.exception.phase, "preflight")
                self.assertEqual(client.writes, [])

    def test_keyboard_interrupt_during_write_reports_uncertainty(self):
        access, client = self.access(), PolicyClient()
        original = client.put_resource_policy
        def interrupted(**kwargs):
            original(**kwargs)
            raise KeyboardInterrupt()
        client.put_resource_policy = interrupted
        with self.assertRaises(AgentCorePolicyApplyError) as caught:
            apply_private_access(client, access)
        self.assertEqual(caught.exception.uncertain_resources, (RUNTIME,))
        self.assertEqual(client.writes, [RUNTIME])

    def test_cli_reports_uncertain_writes_and_disables_automatic_write_retries(self):
        import contextlib
        import io
        import runpy
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch
        try:
            import botocore.config
        except ImportError:
            self.skipTest("requires botocore")
        client = PolicyClient()
        client.fail_on = RUNTIME
        configurations = []
        def create_client(service, **kwargs):
            self.assertEqual(service, "bedrock-agentcore-control")
            configurations.append(kwargs["config"])
            return client
        session = types.SimpleNamespace(client=create_client)
        boto = types.SimpleNamespace(Session=lambda **kwargs: session)
        command = Path(__file__).resolve().parents[1] / "examples" / "agentcore" / "private_access.py"
        arguments = [str(command), "--runtime-arn", RUNTIME, "--caller-role-arn", ROLE,
                     "--vpce-id", VPCE, "--apply"]
        output = io.StringIO()
        with patch.dict(sys.modules, {"boto3": boto}), patch.object(sys, "argv", arguments):
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(command), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["uncertain_resources"], [RUNTIME])
        self.assertEqual(result["verified_resources"], [])
        self.assertEqual(result["phase"], "apply")
        self.assertEqual(configurations[0].connect_timeout, 5)
        self.assertEqual(configurations[0].read_timeout, 10)
        self.assertEqual(configurations[0].retries, {"total_max_attempts": 1})

    def test_requests_match_installed_aws_sdk_shapes(self):
        try:
            import botocore.session
            from botocore.stub import Stubber
        except ImportError:
            self.skipTest("requires botocore")
        access = self.access()
        client = botocore.session.get_session().create_client(
            "bedrock-agentcore-control", region_name=access.region,
            aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
        with Stubber(client) as stub:
            for arn in access.resource_arns:
                stub.add_client_error("get_resource_policy", service_error_code="ResourceNotFoundException",
                                      expected_params={"resourceArn": arn})
            for arn, policy in access.resource_policies().items():
                encoded = json.dumps(policy, sort_keys=True)
                stub.add_client_error("get_resource_policy", service_error_code="ResourceNotFoundException",
                                      expected_params={"resourceArn": arn})
                stub.add_response("put_resource_policy", {"policy": encoded},
                                  {"resourceArn": arn, "policy": encoded})
                stub.add_response("get_resource_policy", {"policy": encoded}, {"resourceArn": arn})
            self.assertEqual(apply_private_access(client, access), access.resource_arns)
            stub.assert_no_pending_responses()
