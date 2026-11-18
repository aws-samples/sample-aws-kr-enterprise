"""Public protocol signatures and exception diagnostics consumed by applications."""
import unittest

import kiro_agent_sdk as sdk


class ExportedRuntimeContractTests(unittest.TestCase):

    def test_protocol_process_and_result_error_payloads_remain_distinct(self):
        rpc = sdk.JSONRPCError({"code": -32602, "message": "bad parameters", "data": {"future": True}})
        self.assertIsInstance(rpc, sdk.ProtocolError)
        self.assertEqual((rpc.code, rpc.data), (-32602, {"future": True}))
        original = ValueError("synthetic parser error")
        malformed = sdk.CLIJSONDecodeError("not json", original)
        self.assertEqual(malformed.line, "not json")
        self.assertIs(malformed.original_error, original)
        process = sdk.ProcessError("process failed", exit_code=7, stderr="synthetic stderr")
        self.assertEqual((process.exit_code, process.stderr), (7, "synthetic stderr"))
        self.assertIn("exit code: 7", str(process))
        result = sdk.ResultError("result failed", data={"kind": "error_fixture", "errors": [" one ", ""],
                                 "result": "partial", "session_id": "session", "future": True}, exit_code=8)
        self.assertIsInstance(result, sdk.ProcessError)
        self.assertEqual((result.kind, result.errors, result.result, result.session_id, result.exit_code),
                         ("error_fixture", ["one"], "partial", "session", 8))
        self.assertTrue(result.data["future"])


if __name__ == "__main__":
    unittest.main()
