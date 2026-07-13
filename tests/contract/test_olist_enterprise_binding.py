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

    def _compile_binding_document(self, document: dict):
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile, EnterpriseDataBinding

        return compile_runtime_bundle(
            self.loader.load_domain_pack(self.domain_root),
            EnterpriseDataBinding.model_validate(document),
            self.loader.load_pack_yaml(self.deployment_path, DeploymentProfile),
            runtime_version="1.0.0",
            skill_versions={},
            tool_registry_version="1.0.0",
            schema_catalog=self.catalog_path,
            pack_lock=self.pack_root / "pack.lock",
        )

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

    def test_olist_deployment_uses_one_governed_database_secret(self) -> None:
        from data_agent.runtime.packs import DeploymentProfile

        deployment = self.loader.load_pack_yaml(
            self.deployment_path,
            DeploymentProfile,
        )

        self.assertEqual(
            deployment.spec.datasource_secrets,
            {"secret://olist/local/database": "DATABASE_URL"},
        )
        self.assertIsNone(deployment.spec.memory_database_ref)

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

    def test_olist_compile_requires_allowlist_and_seller_scope(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile, EnterpriseDataBinding

        domain = self.loader.load_domain_pack(self.domain_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        original = self.loader.load_enterprise_binding(self.pack_root)
        raw = original.model_dump(mode="python", by_alias=True)

        without_allowlist = copy.deepcopy(raw)
        without_allowlist["spec"]["policies"]["relationAllowlist"] = []
        with self.assertRaises(ValidationError):
            EnterpriseDataBinding.model_validate(without_allowlist)

        without_scope = copy.deepcopy(raw)
        without_scope["spec"]["policies"].pop("tenantScope")
        with self.assertRaises(ValidationError):
            EnterpriseDataBinding.model_validate(without_scope)

    def test_olist_compile_rejects_stale_schema_fingerprint(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile

        domain = self.loader.load_domain_pack(self.domain_root)
        binding = self.loader.load_enterprise_binding(self.pack_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        with self.assertRaisesRegex(ValueError, "schema fingerprint"):
            compile_runtime_bundle(
                domain,
                binding,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_fingerprint="stale",
            )

    def test_publication_fails_closed_on_missing_or_stale_pack_lock(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile

        domain = self.loader.load_domain_pack(self.domain_root)
        binding = self.loader.load_enterprise_binding(self.pack_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        with self.assertRaisesRegex(ValueError, "catalog"):
            compile_runtime_bundle(
                domain,
                binding,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_catalog=self.pack_root / "missing-catalog.json",
                pack_lock=self.pack_root / "pack.lock",
            )

        stale_lock = self.pack_root / "pack.lock"
        raw = stale_lock.read_text(encoding="utf-8").replace(
            "0d9c342b75693bf64c46473b0afcc117ebd54bb6dabf9143d404f24d68015bbb",
            "0" * 64,
        )
        temporary = self.pack_root / ".stale-pack.lock"
        temporary.write_text(raw, encoding="utf-8")
        self.addCleanup(temporary.unlink)
        with self.assertRaisesRegex(ValueError, "lock"):
            compile_runtime_bundle(
                domain,
                binding,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_catalog=self.catalog_path,
                pack_lock=temporary,
            )

    def test_catalog_duplicate_relation_nullability_and_timezone_drift_are_rejected(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile, EnterpriseDataBinding

        domain = self.loader.load_domain_pack(self.domain_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        original = self.loader.load_enterprise_binding(self.pack_root)
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        duplicate = copy.deepcopy(catalog)
        duplicate.append(copy.deepcopy(duplicate[0]))
        with self.assertRaisesRegex(ValueError, "duplicate relation"):
            compile_runtime_bundle(
                domain,
                original,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_catalog=duplicate,
            )

        raw = original.model_dump(mode="python", by_alias=True)
        raw["spec"]["bindings"]["commerce.Order"]["fields"]["purchased_at"][
            "timezone"
        ] = None
        candidate = EnterpriseDataBinding.model_validate(raw)
        with self.assertRaisesRegex(ValueError, "timezone"):
            compile_runtime_bundle(
                domain,
                candidate,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_catalog=self.catalog_path,
            )

    def test_target_locale_is_a_typed_constant_not_a_translation_column(self) -> None:
        binding = self.loader.load_enterprise_binding(self.pack_root)
        target_locale = binding.spec.bindings["commerce.CategoryTranslation"].fields[
            "target_locale"
        ]
        self.assertIsNone(target_locale.column)
        self.assertEqual(target_locale.value, "en")

    def test_bundle_manifest_is_rebuildable_and_secret_free(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle, write_bundle_manifest
        from data_agent.runtime.packs import DeploymentProfile

        domain = self.loader.load_domain_pack(self.domain_root)
        binding = self.loader.load_enterprise_binding(self.pack_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        bundle = compile_runtime_bundle(
            domain,
            binding,
            deployment,
            runtime_version="1.0.0",
            skill_versions={"commerce.analytics": "1.0.0"},
            tool_registry_version="1.0.0",
            schema_catalog=self.catalog_path,
        )
        first = PROJECT_ROOT / "tests" / "contract" / ".olist-manifest-1.json"
        second = PROJECT_ROOT / "tests" / "contract" / ".olist-manifest-2.json"
        self.addCleanup(first.unlink)
        self.addCleanup(second.unlink)
        write_bundle_manifest(
            bundle,
            first,
            domain_ref="commerce@1.0.0",
            enterprise_ref="olist@1.0.0",
            deployment_ref="olist-local@1.0.0",
        )
        write_bundle_manifest(
            bundle,
            second,
            domain_ref="commerce@1.0.0",
            enterprise_ref="olist@1.0.0",
            deployment_ref="olist-local@1.0.0",
        )
        first_bytes = first.read_bytes()
        self.assertEqual(first_bytes, second.read_bytes())
        self.assertNotIn(b"secret://", first_bytes)
        self.assertNotIn(b"DATABASE_URL", first_bytes)

    def test_tenant_access_policy_is_structural_and_admin_uses_roles(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle
        from data_agent.runtime.packs import DeploymentProfile, EnterpriseDataBinding

        binding = self.loader.load_enterprise_binding(self.pack_root)
        policy = binding.spec.policies
        self.assertEqual(policy.access_mode, "tenant_scoped")
        self.assertEqual(policy.tenant_scope.admin_bypass.principal_claim, "roles")
        self.assertIn("admin", policy.tenant_scope.admin_bypass.allowed_roles)

        raw = binding.model_dump(mode="python", by_alias=True)
        raw["metadata"]["name"] = "olist-copy"
        raw["spec"]["policies"]["tenantScope"]["ownershipPaths"].pop(
            "commerce.Payment"
        )
        clone = EnterpriseDataBinding.model_validate(raw)
        deployment_raw = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        ).model_dump(mode="python", by_alias=True)
        deployment_raw["spec"]["enterprisePack"] = "olist-copy@1.0.0"
        deployment = DeploymentProfile.model_validate(deployment_raw)
        lock = self.loader._load_yaml_mapping(self.pack_root / "pack.lock")
        lock["enterprisePack"] = "olist-copy@1.0.0"
        with self.assertRaisesRegex(ValueError, "ownership paths"):
            compile_runtime_bundle(
                self.loader.load_domain_pack(self.domain_root),
                clone,
                deployment,
                runtime_version="1.0.0",
                skill_versions={},
                tool_registry_version="1.0.0",
                schema_catalog=self.catalog_path,
                pack_lock=lock,
            )

    def test_unchanged_lock_rejects_unresolvable_tenant_principal_claim(self) -> None:
        raw = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        raw["spec"]["policies"]["tenantScope"]["principalClaim"] = (
            "organization_id"
        )

        with self.assertRaises(ValueError):
            self._compile_binding_document(raw)

    def test_unchanged_lock_rejects_seller_as_global_admin_role(self) -> None:
        raw = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        raw["spec"]["policies"]["tenantScope"]["adminBypass"][
            "allowedRoles"
        ] = ["seller"]

        with self.assertRaises(ValueError):
            self._compile_binding_document(raw)

    def test_unchanged_lock_rejects_order_id_as_tenant_scope_anchor(self) -> None:
        raw = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        tenant_scope = raw["spec"]["policies"]["tenantScope"]
        tenant_scope["canonicalField"] = "commerce.Order.order_id"
        tenant_scope["ownershipPaths"] = {
            "commerce.Order": [],
            "commerce.OrderItem": ["commerce.order_item_order"],
            "commerce.Customer": ["commerce.order_customer"],
            "commerce.Seller": [
                "commerce.order_item_seller",
                "commerce.order_item_order",
            ],
            "commerce.Product": [
                "commerce.order_item_product",
                "commerce.order_item_order",
            ],
            "commerce.Payment": ["commerce.payment_order"],
            "commerce.Review": ["commerce.review_order"],
            "commerce.GeoLocation": [
                "commerce.customer_geolocation",
                "commerce.order_customer",
            ],
            "commerce.CategoryTranslation": [
                "commerce.product_category_translation",
                "commerce.order_item_product",
                "commerce.order_item_order",
            ],
        }

        with self.assertRaises(ValueError):
            self._compile_binding_document(raw)

    def test_legacy_or_extended_pack_lock_fails_closed(self) -> None:
        from data_agent.runtime.composition import compile_runtime_bundle, load_bundle_manifest
        from data_agent.runtime.packs import DeploymentProfile

        domain = self.loader.load_domain_pack(self.domain_root)
        binding = self.loader.load_enterprise_binding(self.pack_root)
        deployment = self.loader.load_pack_yaml(
            self.deployment_path, DeploymentProfile
        )
        valid_lock = self.loader._load_yaml_mapping(self.pack_root / "pack.lock")
        legacy_lock = copy.deepcopy(valid_lock)
        legacy_lock.pop("policyDigest", None)
        extended_lock = {**valid_lock, "unexpected": True}
        manifest_path = PROJECT_ROOT / "generated" / "bundles" / "olist-local.json"

        for lock in (legacy_lock, extended_lock):
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                compile_runtime_bundle(
                    domain,
                    binding,
                    deployment,
                    runtime_version="1.0.0",
                    skill_versions={},
                    tool_registry_version="1.0.0",
                    schema_catalog=self.catalog_path,
                    pack_lock=lock,
                )
            with self.subTest(manifest_lock=lock), self.assertRaises(ValueError):
                load_bundle_manifest(
                    manifest_path,
                    pack_lock=lock,
                    schema_catalog=self.catalog_path,
                )

    def test_manifest_load_rejects_rehashed_policy_with_unchanged_lock(self) -> None:
        from data_agent.runtime.composition import load_bundle_manifest, stable_digest

        manifest_path = PROJECT_ROOT / "generated" / "bundles" / "olist-local.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        compiled_policy = document["bundle"]["compiled_access_policy"]
        compiled_policy["tenantScope"]["adminBypass"]["allowedRoles"] = [
            "seller"
        ]
        bundle_payload = {
            key: value
            for key, value in document["bundle"].items()
            if key != "digest"
        }
        document["bundle"]["digest"] = stable_digest(bundle_payload)
        document["bundleDigest"] = document["bundle"]["digest"]
        document["policyDigest"] = stable_digest(compiled_policy)
        temporary = PROJECT_ROOT / "generated" / "bundles" / ".policy-stale.json"
        temporary.write_text(json.dumps(document), encoding="utf-8")
        self.addCleanup(temporary.unlink)

        with self.assertRaisesRegex(ValueError, "policy"):
            load_bundle_manifest(
                temporary,
                pack_lock=self.pack_root / "pack.lock",
                schema_catalog=self.catalog_path,
            )

    def test_invalid_iana_timezone_is_rejected(self) -> None:
        from data_agent.runtime.packs import EnterpriseDataBinding

        raw = self.loader.load_enterprise_binding(self.pack_root).model_dump(
            mode="python", by_alias=True
        )
        raw["spec"]["bindings"]["commerce.Order"]["fields"]["purchased_at"][
            "timezone"
        ] = "Mars/Olympus_Mons"
        with self.assertRaisesRegex(ValidationError, "timezone"):
            EnterpriseDataBinding.model_validate(raw)

    def test_physical_grains_match_catalog_declared_unique_keys(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog_by_relation = {item["table"]: item for item in catalog}
        binding = self.loader.load_enterprise_binding(self.pack_root)
        for entity_id, entity_binding in binding.spec.bindings.items():
            relation = entity_binding.relation.split(".", 1)[1]
            unique_keys = {
                tuple(key) for key in catalog_by_relation[relation]["unique_keys"]
            }
            physical_grain = tuple(
                entity_binding.fields[field].column for field in entity_binding.grain
            )
            with self.subTest(entity=entity_id):
                self.assertIn(physical_grain, unique_keys)

    def test_published_manifest_load_verifies_bundle_lock_and_catalog(self) -> None:
        from data_agent.runtime.composition import load_bundle_manifest

        manifest_path = PROJECT_ROOT / "generated" / "bundles" / "olist-local.json"
        bundle = load_bundle_manifest(
            manifest_path,
            pack_lock=self.pack_root / "pack.lock",
            schema_catalog=self.catalog_path,
        )
        self.assertEqual(
            bundle.digest,
            json.loads(manifest_path.read_text(encoding="utf-8"))["bundleDigest"],
        )

        stale = json.loads(manifest_path.read_text(encoding="utf-8"))
        stale["bundle"]["schema_fingerprint"] = "0" * 64
        temporary = PROJECT_ROOT / "generated" / "bundles" / ".stale.json"
        temporary.write_text(json.dumps(stale), encoding="utf-8")
        self.addCleanup(temporary.unlink)
        with self.assertRaises(ValueError):
            load_bundle_manifest(
                temporary,
                pack_lock=self.pack_root / "pack.lock",
                schema_catalog=self.catalog_path,
            )

        stale_lock = self.loader._load_yaml_mapping(self.pack_root / "pack.lock")
        stale_lock["schemaFingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "lock"):
            load_bundle_manifest(
                manifest_path,
                pack_lock=stale_lock,
                schema_catalog=self.catalog_path,
            )

        drifted_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        drifted_catalog[0]["comment"] = "drift"
        with self.assertRaisesRegex(ValueError, "stale"):
            load_bundle_manifest(
                manifest_path,
                pack_lock=self.pack_root / "pack.lock",
                schema_catalog=drifted_catalog,
            )


if __name__ == "__main__":
    unittest.main()
