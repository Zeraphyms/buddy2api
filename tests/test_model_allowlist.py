import unittest

from fastapi import HTTPException

from core import converter


class ModelAllowlistTests(unittest.TestCase):
    def tearDown(self):
        converter.MODEL_ALLOWLIST = None

    def test_no_allowlist_allows_everything(self):
        converter.MODEL_ALLOWLIST = None
        converter._check_model("whatever-model")  # 不应抛错

    def test_allowed_model_passes(self):
        converter.MODEL_ALLOWLIST = {"deepseek-v4.1-flash"}
        converter._check_model("deepseek-v4.1-flash")

    def test_blocked_model_raises_403(self):
        converter.MODEL_ALLOWLIST = {"deepseek-v4.1-flash"}
        with self.assertRaises(HTTPException) as ctx:
            converter._check_model("glm-5.3")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("glm-5.3", str(ctx.exception.detail))

    def test_models_list_is_trimmed(self):
        converter.MODEL_ALLOWLIST = {"deepseek-v4.1-flash"}
        self.assertEqual(converter.get_available_models(), ["deepseek-v4.1-flash"])


if __name__ == "__main__":
    unittest.main()
