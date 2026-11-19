from pathlib import Path
import pytest

from backend.app.ingestion.network_loader import load_network
from backend.app.graph.graph_engine import Status


# Locate mock_network from the project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
NETWORK_DIR = PROJECT_ROOT / "mock_network"


def test_network_loading():
    graph = load_network(NETWORK_DIR)

    trust = graph.project_trust({"my_project"})

    assert trust["my_project"] == 1.0
    assert trust["ghostty"] == 0.9
    assert trust["fastapi"] == 0.7
    assert trust["hostile_project"] == 0.2


def test_contributor_evaluations_from_mock_network():
    graph = load_network(NETWORK_DIR)

    assert graph.evaluate("my_project", "bob").status == Status.TRUSTED
    assert graph.evaluate("my_project", "spambot99").status == Status.BLOCKED
    assert graph.evaluate("my_project", "alice").status == Status.WARNING

def test_missing_network_directory():
    with pytest.raises(FileNotFoundError):
        load_network(NETWORK_DIR / "nonexistent")

def test_invalid_trust_weight(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "project_b 1.5",
        encoding="utf-8"
    )

    with pytest.raises(ValueError):
        load_network(tmp_path)

def test_malformed_trust_entry(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "ghostty",
        encoding="utf-8"
    )

    with pytest.raises(ValueError):
        load_network(tmp_path)        

def test_extra_text_after_vouch_username(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "VOUCHED.td").write_text(
        "alice extra",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    parsed_data = graph._files["project_a"]

    assert "github:alice" in parsed_data.vouched
    assert len(parsed_data.warnings) > 0        
def test_invalid_vouch_identity(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "VOUCHED.td").write_text(
        "github:",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    parsed_data = graph._files["project_a"]

    assert len(parsed_data.vouched) == 0
    assert len(parsed_data.denounced) == 0
    assert len(parsed_data.warnings) > 0    
def test_duplicate_contributor_last_entry_wins(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "VOUCHED.td").write_text(
        "alice\n-alice suspicious activity",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)
    parsed_data = graph._files["project_a"]

    assert "github:alice" not in parsed_data.vouched
    assert "github:alice" in parsed_data.denounced
    assert parsed_data.denounced["github:alice"] == "suspicious activity" 

def test_duplicate_contributor_vouch_overrides_denouncement(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "VOUCHED.td").write_text(
        "-alice suspicious activity\nalice",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)
    parsed_data = graph._files["project_a"]

    assert "github:alice" in parsed_data.vouched
    assert "github:alice" not in parsed_data.denounced       

def test_duplicate_trust_relationship_last_weight_wins(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "ghostty 0.8\nghostty 0.9",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    trust = graph.project_trust({"project_a"})

    assert trust["ghostty"] == 0.9    

def test_self_trust_relationship_is_ignored(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "project_a 0.9",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    trust = graph.project_trust({"project_a"})

    assert trust["project_a"] == 1.0
    assert len(graph._edges["project_a"]) == 0    

def test_unknown_project_reference_creates_graph_node(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "unknown_project 0.8",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    assert "unknown_project" in graph._edges
    assert "unknown_project" not in graph._files    

def test_unknown_project_reference_creates_graph_node(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        "unknown_project 0.8",
        encoding="utf-8"
    )

    graph = load_network(tmp_path)

    assert "unknown_project" in graph._edges
    assert "unknown_project" not in graph._files    

def test_missing_vouch_file(tmp_path):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    graph = load_network(tmp_path)

    assert "project_a" in graph._edges
    assert "project_a" not in graph._files

def test_empty_network_directory(tmp_path):
    graph = load_network(tmp_path)

    assert len(graph._edges) == 0
    assert len(graph._files) == 0        

@pytest.mark.parametrize("weight", ["nan", "inf", "-inf"])
def test_non_finite_trust_weights_rejected(tmp_path, weight):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        f"ghostty {weight}",
        encoding="utf-8"
    )

    with pytest.raises(ValueError):
        load_network(tmp_path)    

@pytest.mark.parametrize(
    "weight, should_pass",
    [
        ("0.0", False),
        ("0.01", True),
        ("1.0", True),
        ("1.01", False),
    ]
)
def test_trust_weight_boundaries(tmp_path, weight, should_pass):
    project_dir = tmp_path / "project_a"
    project_dir.mkdir()

    (project_dir / "TRUSTED_PROJECTS.td").write_text(
        f"ghostty {weight}",
        encoding="utf-8"
    )

    if should_pass:
        graph = load_network(tmp_path)
        assert graph._edges["project_a"]["ghostty"] == float(weight)
    else:
        with pytest.raises(ValueError):
            load_network(tmp_path)        