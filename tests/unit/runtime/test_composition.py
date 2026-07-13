from __future__ import annotations

import importlib
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError


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
            "policies": {
                "accessMode": "single_tenant",
                "maxRows": 1000,
                "queryTimeoutSeconds": 10,
                "relationAllowlist": ["public.olist_orders_dataset"],
            },
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
            pack_lock={
                "apiVersion": "dataagent.io/lock/v1",
                "enterprisePack": "olist@1.0.0",
                "accessMode": "single_tenant",
                "domains": ["commerce@1.0.0"],
                "schemaFingerprint": "0d9c342b75693bf64c46473b0afcc117ebd54bb6dabf9143d404f24d68015bbb",
                "policyDigest": "ed9662482e7a64e79508d71bcbb3d87a551bb97766fd7cb27a7cacd65c8fbe1a",
                "relations": ["public.olist_orders_dataset"],
            },
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

    def test_compile_runtime_bundle_rejects_unresolved_binding_references(self) -> None:
        domain, enterprise, deployment = _documents()
        invalid_enterprises = []

        missing_source = deepcopy(enterprise)
        missing_source["spec"]["bindings"]["commerce.Order"]["source"] = "missing"
        invalid_enterprises.append(missing_source)

        missing_entity = deepcopy(enterprise)
        binding = missing_entity["spec"]["bindings"].pop("commerce.Order")
        missing_entity["spec"]["bindings"]["commerce.Customer"] = binding
        invalid_enterprises.append(missing_entity)

        missing_field = deepcopy(enterprise)
        missing_field["spec"]["bindings"]["commerce.Order"]["fields"] = {
            "missing_field": {"column": "order_id"}
        }
        missing_field["spec"]["bindings"]["commerce.Order"]["grain"] = [
            "missing_field"
        ]
        invalid_enterprises.append(missing_field)

        unmapped_grain = deepcopy(enterprise)
        unmapped_grain["spec"]["bindings"]["commerce.Order"]["fields"] = {}
        invalid_enterprises.append(unmapped_grain)

        missing_tenant_scope_field = deepcopy(enterprise)
        missing_tenant_scope_field["spec"]["policies"]["tenantScope"] = {
            "mode": "seller_id",
            "canonicalField": "commerce.Order.missing_field",
            "principalClaim": "tenant_id",
        }
        invalid_enterprises.append(missing_tenant_scope_field)

        for invalid_enterprise in invalid_enterprises:
            with self.subTest(enterprise=invalid_enterprise), self.assertRaises(
                ValueError
            ):
                self._compile(domain, invalid_enterprise, deployment)

    def test_resolved_runtime_bundle_is_deeply_immutable_and_digest_checked(self) -> None:
        domain, enterprise, deployment = _documents()
        bundle = self._compile(domain, enterprise, deployment)

        with self.assertRaises(TypeError):
            bundle.skill_versions["other.skill"] = "1.0.0"
        with self.assertRaises(TypeError):
            bundle.physical_bindings["commerce.Order"]["source"] = "other"
        with self.assertRaises(TypeError):
            bundle.semantic_model["entities"]["commerce.Order"]["grain"][0] = (
                "other_id"
            )
        with self.assertRaises(TypeError):
            bundle.model_copy(update={"schema_fingerprint": "tampered"})

        tampered = bundle.model_dump(mode="json")
        tampered["schema_fingerprint"] = "tampered"
        with self.assertRaises(ValidationError):
            self.composition.ResolvedRuntimeBundle.model_validate(tampered)

    def test_compile_requires_binding_grain_to_equal_domain_grain(self) -> None:
        domain, enterprise, deployment = _documents()
        domain["spec"]["entities"]["commerce.Order"]["fields"]["customer_id"] = {
            "type": "string"
        }
        enterprise["spec"]["bindings"]["commerce.Order"]["grain"] = [
            "customer_id"
        ]
        enterprise["spec"]["bindings"]["commerce.Order"]["fields"][
            "customer_id"
        ] = {"column": "customer_id"}

        with self.assertRaisesRegex(ValueError, "grain"):
            self._compile(domain, enterprise, deployment)

    def test_compile_requires_tenant_scope_to_have_a_physical_mapping(self) -> None:
        domain, enterprise, deployment = _documents()
        domain["spec"]["entities"]["commerce.Seller"] = {
            "grain": ["seller_id"],
            "fields": {"seller_id": {"type": "string"}},
        }
        enterprise["spec"]["policies"]["tenantScope"] = {
            "mode": "seller_id",
            "canonicalField": "commerce.Seller.seller_id",
            "principalClaim": "tenant_id",
            "adminBypass": {
                "principalClaim": "roles",
                "allowedRoles": ["admin"],
            },
            "ownershipPaths": {
                "commerce.Order": [],
                "commerce.Seller": [],
            },
        }
        enterprise["spec"]["policies"]["accessMode"] = "tenant_scoped"
        with self.assertRaisesRegex(ValueError, "tenant scope|canonicalField"):
            self._compile(domain, enterprise, deployment)

        domain, enterprise, deployment = _documents()
        domain["spec"]["entities"]["commerce.Order"]["fields"]["seller_id"] = {
            "type": "string"
        }
        enterprise["spec"]["policies"]["tenantScope"] = {
            "mode": "seller_id",
            "canonicalField": "commerce.Order.seller_id",
            "principalClaim": "tenant_id",
            "adminBypass": {
                "principalClaim": "roles",
                "allowedRoles": ["admin"],
            },
            "ownershipPaths": {"commerce.Order": []},
        }
        enterprise["spec"]["policies"]["accessMode"] = "tenant_scoped"
        with self.assertRaisesRegex(ValueError, "tenant scope|canonicalField"):
            self._compile(domain, enterprise, deployment)

    def test_compile_requires_exactly_one_matching_domain_reference(self) -> None:
        domain, enterprise, deployment = _documents()
        enterprise["spec"]["domains"].append({"ref": "finance@1.0.0"})

        with self.assertRaisesRegex(ValueError, "domain pack"):
            self._compile(domain, enterprise, deployment)


if __name__ == "__main__":
    unittest.main()
