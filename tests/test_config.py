from __future__ import annotations

from cfg.core.config import load_config


def test_load_config_reads_branch_settings(tmp_path) -> None:
    path = tmp_path / ".cfg.toml"
    path.write_text(
        """
[project]
name = "test"

[branches]
enabled = true
refs_collection = "cfgit_refs_custom"
default_branch = "main"

[[collection]]
name = "demo"
id_field = "id"

[env.dev]
database = "mongo"
uri = "mongodb://localhost:27017"
db = "cfgit-test"
""",
        encoding="utf-8",
    )

    project = load_config(path)

    assert project.branches.enabled is True
    assert project.branches.refs_collection == "cfgit_refs_custom"
    assert project.branches.default_branch == "main"
