from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.datasource_service import DataSourceService
from data_agent.semantic_metrics import SemanticMetricFeatures
from tests.test_api_runtime_contract import (
    TEST_JWT_SECRET,
    _RecordingComposition,
    _auth_headers,
)


class ApiSemanticMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.composition = _RecordingComposition()
        self.environment = mock.patch.dict(
            "os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}
        )
        self.environment.start()
        app = create_app(
            runtime_factory=mock.AsyncMock(return_value=self.composition),
            data_source_service=DataSourceService(
                state_root=self.temporary_directory.name,
                semantic_metric_features=SemanticMetricFeatures(
                    provisional_overlays=True
                ),
            ),
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.analyst = _auth_headers(roles=["analyst"])
        self._seed_binding()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temporary_directory.cleanup()

    def _seed_binding(self) -> None:
        uploaded = self.client.post(
            "/api/data-sources/files",
            headers=self.analyst,
            data={"name": "OList", "source_id": "olist"},
            files={
                "files": (
                    "items.csv",
                    b"price\n10.5\n20.0\n",
                    "text/csv",
                )
            },
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        relation = self.client.get(
            "/api/data-sources/olist/catalog", headers=self.analyst
        ).json()["catalog"]["relations"][0]["relation"]
        binding = self.client.post(
            "/api/data-sources/olist/bindings",
            headers=self.analyst,
            json={
                "domain_id": "commerce",
                "mappings": [
                    {
                        "logical_ref": "item.price",
                        "physical_relation": relation,
                        "physical_column": "price",
                        "semantic_role": "measure",
                    }
                ],
            },
        )
        self.assertEqual(binding.status_code, 201, binding.text)
        activated = self.client.post(
            f"/api/data-sources/olist/bindings/{binding.json()['binding_id']}/activate",
            headers=_auth_headers(roles=["semantic_admin"]),
        )
        self.assertEqual(activated.status_code, 200, activated.text)

    @staticmethod
    def _proposal_body() -> dict:
        return {
            "domain_id": "commerce",
            "requested_term": "GMV",
            "candidates": [
                {
                    "candidate_id": "gmv-price",
                    "label": "Price GMV",
                    "rationale": "Uses item price",
                    "definition": {
                        "metric_ref": "commerce.gmv",
                        "display_name": "GMV",
                        "description": "Sum of item price",
                        "synonyms": ["成交总额"],
                        "formula": {
                            "kind": "aggregate",
                            "operation": "sum",
                            "operand": {
                                "kind": "field",
                                "ref": "item.price",
                            },
                        },
                        "currency": "BRL",
                    },
                }
            ],
        }

    def test_proposal_validation_overlay_and_activation_api(self) -> None:
        denied = self.client.post(
            "/api/data-sources/olist/metric-proposals",
            headers=_auth_headers(roles=["viewer"]),
            json=self._proposal_body(),
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        created = self.client.post(
            "/api/data-sources/olist/metric-proposals",
            headers=self.analyst,
            json=self._proposal_body(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        proposal = created.json()
        selected = self.client.post(
            f"/api/metric-proposals/{proposal['proposal_id']}/select",
            headers=self.analyst,
            json={"candidate_id": "gmv-price", "expected_revision": 1},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        validated = self.client.post(
            f"/api/metric-proposals/{proposal['proposal_id']}/validate",
            headers=_auth_headers(roles=["semantic_editor"]),
            json={"expected_revision": selected.json()["revision"]},
        )
        self.assertEqual(validated.status_code, 200, validated.text)
        validation = validated.json()
        self.assertEqual(validation["proposal"]["status"], "pending_approval")

        overlay = self.client.post(
            f"/api/metric-proposals/{proposal['proposal_id']}/overlays",
            headers=self.analyst,
            json={
                "validation_report_id": validation["report"]["report_id"],
                "scope": "conversation",
                "conversation_id": "conversation-1",
            },
        )
        self.assertEqual(overlay.status_code, 201, overlay.text)
        self.assertEqual(overlay.json()["definition"]["metric_ref"], "commerce.gmv")

        activated = self.client.post(
            f"/api/metric-proposals/{proposal['proposal_id']}/approve-and-activate",
            headers=_auth_headers(roles=["semantic_admin"]),
            json={
                "validation_report_id": validation["report"]["report_id"],
                "expected_revision": validation["proposal"]["revision"],
                "expected_pointer_revision": 0,
            },
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        body = activated.json()
        self.assertEqual(body["proposal"]["status"], "approved")
        self.assertEqual(body["metric_set"]["status"], "published")
        self.assertEqual(body["active_pointer"]["metric_set_version"], 1)

        listed = self.client.get(
            "/api/data-sources/olist/metric-proposals", headers=self.analyst
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["items"]), 1)

    def test_commerce_pack_discovers_gmv_candidate_without_activation(self) -> None:
        discovered = self.client.post(
            "/api/data-sources/olist/metric-proposals/discover",
            headers=self.analyst,
            json={"domain_id": "commerce", "requested_term": "成交总额"},
        )

        self.assertEqual(discovered.status_code, 201, discovered.text)
        proposal = discovered.json()
        self.assertEqual(proposal["status"], "draft")
        self.assertEqual(proposal["domain_pack"]["pack_id"], "domain.commerce")
        self.assertEqual(
            [item["candidate_id"] for item in proposal["candidates"]],
            ["gmv-item-price"],
        )
        self.assertIn("退款处理", proposal["candidates"][0]["required_decisions"])
