from __future__ import annotations

import importlib
import json
import shutil
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


class PackSchemaExportTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.packs = importlib.import_module("data_agent.runtime.packs")
            self.loader = importlib.import_module("data_agent.runtime.profile_loader")
        except ModuleNotFoundError as exc:
            self.fail(f"pack schema export or loader is missing: {exc}")

    def test_three_json_schemas_export_stably_and_match_repository_copies(self) -> None:
        expected_names = {
            "domain-pack.schema.json",
            "enterprise-binding.schema.json",
            "deployment-profile.schema.json",
        }
        tracked_dir = PROJECT_ROOT / "packs" / "schemas" / "v1"
        tracked_before = {
            name: (tracked_dir / name).read_bytes() for name in expected_names
        }
        output_dir = PROJECT_ROOT / "tests" / "contract" / ".schema-export-tmp"
        self.assertFalse(output_dir.exists(), "previous schema export temp remains")
        output_dir.mkdir()
        self.addCleanup(shutil.rmtree, output_dir)

        first_paths = self.loader.export_pack_schemas(output_dir)
        first_bytes = {path.name: path.read_bytes() for path in first_paths}
        second_paths = self.loader.export_pack_schemas(output_dir)
        second_bytes = {path.name: path.read_bytes() for path in second_paths}

        self.assertEqual(set(first_bytes), expected_names)
        self.assertEqual(first_bytes, second_bytes)
        for name, content in first_bytes.items():
            with self.subTest(schema=name):
                schema = json.loads(content)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(
                    content,
                    (tracked_dir / name).read_bytes(),
                )
        self.assertEqual(
            tracked_before,
            {name: (tracked_dir / name).read_bytes() for name in expected_names},
        )

    def test_yaml_loader_uses_safe_parsing_and_pydantic_validation(self) -> None:
        fixtures = PROJECT_ROOT / "tests" / "contract" / "fixtures"
        loaded = self.loader.load_pack_yaml(
            fixtures / "domain-minimal.yaml",
            self.packs.DomainPack,
        )
        self.assertEqual(loaded.metadata.name, "commerce")

        with self.assertRaises(self.loader.PackLoadError):
            self.loader.load_pack_yaml(
                fixtures / "unsafe-python-tag.yaml",
                self.packs.DomainPack,
            )

    def test_exported_schemas_enforce_pydantic_security_with_draft_2020_12(
        self,
    ) -> None:
        schema_dir = PROJECT_ROOT / "packs" / "schemas" / "v1"

        def validator(filename: str) -> Draft202012Validator:
            schema = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            return Draft202012Validator(schema)

        def assert_rejected(
            schema_validator: Draft202012Validator,
            document: dict,
        ) -> None:
            self.assertTrue(
                list(schema_validator.iter_errors(document)),
                f"exported schema accepted unsafe document: {document!r}",
            )

        domain_validator = validator("domain-pack.schema.json")
        domain = {
            "apiVersion": "dataagent.io/domain/v1",
            "kind": "DomainPack",
            "metadata": {"name": "commerce", "version": "1.0.0"},
            "spec": {
                "entities": {
                    "commerce.Order": {
                        "grain": ["order_id"],
                        "fields": {"order_id": {"type": "string"}},
                    }
                }
            },
        }
        self.assertFalse(list(domain_validator.iter_errors(domain)))
        unsafe_domain = deepcopy(domain)
        entity = unsafe_domain["spec"]["entities"].pop("commerce.Order")
        unsafe_domain["spec"]["entities"]["public.olist_orders_dataset"] = entity
        assert_rejected(domain_validator, unsafe_domain)

        enterprise_validator = validator("enterprise-binding.schema.json")
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
                "bindings": {},
                "policies": {},
            },
        }
        self.assertFalse(list(enterprise_validator.iter_errors(enterprise)))
        invalid_enterprises = []
        bad_source_key = deepcopy(enterprise)
        source = bad_source_key["spec"]["sources"].pop("sales")
        bad_source_key["spec"]["sources"]["Bad-Key"] = source
        invalid_enterprises.append(bad_source_key)
        mysql = deepcopy(enterprise)
        mysql["spec"]["sources"]["sales"]["connector"] = "mysql"
        invalid_enterprises.append(mysql)
        plaintext = deepcopy(enterprise)
        plaintext["spec"]["sources"]["sales"]["connectionRef"] = (
            "postgresql://user:password@localhost/olist"
        )
        invalid_enterprises.append(plaintext)
        writable = deepcopy(enterprise)
        writable["spec"]["sources"]["sales"]["readOnly"] = False
        invalid_enterprises.append(writable)
        for document in invalid_enterprises:
            with self.subTest(document=document):
                assert_rejected(enterprise_validator, document)

        deployment_validator = validator("deployment-profile.schema.json")
        deployment = {
            "apiVersion": "dataagent.io/deployment/v1",
            "kind": "DeploymentProfile",
            "metadata": {"name": "olist-local", "version": "1.0.0"},
            "spec": {
                "enterprisePack": "olist@1.0.0",
                "environment": "local",
                "secretsProvider": "environment",
                "datasourceSecrets": {
                    "secret://olist/local/database": "DATABASE_URL"
                },
                "runtime": {},
            },
        }
        self.assertFalse(list(deployment_validator.iter_errors(deployment)))
        plaintext_key = deepcopy(deployment)
        plaintext_key["spec"]["datasourceSecrets"] = {
            "postgresql://user:password@localhost/olist": "DATABASE_URL"
        }
        assert_rejected(deployment_validator, plaintext_key)


if __name__ == "__main__":
    unittest.main()
