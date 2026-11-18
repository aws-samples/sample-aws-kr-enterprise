"""AgentCoreConfig rejects invalid scalar settings while accepting a valid non-default value."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

NUMBER_BAD = (object(), "", -1, 0, True, float("nan"), float("inf"))
TEXT_BAD = (object(), "", "bad\0value", -1, True)

PRO_DEVICE = {"auth_mode": "device", "device_license": "pro", "device_start_url": "https://org.awsapps.com/start"}

# field -> (base without the field's own conflicts, valid non-default value, invalid values)
CASES = {
    "state_dir": ({"api_key": "synthetic"}, None, (object(), "bad\0dir", -1, True, float("nan"))),
    "cli_path": ({"api_key": "synthetic"}, "custom-kiro-cli", TEXT_BAD),
    "agent_engine": ({"api_key": "synthetic"}, "v2", (object(), "", "v4", True)),
    "auth_mode": ({}, "cli", (object(), "", "unknown", True)),
    "api_key": ({}, "synthetic", TEXT_BAD),
    "startup_timeout": ({"api_key": "synthetic"}, 12.5, NUMBER_BAD),
    "turn_timeout": ({"api_key": "synthetic"}, 12.5, NUMBER_BAD),
    "max_sessions": ({"api_key": "synthetic"}, 2, NUMBER_BAD),
    "max_prompt_bytes": ({"api_key": "synthetic"}, 4096, NUMBER_BAD),
    "max_body_bytes": ({"api_key": "synthetic"}, 8192, NUMBER_BAD),
    "interaction_timeout": ({"api_key": "synthetic"}, 12.5, NUMBER_BAD),
    "session_authorization_timeout": ({"api_key": "synthetic"}, 2.5, NUMBER_BAD),
    "secret_id": ({}, "secret-ref", TEXT_BAD),
    "secret_json_key": ({"secret_id": "secret-ref"}, "key", (object(), "", -1, True)),
    "device_region": (PRO_DEVICE, "eu-west-1", (object(), "", "bad\0", "not a region", 0, -1)),
    "device_timeout": ({"auth_mode": "device"}, 600, (0, -1, 901, True, float("nan"), float("inf"))),
}


@unittest.skipUnless(importlib.util.find_spec("starlette"), "AgentCore extra is absent")
class AgentCoreConfigInputContracts(unittest.TestCase):
    def test_each_setting_accepts_a_valid_value_and_rejects_invalid_values(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig
        with tempfile.TemporaryDirectory() as directory:
            for field, (base, valid, invalid) in CASES.items():
                values = {"state_dir": Path(directory), **base}
                chosen = Path(directory) / "state" if valid is None else valid
                with self.subTest(field=field, value="valid"):
                    self.assertEqual(getattr(AgentCoreConfig(**{**values, field: chosen}), field), chosen)
                for value in invalid:
                    with self.subTest(field=field, value=repr(value)[:20]), self.assertRaises(ValueError):
                        AgentCoreConfig(**{**values, field: value})


if __name__ == "__main__":
    unittest.main()
