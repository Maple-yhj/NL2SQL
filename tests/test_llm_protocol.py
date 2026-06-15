import os
import unittest
from unittest import mock

from core.llm import LangChainLLM, create_llm


class FakeMessage:
    content = "model output"


class FakeChatModel:
    def __init__(self):
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return FakeMessage()


class LLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_langchain_adapter_converts_prompt_to_chat_messages(self):
        model = FakeChatModel()
        llm = LangChainLLM(model)

        result = await llm.complete("question", system="rules")

        self.assertEqual(result, "model output")
        self.assertEqual(model.messages[0].content, "rules")
        self.assertEqual(model.messages[1].content, "question")

    def test_create_llm_uses_deepseek_langchain_model(self):
        with mock.patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-key",
                "DEFAULT_MODEL_NAME": "deepseek-chat",
            },
            clear=True,
        ), mock.patch("core.llm.ChatOpenAI") as chat_openai:
            result = create_llm()

        self.assertIsInstance(result, LangChainLLM)
        chat_openai.assert_called_once_with(
            model="deepseek-chat",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            temperature=0,
        )


if __name__ == "__main__":
    unittest.main()
