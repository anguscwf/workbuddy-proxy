import unittest

import server


class ModelWhitelistTest(unittest.TestCase):
    def test_required_tunnel_models_are_advertised(self):
        self.assertTrue({
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "kimi-k3-ioa",
        }.issubset(server.ADVERTISED_MODEL_IDS))

    def test_aliases_resolve_to_advertised_workbuddy_models(self):
        for alias, target in server.CURSOR_TO_WB_MAP.items():
            self.assertIn(alias, server.ADVERTISED_MODEL_IDS)
            self.assertIn(target, server.MODEL_IDS)
            self.assertEqual(server.resolve_allowed_model(alias), target)

    def test_unknown_or_malformed_models_are_rejected(self):
        for value in (
            "not-a-real-model",
            "DEEPSEEK-V4-PRO",
            "../../v1/models",
            "deepseek-v4-pro\r\nX-Test: injected",
            "",
            "   ",
            " deepseek-v4-pro",
            "deepseek-v4-pro ",
            None,
            42,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    server.resolve_allowed_model(value)


if __name__ == "__main__":
    unittest.main()
