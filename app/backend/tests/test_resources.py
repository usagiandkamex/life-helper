from __future__ import annotations

from pathlib import Path

import yaml

RESOURCES = Path(__file__).resolve().parents[1] / "src" / "life_helper" / "resources"


def test_skills_have_valid_frontmatter():
    skills = sorted((RESOURCES / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"memory-keeper", "furusato-nozei", "life-plan", "investment"}
    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), path
        meta = yaml.safe_load(text.split("---", 2)[1])
        assert meta["name"] == path.parent.name
        assert len(meta["description"]) > 20


def test_seed_and_params_present():
    assert (RESOURCES / "seed" / "INDEX.md").exists()
    assert (RESOURCES / "seed" / "profile" / "about-me.md").exists()
    for year in ("2025", "2026"):
        params = yaml.safe_load((RESOURCES / "tax_params" / f"{year}.yaml").read_text(encoding="utf-8"))
        assert params["year"] == int(year) and params["sources"] and params["capital_gains_tax_rate"] == 0.20315
        assert "nisa" not in params
    for broker in ("sbi", "rakuten"):
        mapping = yaml.safe_load((RESOURCES / "broker_csv" / f"{broker}.yaml").read_text(encoding="utf-8"))
        assert set(mapping["required"]) <= set(mapping["columns"])
