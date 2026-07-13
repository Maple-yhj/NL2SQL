import unittest

from fastapi.testclient import TestClient

from api.app import create_app


class ApiHealthTests(unittest.TestCase):
    def test_health_returns_service_status(self):
        client = TestClient(create_app())

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "service": "data-agent-api"})


if __name__ == "__main__":
    unittest.main()
