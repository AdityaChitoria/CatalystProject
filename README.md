# CatalystProject
### Decentralized Contributor Trust Network

CatalystProject is an evidence-based, decentralized contributor trust network designed to evaluate contributors using information shared across independent projects.

Instead of relying on a single global reputation score, each project evaluates contributors from its own perspective using trusted project relationships, positive evidence (vouches), negative evidence (denouncements), and coordinated campaign signals.

The system aims to make contributor trust more transparent, resistant to Sybil attacks, and less vulnerable to malicious or unsupported accusations.

---

## 1. Project Objectives

- Build a decentralized, project-specific trust network.
- Calculate transitive trust between projects.
- Reduce Sybil attacks and artificial reputation manipulation.
- Evaluate contributors using evidence rather than simple vote counts.
- Prevent unsupported denouncements from automatically blocking contributors.
- Preserve local project authority over contributor decisions.
- Detect potentially coordinated malicious contribution campaigns.
- Generate concise evidence-based explanations for trust decisions.

---

## 2. Technology Stack

| Technology | Purpose |
|---|---|
| Python | Core backend logic |
| Pytest | Automated testing |
| Git | Version control |
| VOUCHED.td | Contributor evidence format |
| TRUSTED_PROJECTS.td | Project trust relationships |
| OpenAI-compatible LLM API | Optional evidence brief generation |

---

## 3. Project Structure

```text
CatalystProject/
│
├── backend/
│   ├── app/
│   │   ├── campaign/
│   │   │   └── campaign_detector.py
│   │   │
│   │   ├── graph/
│   │   │   └── graph_engine.py
│   │   │
│   │   ├── ingestion/
│   │   │   ├── parser.py
│   │   │   └── network_loader.py
│   │   │
│   │   └── reporting/
│   │       └── brief_generator.py
│   │
│   └── tests/
│       ├── test_engine.py
│       ├── test_network_loader.py
│       ├── test_campaign_detector.py
│       ├── test_brief_generator.py
│       └── test_phase3.py
│
├── data/
│   └── fixtures/
│
├── docs/
│
├── mock_network/
│   ├── ghostty/
│   ├── fastapi/
│   ├── hostile_project/
│   └── my_project/
│
├── .gitignore
└── README.md
├── requirements.txt  
├── pytest.ini         
├── setup_venv.sh        
└── setup_venv.bat
```

---

## 4. Core Components

### A. Data Ingestion

**Files:** `parser.py`, `network_loader.py`

The ingestion module processes project evidence files and trust relationships.

Supported contributor evidence examples:

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
- Normalizes contributor usernames.
- Handles duplicate entries.
- Ignores comments.
- Reports malformed entries through warnings.
- Loads project network information for trust graph construction.

Main parser functions:

- `parse_vouch_text()`
- `parse_vouch_file()`
- `normalize_username()`

### B. Trust Graph Engine

**File:** `graph_engine.py`

The trust engine calculates project-to-project trust and evaluates contributors from an individual project's perspective.

**Transitive Trust and Hop Decay**

Trust propagates through connected projects with a damping factor.

Example:

```text
Project A --0.8--> Project B --0.7--> Project C
```

With a damping factor of 0.5:

```text
Trust(A, C) = 0.8 × 0.7 × 0.5

Trust(A, C) = 0.28
```

The engine supports bounded graph traversal and selects the strongest available path.

**Sybil Resistance**

Trust propagates outward from the querying project's trusted seed network.

Disconnected or untrusted project networks cannot create influence merely by generating artificial trust relationships among themselves.

**Denouncement Handling**

The engine distinguishes between isolated accusations and sufficiently supported negative evidence.

Current behaviour includes:

- Highly trusted denouncers can trigger blocking decisions.
- Weak isolated denouncements produce warnings.
- Multiple denouncements are preserved as evidence.
- Corroboration is handled conservatively when source independence is uncertain.
- Uncertainty alone is not treated as negative evidence.

**Local Project Authority**

Projects retain authority over their own contributor decisions.

A local vouch can take precedence over remote denouncements, while a direct local denouncement retains local blocking authority.

Main components:

- `TrustConfig`
- `Status`
- `Evidence`
- `Verdict`
- `TrustGraph`
- `project_trust()`
- `evaluate()`
- `evaluate_many()`

### C. Campaign Detection

**File:** `campaign_detector.py`

The campaign detector identifies potentially coordinated pull-request activity across repositories.

Features include:

