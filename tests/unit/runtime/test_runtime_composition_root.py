from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class _Pool:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _PoolFactory:
    def __init__(self) -> None:
        self.calls = []
        self.pool = _Pool()

    async def __call__(self, *, dsn: str, min_size: int, max_size: int):
        self.calls.append((dsn, min_size, max_size))
        return self.pool


class _Model:
    model_id = "planner"
    version = "fake-v1"

    async def complete(self, prompt: str, **kwargs) -> str:
        return "{}"


class RuntimeCompositionRootTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_is_inert_and_build_uses_one_factory_pool(self) -> None:
        module = importlib.import_module("data_agent.runtime.composition_root")
        factory = _PoolFactory()

        composition = await module.build_olist_runtime(
            project_root=ROOT,
            pool_factory=factory,
            model_client_factory=lambda: _Model(),
            environment={"DATABASE_URL": "postgresql://runtime-only"},
        )

        self.assertEqual(factory.calls, [("postgresql://runtime-only", 1, 10)])
        self.assertEqual(
            composition.dependencies.tool_registry.names(),
            (
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            ),
        )
        self.assertIs(composition.dependencies.memory._pool, factory.pool)
        self.assertIs(
            composition.dependencies.bundle_store.snapshot(),
            composition.snapshot,
        )
        self.assertIs(composition.dependencies.executor.graph, composition.dependencies.graph)

        await composition.close()
        await composition.close()
        self.assertEqual(factory.pool.close_calls, 1)

    async def test_generic_builder_accepts_explicit_verified_bundle_paths(
        self,
    ) -> None:
        from data_agent.runtime.bundle_store import BundlePaths
        from data_agent.runtime.composition_root import build_runtime

        factory = _PoolFactory()
        composition = await build_runtime(
            bundle=BundlePaths(
                domain_root=ROOT / "packs" / "domains" / "commerce",
                enterprise_root=ROOT / "packs" / "enterprises" / "olist",
                deployment_profile=(
                    ROOT / "packs" / "deployments" / "olist-local.yaml"
                ),
                pack_lock=(
                    ROOT / "packs" / "enterprises" / "olist" / "pack.lock"
                ),
                schema_catalog=ROOT / "schema_catalog.json",
                bundle_manifest=(
                    ROOT / "generated" / "bundles" / "olist-local.json"
                ),
            ),
            pool_factory=factory,
            model_client_factory=lambda: _Model(),
            environment={"DATABASE_URL": "postgresql://runtime-only"},
        )

        self.assertEqual(
            composition.snapshot.enterprise_binding.metadata.name,
            "olist",
        )
        await composition.close()

    async def test_missing_secret_fails_before_pool_creation(self) -> None:
        from data_agent.runtime.composition_root import build_olist_runtime

        factory = _PoolFactory()
        with self.assertRaisesRegex(ValueError, "secret|DATABASE_URL"):
            await build_olist_runtime(
                project_root=ROOT,
                pool_factory=factory,
                model_client_factory=lambda: _Model(),
                environment={},
            )
        self.assertEqual(factory.calls, [])

    async def test_failed_assembly_closes_the_single_created_pool(self) -> None:
        from data_agent.runtime.composition_root import build_olist_runtime

        factory = _PoolFactory()

        def broken_model_factory():
            raise RuntimeError("model unavailable")

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            await build_olist_runtime(
                project_root=ROOT,
                pool_factory=factory,
                model_client_factory=broken_model_factory,
                environment={"DATABASE_URL": "postgresql://runtime-only"},
            )
        self.assertEqual(factory.pool.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
