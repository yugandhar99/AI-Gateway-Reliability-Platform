import unittest

from gateway.service import MAX_PROMPT_LENGTH, ValidationError, chat


class ChatTests(unittest.TestCase): 
    def test_all_providers_and_normalization(self):
        for name in ("OpenAI", "Anthropic", "Gemini"):
            with self.subTest(provider=name):
                result = chat({"prompt": " Hello 世界 ", "provider": name.lower()})
                self.assertEqual(result["provider"], name)
                self.assertEqual(result["status"], "success")
                self.assertIn("Hello 世界", result["response"])
                self.assertIn("Simulated", result["response"])
                self.assertGreaterEqual(result["response_time_ms"], 0)

    def test_invalid_payloads(self):
        cases = [None, [], "text", {}, {"prompt": "hi"},
                 {"prompt": "hi", "provider": "unknown"}]
        for value in (None, True, 42, [], {}, "", "   "):
            cases.append({"prompt": value, "provider": "OpenAI"})
            cases.append({"prompt": "hi", "provider": value})
        cases.append({"prompt": "a" * (MAX_PROMPT_LENGTH + 1), "provider": "OpenAI"})
        for payload in cases:
            with self.subTest(payload=repr(payload)[:100]):
                with self.assertRaises(ValidationError):
                    chat(payload)

    def test_prompt_length_boundary(self):
        self.assertEqual(chat({"prompt": "a" * MAX_PROMPT_LENGTH,
                               "provider": "OpenAI"})["status"], "success")
