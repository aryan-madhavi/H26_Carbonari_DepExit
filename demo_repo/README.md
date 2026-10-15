# DepExit Demo Target Repository

This is a sample target repository created to demonstrate the automated DepExit pipeline:
- `requirements.txt`: Declares older versions of `requests` and `urllib3` with known CVEs.
- `client.py`: Source code using `requests` and `urllib3`.
- `tests/test_client.py`: Python `unittest` test suite.

Run DepExit against this repository:
```bash
python main.py demo_repo
```
