"""Sample client application demonstrating usage of requests and urllib3."""
import requests
import urllib3


def fetch_api_data(endpoint: str, timeout: int = 5) -> dict:
    """Fetch JSON data from a REST endpoint using requests."""
    url = f"https://api.example.com/{endpoint}"
    response = requests.get(url, timeout=timeout)
    return response.json()


def check_health(host: str = "example.com") -> int:
    """Check host health using urllib3 PoolManager."""
    http = urllib3.PoolManager()
    response = http.request("GET", f"https://{host}/health")
    return response.status
