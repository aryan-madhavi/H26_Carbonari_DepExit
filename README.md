# DepExit 🚀

DepExit is a hackathon-built developer tool designed to safely and methodically guide Python projects away from risky, deprecated, unmaintained, or vulnerable dependencies.

It scans a target Python codebase, identifies risky packages, maps AST usages across source files, uses an AI model (Gemma) to recommenddrop-in replacements and step-by-step migration plans, verifies candidate packages against the project's test suite, and outputs a comprehensive **Dependency Exit Capsule** report.

## Project Structure

```
depexit/
├── main.py                 # CLI entrypoint and orchestrator
├── schema.py               # Shared data contracts (dataclasses & enums)
├── scanner/
│   ├── __init__.py
│   ├── discover.py         # Locates & parses dependency declarations
│   └── risk_assess.py      # Audits vulnerabilities (pip-audit/OSV) & maintenance
├── analyzer/
│   ├── __init__.py
│   ├── usage_mapper.py     # AST-based code usage tracking
│   └── verify_test.py      # Test suite runner & verification harness
├── ai/
│   ├── __init__.py
│   ├── gemma_client.py     # Gemma AI model client for migration plans
│   └── capsule_builder.py  # Assembles findings into Exit Capsules
├── report/
│   ├── __init__.py
│   └── render.py           # Jinja2 markdown and HTML report renderer
├── requirements.txt
├── README.md
├── LICENSE
├── .env.example
└── .gitignore
```

## Schema Contract

The shared data models defined in `schema.py` form the contract across all components:

- **`Dependency`**: `name`, `declared_version`, `source_file`
- **`RiskFlag`**: `dependency_name`, `risk_type` (`UNMAINTAINED` / `DEPRECATED` / `VULNERABLE` / `EOL` / `INCOMPATIBLE`), `reason`, `severity` (`LOW` / `MED` / `HIGH`)
- **`UsageLocation`**: `dependency_name`, `file_path`, `line_number`, `usage_type` (`IMPORT` / `CALL` / `CLASS_INSTANCE` / `ATTRIBUTE`), `code_snippet`
- **`AIRecommendation`**: `dependency_name`, `proposed_replacement`, `migration_steps`, `reasoning`, `ai_generated=True`
- **`VerificationResult`**: `dependency_name`, `test_name`, `status` (`PASS` / `FAIL` / `ERROR` / `NOT_RUN`), `raw_output`, `verified=True`
- **`ExitCapsule`**: `dependency`, `risk_flags`, `usage_locations`, `ai_recommendation`, `verification_results`, `compatibility_gaps`, `open_concerns`

## Setup & Installation

### Prerequisites

- Python 3.12+
- Virtual environment

### Installation

1. Create and activate a Python 3.12 virtual environment:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   ```bash
   cp .env.example .env
   # Set your GEMMA_API_KEY in .env
   ```

## Usage

```bash
python main.py /path/to/target-repo --output exit-capsule.md --format markdown
```

## License

MIT License. See [LICENSE](LICENSE) for details.
