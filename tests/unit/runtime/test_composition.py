from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(SRC_ROOT))


def _documents() -> tuple[dict, dict, dict]:
    domain = {
        "apiVersion": "dataagent.io/domain/v1",
        "kind": "DomainPack",
        "metadata": {"name": "commerce", "version": "1.0.0"},
        "spec": {
            "entities": {
                "commerce.Order": {
                    "grain": ["order_id"],
                    "fields": {
                        "order_id": {"type": "string", "nullable": False}
                    },
                }
            }
        },
    }
    enterprise = {
        "apiVersion": "dataagent.io/enterprise/v1",
        "kind": "EnterpriseDataBinding",
        "metadata": {"name": "olist", "version": "1.0.0"},
        "spec": {
            "domains": [{"ref": "commerce@1.0.0"}],
            "sources": {
                "sales": {
                    "connector": "postgres",
                    "connectionRef": "secret://olist/local/database",
                    "readOnly": True,
                }
            },
            "bindings": {
                "commerce.Order": {
                    "source": "sales",
                    "relation": "public.olist_orders_dataset",
                    "grain": ["order_id"],
                    "fields": {"order_id": {"column": "order_id"}},
                }
            },
            "policies": {"maxRows": 1000, "queryTimeoutSeconds": 10},
        },
    }
    deployment = {
        "apiVersion": "dataagent.io/deployment/v1",
        "kind": "DeploymentProfile",
        "metadata": {"name": "olist-local"},
        "spec": {
            "enterprisePack": "olist@1.0.0",
            "environment": "local",
            "secretsProvider": "environment",
            "datasourceSecrets": {
                "secret://olist/local/database": "DATABASE_URL"
            },
            "runtime": {"maxToolCalls": 24},
        },
    }
    return domain, enterprise, deployment


class RuntimeBundleCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.packs = importlib.import_module("data_agent.runtime.packs")
            self.composition = importlib.import_module(
                "data_agent.runtime.composition"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"runtime bundle compiler is missing: {exc}")

    def _compile(self, domain_doc: dict, enterprise_doc: dict, deployment_doc: dict):
        return self.composition.compile_runtime_bundle(
            self.packs.DomainPack.model_validate(domain_doc),
            self.packs.EnterpriseDataBinding.model_validate(enterprise_doc),
            self.packs.DeploymentProfile.model_validate(deployment_doc),
            runtime_version="1.0.0",
            skill_versions={"commerce.analytics": "1.0.0"},
            tool_registry_version="1.0.0",
            schema_fingerprint="olist-schema-v1",
        )

    def test_compile_runtime_bundle_is_canonical_and_digest_stable(self) -> None:
        domain, enterprise, deployment = _documents()
        first = self._compile(domain, enterprise, deployment)

        reordered_domain = {
            "spec": domain["spec"],
            "metadata": {"version": "1.0.0", "name": "commerce"},
            "kind": "DomainPack",
            "apiVersion": "dataagent.io/domain/v1",
        }
        second = self._compile(reordered_domain, enterprise, deployment)

        self.assertIsInstance(first, self.composition.ResolvedRuntimeBundle)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            self.composition.canonical_json(first),
            self.composition.canonical_json(second),
        )
        self.assertEqual(len(first.digest), 64)
        self.assertEqual(first.physical_bindings["commerce.Order"]["source"], "sales")
        self.assertNotIn("secret://", self.composition.canonical_json(first))
        self.assertNotIn("DATABASE_URL", self.composition.canonical_json(first))

        changed_deployment = _documents()[2]
        changed_deployment["spec"]["runtime"]["maxToolCalls"] = 12
        changed = self._compile(domain, enterprise, changed_deployment)
        self.assertNotEqual(first.digest, changed.digest)

    def test_compile_runtime_bundle_rejects_pack_reference_mismatch(self) -> None:
        domain, enterprise, deployment = _documents()
        enterprise["spec"]["domains"] = [{"ref": "finance@1.0.0"}]

        with self.assertRaisesRegex(ValueError, "domain pack"):
            self._compile(domain, enterprise, deployment)


if __name__ == "__main__":
    unittest.main()
