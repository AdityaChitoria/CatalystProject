from pathlib import Path

from backend.app.graph.graph_engine import TrustGraph
from backend.app.ingestion.parser import parse_vouch_file


def load_network(network_dir: str | Path) -> TrustGraph:
    graph = TrustGraph()

    network_path = Path(network_dir)

    if not network_path.is_dir():
        raise FileNotFoundError(
            f"Network directory not found: {network_path}"
        )

    project_dirs = sorted(
        path for path in network_path.iterdir() if path.is_dir()
    )

    # Step 1: Load all projects and their evidence
    for project_dir in project_dirs:
        vouch_file = project_dir / "VOUCHED.td"

        if vouch_file.exists():
            parsed_data = parse_vouch_file(vouch_file)
            graph.add_project(project_dir.name, parsed_data)
        else:
            graph.add_project(project_dir.name)

    # Step 2: Load trust relationships between projects
    for project_dir in project_dirs:
        trust_file = project_dir / "TRUSTED_PROJECTS.td"

        if not trust_file.exists():
            continue

        for line_number, line in enumerate(
            trust_file.read_text(encoding="utf-8-sig").splitlines(),
            start=1
        ):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = line.split()

            if len(parts) != 2:
                raise ValueError(
                    f"Invalid entry in {trust_file}, "
                    f"line {line_number}: {line}"
                )

            target_project, weight_text = parts

            try:
                weight = float(weight_text)
            except ValueError:
                raise ValueError(
                    f"Invalid trust weight in {trust_file}, "
                    f"line {line_number}"
                )

            graph.add_trust_edge(
                project_dir.name,
                target_project,
                weight
            )

    return graph 