- Text normalization.
- File-path normalization.
- Character-shingle fingerprinting.
- MinHash-based similarity estimation.
- Similarity comparison across pull requests.
- Time-window-based campaign detection.
- Cross-repository matching.
- Salted author pseudonymization.
- Reduced storage of raw pull-request content.

Campaign detection provides additional evidence for review. A similarity match alone does not prove malicious intent.

### D. Evidence Brief Generation

**File:** `brief_generator.py`

This module generates concise explanations of already-computed trust decisions.

Features:

- Deterministic mock evidence briefs.
- Optional OpenAI-compatible LLM integration.
- Neutral, evidence-based summaries.
- Validation against supplied evidence.
- Rejection of unsupported references.
- Deterministic fallback when LLM configuration is missing or generation fails.

**Important design principle:**

The LLM does not calculate trust, classify contributors, or decide whether a contributor should be blocked.

Trust decisions are computed by the deterministic backend. The LLM is used only as an optional summarization component.

### E. Integration Testing

**File:** `test_phase3.py`

The integration tests exercise interactions between the trust graph, campaign detector, contributor evidence, and evidence brief generator.

Scenarios include:

- Coordinated activity across multiple repositories.
- New contributor handling.
- Trusted contributor facing a weak accusation.
- Local project denouncement.
- Rejection of invented pull-request references.
- Rejection of unsupported repository references.

---

## 5. Contributor Statuses

The trust engine currently supports five statuses:

| Status | Meaning |
|---|---|
| TRUSTED | Sufficient positive trust evidence |
| LOW_TRUST | Positive evidence exists but is weak |
| WARNING | Negative evidence exists but does not meet blocking requirements |
| BLOCKED | Evidence meets the blocking requirements |
| UNKNOWN | Insufficient trusted evidence to make a decision |

These statuses are intended to distinguish established trust, insufficient evidence, and supported negative evidence.

---

## 6. Testing

The project uses Pytest for unit and integration testing.

Activate the virtual environment:

```powershell
.venv\Scripts\Activate.ps1
```

Run the complete test suite:

```powershell
python -m pytest backend/tests/ -v
```

Run individual test modules:

```powershell
python -m pytest backend/tests/test_engine.py -v
```

```powershell
python -m pytest backend/tests/test_network_loader.py -v
```

```powershell
python -m pytest backend/tests/test_campaign_detector.py -v
```

```powershell
python -m pytest backend/tests/test_brief_generator.py -v
```

Run the Phase 3 integration test:

```powershell
python -m backend.tests.test_phase3
```

---

## 7. Mock Network

The project includes a mock network for testing relationships between independent projects.

Example projects:

- ghostty
- fastapi
- hostile_project
- my_project

The mock network contains:

- Positive contributor evidence.
- Negative contributor evidence.
- Project-to-project trust relationships.
- Potentially malicious denouncements.
- Scenarios for testing local project authority.

The mock network helps test whether the trust engine can distinguish reliable evidence from weak or potentially malicious claims.

---

## 8. Development Setup

Clone the repository:

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

Install testing dependencies:

```powershell
python -m pip install pytest
```

Run the tests:

```powershell
python -m pytest backend/tests/ -v
```

### Optional Environment Configuration

Campaign detection can use a secret salt:

```text
CAMPAIGN_AUTHOR_SALT=your-secret-value
```

Optional LLM configuration:

```text
LLM_API_KEY=your-api-key
LLM_BASE_URL=your-api-base-url
LLM_MODEL=your-model-name
```

Keep secrets in a local `.env` file or environment variables. Never commit API keys or secrets to GitHub.

---

## 9. Current Development Progress

### Implemented

- Initial repository structure.
- Contributor evidence parser.
- Project network loader.
- Transitive trust graph.
- Hop decay and bounded graph traversal.
- Sybil resistance logic.
- Conservative denouncement handling.
- Local project authority.
- Contributor status classification.
- Campaign detection.
- Pull-request similarity fingerprinting.
- Evidence brief generation.
- Optional LLM integration with deterministic fallback.
- Unit and integration tests.

### Remaining Development

- FastAPI backend and API endpoints.
- GitHub API integration.
- Persistent database integration.
- Frontend dashboard.
- Trust graph visualization.
- Complete end-to-end integration.
- Additional security and adversarial testing.

---

## 10. Future Development

Planned improvements include:

- Automated GitHub repository ingestion.
- FastAPI-based backend services.
- PostgreSQL persistence.
- Interactive contributor reputation dashboard.
- Trust network visualization.
- Improved evidence independence and provenance.
- Additional Sybil and coordinated attack resistance.
- Expanded security and integration testing.

---

**CatalystProject — Evidence-based trust for decentralized open-source communities.**
