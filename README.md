# CatalystProject
### Decentralized Contributor Trust Network

CatalystProject is a decentralized contributor trust network designed to evaluate contributors using evidence shared by different projects.

Instead of relying on a single global reputation score, each project calculates contributor trust from its own network of trusted projects.

The system considers positive evidence (vouches), negative evidence (denouncements), project trust relationships, and corroboration before making decisions.

---

## 1. Project Objectives

The main objectives of CatalystProject are:

- Build a decentralized, project-specific trust network.
- Calculate transitive trust between projects.
- Reduce Sybil attacks involving fake repositories and artificial reputation.
- Prevent malicious or unsupported denouncements from automatically blocking contributors.
- Evaluate contributors using evidence rather than simple vote counts.
- Maintain local project authority over contributor decisions.

## 2. Current Technology

| Technology | Purpose |
|---|---|
| Python | Core trust engine and data processing |
| Pytest | Automated testing |
| Git | Version control |
| VOUCHED.td | Contributor evidence format |
| TRUSTED_PROJECTS.td | Project-to-project trust relationships |

## 3. Project Structure

```text
CatalystProject/
│
├── backend/
│   ├── app/
│   │   ├── graph/
│   │   │   └── graph_engine.py
│   │   │
│   │   └── ingestion/
│   │       └── parser.py
│   │
│   └── tests/
│       └── test_engine.py
│
├── data/
│   └── fixtures/
│       └── sample.VOUCHED.td
│
├── docs/
│
├── mock_network/
│   ├── ghostty/
│   │   ├── VOUCHED.td
│   │   └── TRUSTED_PROJECTS.td
│   │
│   ├── fastapi/
│   │   ├── VOUCHED.td
│   │   └── TRUSTED_PROJECTS.td
│   │
│   ├── hostile_project/
│   │   ├── VOUCHED.td
│   │   └── TRUSTED_PROJECTS.td
│   │
│   └── my_project/
│       ├── VOUCHED.td
│       └── TRUSTED_PROJECTS.td
│
├── .gitignore
└── README.md
```

## 4. Core Components

### A. Data Ingestion — `parser.py`

The parser processes VOUCHED.td files containing contributor vouches and denouncements.

Supported examples:

```text
# Positive evidence
alice
bob
github:mitchellh

# Negative evidence
-spambot99 AI slop PRs
```

Features:
- Parses positive and negative evidence.
- Supports platform-specific identities.
- Normalizes usernames.
- Handles duplicate entries.
- Ignores comments and malformed entries with warnings.

Main functions:

- `parse_vouch_text()`
- `parse_vouch_file()`
- `normalize_username()`

### B. Trust Graph Engine — `graph_engine.py`

The trust engine calculates trust relationships between projects and evaluates contributors from an individual project's perspective.

**Transitive Trust and Hop Decay**

Trust propagates through connected projects with a damping factor.

For example:

```text
Project A --0.8--> Project B --0.7--> Project C
```

With a damping factor of 0.5:

```text
Trust(A, C) = 0.8 × 0.7 × 0.5

Trust(A, C) = 0.28
```

The current implementation limits propagation to a maximum of two hops and selects the strongest available path.

**Sybil Resistance**

Trust is calculated starting from the querying project's trusted seed network.

Projects without a valid trust path receive zero influence, preventing isolated fake project networks from creating artificial reputation.

**Denouncement Corroboration**

The engine distinguishes between isolated denouncements and sufficiently supported negative evidence.

Current default rules include:

- A highly trusted denouncer can trigger a block.
- Multiple sufficiently trusted, distinct denouncers can trigger a block through corroboration.
- An isolated low-trust denouncement produces a warning rather than an automatic block.

**Local Project Authority**

A project's own direct vouch can override remote denouncements, unless the project itself has denounced the contributor.

Main components:

- `TrustConfig`
- `Status`
- `Evidence`
- `Verdict`
- `TrustGraph`
- `project_trust()`
- `evaluate()`
- `evaluate_many()`

## 5. Current Contributor Statuses

The engine currently supports five statuses:

| Status | Meaning |
|---|---|
| TRUSTED | Sufficient positive evidence |
| LOW_TRUST | Positive evidence exists but is weak |
| WARNING | Negative evidence exists but does not meet blocking requirements |
| BLOCKED | Denouncement meets blocking requirements |
| UNKNOWN | No trusted project has an opinion |

These statuses represent the current implementation and may be refined as the project develops.

## 6. Testing

Automated tests are implemented using Pytest.

The current test suite checks:

1. Transitive trust calculations.
2. Hop decay.
3. Sybil network isolation.
4. Contributor reputation evaluation.

Run the tests from the repository root:

```powershell
python -m pytest backend/tests/test_engine.py -v
```

**Current test result:**

```text
2 passed in 0.06s
```

Both existing automated tests passed successfully.

## 7. Mock Network

A mock network has been created to simulate relationships between independent projects.

Current projects:

| Project | Trust from my_project |
|---|---:|
| ghostty | 0.9 |
| fastapi | 0.7 |
| hostile_project | 0.2 |
| my_project | Local project |

The mock network contains:

- Ghostty vouches for Alice and Bob.
- Ghostty denounces spambot99.
- Hostile Project maliciously denounces Alice.
- My Project defines trust relationships with the other projects.

The mock network is intended to test whether strong positive evidence can withstand an isolated malicious denouncement from a low-trust project.

**Implementation status:** The mock files have been created, but automatic loading of these directories into the TrustGraph is still pending.

## 8. Development Setup

Clone the repository and enter its directory:

```powershell
git clone <repository-url>
cd CatalystProject
```

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

Install testing dependency:

```powershell
python -m pip install pytest
```

Run the tests:

```powershell
python -m pytest backend/tests/test_engine.py -v
```

## 9. Current Development Progress

### Completed

- Initial repository structure.
- Python virtual environment setup.
- VOUCHED.td parser.
- Contributor evidence representation.
- Transitive trust graph engine.
- Hop decay and bounded graph traversal.
- Sybil isolation logic.
- Denouncement corroboration.
- Contributor evaluation and status classification.
- Initial automated test suite.
- Mock network directory and sample data.

### In Progress / Upcoming

- Automatic loading of mock project directories.
- Parsing TRUSTED_PROJECTS.td files.
- Integrating mock network data with TrustGraph.
- Testing Alice, Bob, and spambot99 using actual mock files.
- Expanding adversarial test scenarios.
- Refining evidence independence and reputation classification.

## 10. Future Development

Planned improvements include:

- GitHub API integration.
- Automated repository data ingestion.
- Backend API development using FastAPI.
- Persistent storage using PostgreSQL.
- Improved Sybil and coordinated attack detection.
- Frontend dashboard and trust graph visualization.
- Evidence provenance and contributor transparency.

---

**CatalystProject — Evidence-based trust for decentralized open-source communities.**