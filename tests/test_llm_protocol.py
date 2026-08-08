import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.adapters.llm import (
    LangChainLLM,
    ModelProviderConfig,
    create_llm,
    create_llm_from_config,
    resolve_llm_config,
)


class FakeMessage:
    content = "model output"


class FakeChatModel:
    def __init__(self):
        self.messages = None
        self.kwargs = None

    async def ainvoke(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return FakeMessage()


class LLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_langchain_adapter_converts_prompt_to_chat_messages(self):
        model = FakeChatModel()
        llm = LangChainLLM(model)

        result = await llm.complete("question", system="rules")

        self.assertEqual(result, "model output")
        self.assertEqual(model.messages[0].content, "rules")
        self.assertEqual(model.messages[1].content, "question")

    async def test_langchain_adapter_normalizes_text_blocks_and_token_limit(self):
        class BlockMessage:
            content = [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "model "},
                {"type": "text", "text": "output"},
            ]

        model = FakeChatModel()
        model.ainvoke = mock.AsyncMock(return_value=BlockMessage())
        llm = LangChainLLM(
            model,
            max_output_tokens_parameter="max_output_tokens",
        )

        result = await llm.complete("question", max_output_tokens=321)

        self.assertEqual(result, "model output")
        model.ainvoke.assert_awaited_once()
        self.assertEqual(
            model.ainvoke.await_args.kwargs,
            {"max_output_tokens": 321},
        )

    def test_create_llm_uses_deepseek_langchain_model(self):
        environment = {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key",
            "DEFAULT_MODEL_NAME": "deepseek-chat",
        }
        with mock.patch(
            "data_agent.adapters.llm.load_dotenv",
            create=True,
            side_effect=AssertionError("runtime dotenv load is blocking"),
        ), mock.patch("data_agent.adapters.llm.ChatOpenAI") as chat_openai:
            result = create_llm(environment)

        self.assertIsInstance(result, LangChainLLM)
        chat_openai.assert_called_once_with(
            model="deepseek-chat",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            temperature=0,
        )

    def test_provider_aliases_resolve_to_canonical_transports(self):
        cases = (
            ("gpt", "OPENAI_API_KEY", "openai", "openai-compatible"),
            ("qwen", "DASHSCOPE_API_KEY", "qwen", "openai-compatible"),
            ("gemini", "GEMINI_API_KEY", "google", "google"),
            ("claude", "ANTHROPIC_API_KEY", "anthropic", "anthropic"),
            ("zhipu", "ZAI_API_KEY", "glm", "openai-compatible"),
            ("deepseek", "DEEPSEEK_API_KEY", "deepseek", "openai-compatible"),
        )

        for alias, key_name, provider, transport in cases:
            with self.subTest(alias=alias):
                config = resolve_llm_config(
                    {
                        "LLM_PROVIDER": alias,
                        "DEFAULT_MODEL_NAME": f"{provider}-model",
                        key_name: "test-key",
                    }
                )
                self.assertEqual(config.provider, provider)
                self.assertEqual(config.transport, transport)
                self.assertEqual(config.model_name, f"{provider}-model")

    def test_custom_openai_compatible_provider_requires_base_url(self):
        environment = {
            "LLM_PROVIDER": "custom",
            "DEFAULT_MODEL_NAME": "private-model",
            "LLM_API_KEY": "test-key",
        }

        with self.assertRaisesRegex(ValueError, "LLM_BASE_URL"):
            resolve_llm_config(environment)

        config = resolve_llm_config(
            {**environment, "LLM_BASE_URL": "https://models.example/v1"}
        )
        self.assertEqual(config.provider, "openai-compatible")
        self.assertEqual(config.base_url, "https://models.example/v1")

    def test_factory_uses_native_google_and_anthropic_adapters(self):
        google = ModelProviderConfig(
            provider="google",
            model_name="gemini-test",
            transport="google",
            api_key="google-key",
        )
        anthropic = ModelProviderConfig(
            provider="anthropic",
            model_name="claude-test",
            transport="anthropic",
            api_key="anthropic-key",
        )

        with mock.patch(
            "data_agent.adapters.llm.ChatGoogleGenerativeAI"
        ) as google_model:
            google_llm = create_llm_from_config(google)
        with mock.patch(
            "data_agent.adapters.llm._create_anthropic_model"
        ) as anthropic_model:
            anthropic_llm = create_llm_from_config(anthropic)

        google_model.assert_called_once_with(
            model="gemini-test",
            api_key="google-key",
            temperature=0,
        )
        anthropic_model.assert_called_once_with(anthropic)
        self.assertEqual(
            google_llm.max_output_tokens_parameter,
            "max_output_tokens",
        )
        self.assertEqual(
            anthropic_llm.max_output_tokens_parameter,
            "max_tokens",
        )

    def test_explicit_environment_is_not_overridden_by_process_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "deepseek",
                "DEFAULT_MODEL_NAME": "wrong-model",
                "DEEPSEEK_API_KEY": "wrong-key",
            },
            clear=True,
        ):
            config = resolve_llm_config(
                {
                    "LLM_PROVIDER": "gpt",
                    "DEFAULT_MODEL_NAME": "right-model",
                    "OPENAI_API_KEY": "right-key",
                }
            )

        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.model_name, "right-model")
        self.assertEqual(config.api_key, "right-key")


if __name__ == "__main__":
    unittest.main()
