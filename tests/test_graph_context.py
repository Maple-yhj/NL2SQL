import unittest
from unittest import mock

from graph import context as context_module
from graph.context import GraphContext
from graph.pipeline import graph


class FakeLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return "unused"


class FakeEmbeddings:
    model_name = "fake"
    dimension = 3

    async def embed_text(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_texts(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class GraphContextTests(unittest.TestCase):
    def test_graph_context_schema_only_contains_serializable_settings(self):
        schema = graph.get_context_jsonschema()

        self.assertEqual(
            set(schema["properties"]),
            {
                "dsn",
                "timeout_ms",
                "max_limit",
                "max_validation_attempts",
            },
        )

    def test_graph_context_ignores_langsmith_thread_id(self):
        context = GraphContext(
            llm=FakeLLM(),
            embeddings=FakeEmbeddings(),
            thread_id="langsmith-thread",
        )

        self.assertFalse(hasattr(context, "thread_id"))

    def test_graph_context_creates_default_runtime_clients(self):
        llm = FakeLLM()
        embeddings = FakeEmbeddings()
        with mock.patch.object(
            context_module, "create_llm", return_value=llm, create=True
        ) as create_llm, mock.patch.object(
            context_module,
            "create_embedding_client",
            return_value=embeddings,
            create=True,
        ) as create_embeddings:
            context = GraphContext()

        self.assertIs(context.llm, llm)
        self.assertIs(context.embeddings, embeddings)
        create_llm.assert_called_once_with()
        create_embeddings.assert_called_once_with()

    def test_graph_context_preserves_explicit_runtime_clients(self):
        llm = FakeLLM()
        embeddings = FakeEmbeddings()

        context = GraphContext(llm=llm, embeddings=embeddings)

        self.assertIs(context.llm, llm)
        self.assertIs(context.embeddings, embeddings)


if __name__ == "__main__":
    unittest.main()
