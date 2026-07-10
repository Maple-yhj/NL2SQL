from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class OListEnterpriseBindingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from data_agent.runtime import profile_loader

        cls.loader = profile_loader
        cls.pack_root = PROJECT_ROOT / "packs" / "enterprises" / "olist"
        cls.deployment_path = PROJECT_ROOT / "packs" / "deployments" / "olist-local.yaml"
        cls.domain_root = PROJECT_ROOT / "packs" / "domains" / "commerce"
        cls.catalog_path = PROJECT_ROOT / "schema_catalog.json"

    def test_olist_binding_maps_all_nine_canonical_entities(self) -> None:
        binding = self.loader.load_enterprise_binding(self.pack_root)
        domain = self.loader.load_domain_pack(self.domain_root)

        self.assertEqual(binding.metadata.name, "olist")
        self.assertEqual(binding.metadata.version, "1.0.0")
        self.assertEqual(set(binding.spec.bindings), set(domain.spec.entities))
        self.assertEqual(
            {item.relation for item in binding.spec.bindings.values()},
            {
                "public.olist_orders_dataset",
                "public.olist_order_items_dataset",
                "public.olist_customers_dataset",
                "public.olist_sellers_dataset",
                "public.olist_products_dataset",
                "public.olist_order_payments_dataset",
                "public.olist_order_reviews_dataset",
                "public.olist_geolocation_dataset",
                "public.product_category_name_translation",
            },
        )
        for entity_id, entity in domain.spec.entities.items():
            mapped = binding.spec.bindings[entity_id]
            self.assertEqual(tuple(mapped.grain), tuple(entity.grain))
            self.assertEqual(set(mapped.fields), set(entity.fields))

    def test_binding_schema_catalog_and_bundle_are_deterministic(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile

        binding = self.loader.load_enterprise_binding(self.pack_root)
        domain = self.loader.load_domain_pack(self.domain_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        first = compile_runtime_bundle(
            domain,
            binding,
            deployment,
            runtime_version="1.0.0",
            skill_versions={"commerce.analytics": "1.0.0"},
            tool_registry_version="1.0.0",
            schema_catalog=self.catalog_path,
        )
        second = compile_runtime_bundle(
            domain,
            binding,
            deployment,
            runtime_version="1.0.0",
            skill_versions={"commerce.analytics": "1.0.0"},
            tool_registry_version="1.0.0",
            schema_catalog=self.catalog_path,
        )
        self.assertEqual(first.digest, second.digest)
        self.assertNotIn("secret://", json.dumps(first.model_dump(mode="json")))
        self.assertNotIn("DATABASE_URL", json.dumps(first.model_dump(mode="json")))
        self.assertEqual(
            binding.spec.policies.tenant_scope.canonical_field,
            "commerce.Seller.seller_id",
        )

    def test_schema_drift_unknown_relation_column_and_type_are_rejected(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile, EnterpriseDataBinding

        domain = self.loader.load_domain_pack(self.domain_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        original = self.loader.load_enterprise_binding(self.pack_root)
        raw = original.model_dump(mode="python", by_alias=True)
        for mutation in (
            lambda doc: doc["spec"]["bindings"]["commerce.Order"].update(
                relation="public.unknown_relation"
            ),
            lambda doc: doc["spec"]["bindings"]["commerce.Order"]["fields"][
                "order_id"
            ].update(column="unknown_column"),
            lambda doc: doc["spec"]["bindings"]["commerce.Order"]["fields"][
                "order_id"
            ].update(cast="integer"),
        ):
            invalid = copy.deepcopy(raw)
            mutation(invalid)
            candidate = EnterpriseDataBinding.model_validate(invalid)
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    compile_runtime_bundle(
                        domain,
                        candidate,
                        deployment,
                        runtime_version="1.0.0",
                        skill_versions={},
                        tool_registry_version="1.0.0",
                        schema_catalog=self.catalog_path,
                    )

    def test_missing_secret_and_executable_configuration_are_rejected(self) -> None:
        from data_agent.runtime.packs import EnterpriseDataBinding

        document = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        document["spec"]["sources"]["sales"]["connectionRef"] = (
            "secret://olist/local/missing"
        )
        loaded = EnterpriseDataBinding.model_validate(document)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path,
            __import__("data_agent.runtime.packs", fromlist=["DeploymentProfile"]).DeploymentProfile,
        )
        with self.assertRaises(ValueError):
            self.loader.compile_profile_bundle(
                self.domain_root, loaded, deployment, self.catalog_path
            )

        document = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        document["spec"]["bindings"]["commerce.Order"]["sql"] = "SELECT 1"
        with self.assertRaises(ValidationError):
            EnterpriseDataBinding.model_validate(document)


if __name__ == "__main__":
    unittest.main()
