"""Unit tests for client application."""
import unittest
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path

# Add demo_repo directory to sys.path so client can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import fetch_api_data, check_health


class TestClient(unittest.TestCase):
    """Test suite for client operations."""

    @patch("requests.get")
    def test_fetch_api_data(self, mock_get):
        """Test fetch_api_data successfully parses JSON response."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "count": 42}
        mock_get.return_value = mock_resp

        result = fetch_api_data("users")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 42)
        mock_get.assert_called_once()

    @patch("urllib3.PoolManager")
    def test_check_health(self, mock_pool_cls):
        """Test check_health queries host via urllib3."""
        mock_instance = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_instance.request.return_value = mock_resp
        mock_pool_cls.return_value = mock_instance

        status_code = check_health("example.com")
        self.assertEqual(status_code, 200)


if __name__ == "__main__":
    unittest.main()
