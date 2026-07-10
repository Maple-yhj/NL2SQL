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


if __name__ == "__main__":
    unittest.main()
