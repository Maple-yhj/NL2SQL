from __future__ import annotations

import importlib
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError


SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC_ROOT))


def _domain_document() -> dict:
    return {
        "apiVersion": "dataagent.io/domain/v1",
        "kind": "DomainPack",
        "metadata": {"name": "commerce", "version": "1.0.0"},
        "spec": {},
    }


def _domain_semantic_document() -> dict:
    document = _domain_document()
    document["spec"] = {
        "entities": {
            "commerce.Order": {
                "grain": ["order_id"],
                "fields": {
                    "order_id": {"type": "string", "nullable": False},
                },
            }
        },
        "relationships": [],
        "metrics": {
            "commerce.order_count": {
                "aggregation": "count_distinct",
                "inputs": ["commerce.Order.order_id"],
            }
        },
        "vocabulary": [
            {
                "term": "订单数",
                "refs": ["commerce.order_count", "commerce.Order"],
            }
        ],
        "evals": [
            {
                "id": "commerce.order_count_basic",
                "question": "How many orders?",
                "expectedMetrics": ["commerce.order_count"],
                "expectedEntities": ["commerce.Order"],
            }
        ],
    }
    return document


def _enterprise_document() -> dict:
    return {
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
            "bindings": {},
            "policies": {},
        },
    }


def _deployment_document() -> dict:
    return {
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
            "memoryDatabaseRef": "MEMORY_DATABASE_URL",
            "runtime": {
                "maxToolCalls": 24,
                "maxCorrectionRounds": 2,
                "maxDurationSeconds": 120,
            },
        },
    }


class PackModelContractTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.packs = importlib.import_module("data_agent.runtime.packs")
        except ModuleNotFoundError as exc:
            self.fail(f"pack contracts are missing: {exc}")

    def test_three_pack_types_accept_only_declared_fields(self) -> None:
        domain = self.packs.DomainPack.model_validate(_domain_document())
        enterprise = self.packs.EnterpriseDataBinding.model_validate(
            _enterprise_document()
        )
        deployment = self.packs.DeploymentProfile.model_validate(
            _deployment_document()
        )

        self.assertEqual(domain.metadata.name, "commerce")
        self.assertTrue(enterprise.spec.sources["sales"].read_only)
        self.assertEqual(deployment.spec.runtime.max_tool_calls, 24)
        self.assertEqual(
            domain.model_dump(mode="json", by_alias=True)["apiVersion"],
            "dataagent.io/domain/v1",
        )

        for model, document in (
            (self.packs.DomainPack, _domain_document()),
            (self.packs.EnterpriseDataBinding, _enterprise_document()),
            (self.packs.DeploymentProfile, _deployment_document()),
        ):
            invalid = deepcopy(document)
            invalid["undeclared"] = True
            with self.subTest(model=model.__name__), self.assertRaises(
                ValidationError
            ):
                model.model_validate(invalid)

    def test_enterprise_sources_require_secret_refs_and_read_only_access(self) -> None:
        plaintext = _enterprise_document()
        plaintext["spec"]["sources"]["sales"]["connectionRef"] = (
            "postgresql://user:password@localhost/olist"
        )
        with self.assertRaises(ValidationError):
            self.packs.EnterpriseDataBinding.model_validate(plaintext)

        writable = _enterprise_document()
        writable["spec"]["sources"]["sales"]["readOnly"] = False
        with self.assertRaises(ValidationError):
            self.packs.EnterpriseDataBinding.model_validate(writable)

    def test_enterprise_bindings_reject_unsafe_relations_and_columns(self) -> None:
        unsafe_relations = (
            "public.orders; DROP TABLE users",
            "public.orders JOIN public.users",
            "public..orders",
            "public.orders -- comment",
        )
        for relation in unsafe_relations:
            document = _enterprise_document()
            document["spec"]["bindings"] = {
                "commerce.Order": {
                    "source": "sales",
                    "relation": relation,
                    "grain": ["order_id"],
                    "fields": {"order_id": {"column": "order_id"}},
                }
            }
            with self.subTest(relation=relation), self.assertRaises(ValidationError):
                self.packs.EnterpriseDataBinding.model_validate(document)

        unsafe_columns = (
            "order_id; DELETE FROM orders",
            "price + freight_value",
            "orders.order_id",
            "order_id)/*",
        )
        for column in unsafe_columns:
            document = _enterprise_document()
            document["spec"]["bindings"] = {
                "commerce.Order": {
                    "source": "sales",
                    "relation": "public.olist_orders_dataset",
                    "grain": ["order_id"],
                    "fields": {"order_id": {"column": column}},
                }
            }
            with self.subTest(column=column), self.assertRaises(ValidationError):
                self.packs.EnterpriseDataBinding.model_validate(document)

    def test_domain_and_all_config_reject_physical_or_executable_content(self) -> None:
        physical_domain = _domain_document()
        physical_domain["spec"]["relation"] = "public.olist_orders_dataset"
        with self.assertRaises(ValidationError):
            self.packs.DomainPack.model_validate(physical_domain)

        arbitrary_sql = _enterprise_document()
        arbitrary_sql["spec"]["bindings"] = {
            "commerce.Order": {
                "source": "sales",
                "relation": "public.olist_orders_dataset",
                "grain": ["order_id"],
                "fields": {},
                "sql": "select * from public.olist_orders_dataset",
            }
        }
        with self.assertRaises(ValidationError):
            self.packs.EnterpriseDataBinding.model_validate(arbitrary_sql)

        jinja = _deployment_document()
        jinja["spec"]["environment"] = "{{ lookup('env') }}"
        with self.assertRaises(ValidationError):
            self.packs.DeploymentProfile.model_validate(jinja)

    def test_domain_pack_rejects_physical_identifiers_in_logical_positions(self) -> None:
        invalid_documents = []

        physical_entity = _domain_semantic_document()
        entity = physical_entity["spec"]["entities"].pop("commerce.Order")
        physical_entity["spec"]["entities"]["public.olist_orders_dataset"] = entity
        invalid_documents.append(physical_entity)

        physical_grain = _domain_semantic_document()
        physical_grain["spec"]["entities"]["commerce.Order"]["grain"] = [
            "public.olist_orders_dataset.order_id"
        ]
        invalid_documents.append(physical_grain)

        physical_metric_input = _domain_semantic_document()
        physical_metric_input["spec"]["metrics"]["commerce.order_count"][
            "inputs"
        ] = ["public.olist_orders_dataset.order_id"]
        invalid_documents.append(physical_metric_input)

        physical_vocabulary_ref = _domain_semantic_document()
        physical_vocabulary_ref["spec"]["vocabulary"][0]["refs"] = [
            "public.olist_orders_dataset"
        ]
        invalid_documents.append(physical_vocabulary_ref)

        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                self.packs.DomainPack.model_validate(document)

    def test_domain_pack_requires_all_logical_references_to_resolve(self) -> None:
        invalid_documents = []

        missing_grain = _domain_semantic_document()
        missing_grain["spec"]["entities"]["commerce.Order"]["grain"] = [
            "missing_field"
        ]
        invalid_documents.append(missing_grain)

        missing_relationship = _domain_semantic_document()
        missing_relationship["spec"]["relationships"] = [
            {
                "name": "commerce.order_customer",
                "fromEntity": "commerce.Order",
                "toEntity": "commerce.Customer",
                "cardinality": "many_to_one",
                "fromFields": ["order_id"],
                "toFields": ["customer_id"],
            }
        ]
        invalid_documents.append(missing_relationship)

        missing_metric_input = _domain_semantic_document()
        missing_metric_input["spec"]["metrics"]["commerce.order_count"][
            "inputs"
        ] = ["commerce.Order.missing_field"]
        invalid_documents.append(missing_metric_input)

        missing_vocabulary_ref = _domain_semantic_document()
        missing_vocabulary_ref["spec"]["vocabulary"][0]["refs"] = [
            "commerce.missing_metric"
        ]
        invalid_documents.append(missing_vocabulary_ref)

        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                self.packs.DomainPack.model_validate(document)

    def test_pack_metadata_uses_consistent_pack_name_and_semver_validation(self) -> None:
        invalid_name = _domain_document()
        invalid_name["metadata"]["name"] = "Commerce.Production"
        with self.assertRaises(ValidationError):
            self.packs.DomainPack.model_validate(invalid_name)

        invalid_version = _domain_document()
        invalid_version["metadata"]["version"] = "latest"
        with self.assertRaises(ValidationError):
            self.packs.DomainPack.model_validate(invalid_version)

        for version in ("1.0.0-01", "1.0.0-alpha.01"):
            invalid_prerelease = _domain_document()
            invalid_prerelease["metadata"]["version"] = version
            with self.subTest(version=version), self.assertRaises(ValidationError):
                self.packs.DomainPack.model_validate(invalid_prerelease)

        invalid_ref = _enterprise_document()
        invalid_ref["spec"]["domains"] = [{"ref": "commerce@1.0.0-01"}]
        with self.assertRaises(ValidationError):
            self.packs.EnterpriseDataBinding.model_validate(invalid_ref)


if __name__ == "__main__":
    unittest.main()
