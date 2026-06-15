import os
import unittest
from unittest import mock

from core.embeddings import LangChainEmbeddingClient, create_embedding_client


class FakeEmbeddings:
    async def aembed_documents(self, texts):
        return [[float(index), 1.0] for index, _ in enumerate(texts)]

    async def aembed_query(self, text):
        return [1.0, 2.0]


class EmbeddingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_uses_langchain_async_embedding_api(self):
        client = LangChainEmbeddingClient(
            FakeEmbeddings(),
            model_name="embedding-model",
            dimension=2,
        )

        self.assertEqual(await client.embed_text("query"), [1.0, 2.0])
        self.assertEqual(
            await client.embed_texts(["a", "b"]),
            [[0.0, 1.0], [1.0, 1.0]],
        )

    def test_factory_builds_google_langchain_embeddings(self):
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "EMBEDDING_MODEL": "models/gemini-embedding-001",
                "EMBEDDING_DIM": "768",
            },
            clear=True,
        ), mock.patch("core.embeddings.GoogleGenerativeAIEmbeddings") as embeddings:
            result = create_embedding_client()

        self.assertEqual(result.model_name, "models/gemini-embedding-001")
        self.assertEqual(result.dimension, 768)
        embeddings.assert_called_once_with(
            model="models/gemini-embedding-001",
            api_key="test-key",
            output_dimensionality=768,
        )


if __name__ == "__main__":
    unittest.main()
