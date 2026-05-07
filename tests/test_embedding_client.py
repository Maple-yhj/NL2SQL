import importlib
import sys
import types
import unittest


fake_google = types.ModuleType("google")
fake_genai = types.ModuleType("google.genai")
fake_genai.types = types.SimpleNamespace(
    EmbedContentConfig=lambda **kwargs: types.SimpleNamespace(**kwargs)
)
fake_google.genai = fake_genai
sys.modules.setdefault("google", fake_google)
sys.modules.setdefault("google.genai", fake_genai)

embedding_client = importlib.import_module("rag.embedding_client")


class EmbeddingValidationTests(unittest.TestCase):
    def test_validate_vectors_reports_embedding_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "Expected 2 embeddings, got 1"):
            embedding_client._validate_vectors(
                vectors=[[0.1, 0.2, 0.3]],
                expected_count=2,
                expected_dim=3,
            )

    def test_validate_vectors_reports_dimension_mismatch(self):
        with self.assertRaisesRegex(
            ValueError,
            "Expected embedding dim 3 at index 1, got 2",
        ):
            embedding_client._validate_vectors(
                vectors=[[0.1, 0.2, 0.3], [0.4, 0.5]],
                expected_count=2,
                expected_dim=3,
            )


if __name__ == "__main__":
    unittest.main()
