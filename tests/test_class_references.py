"""0.1.2 regression: a class used only in annotations and imports has dependents (rich's ConsoleOptions case).

In the 2026-11-17 with/without run (task t06), `check_blast_radius("rich.console.ConsoleOptions")` returned only
the class itself, although ~29 rich modules import it under TYPE_CHECKING and use it in quoted annotations.
"""
import json
from pathlib import Path

from tenthcheck.cli import main


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


def test_consoleoptions_has_annotation_and_import_dependents(tmp_path, capsys):
    repo = _write(tmp_path / "repo", {
        "rich/__init__.py": "",
        "rich/console.py": "class ConsoleOptions:\n    max_width: int = 80\n",
        "rich/padding.py": ("from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
                            "    from .console import ConsoleOptions\n\n"
                            "class Padding:\n    def __rich_console__(self, console, options: 'ConsoleOptions'):\n"
                            "        return []\n"),
        "rich/table.py": ("from .console import ConsoleOptions\n\n"
                          "def render(o):\n    return isinstance(o, ConsoleOptions)\n"),
    })
    assert main(["blast", str(repo), "rich.console.ConsoleOptions", "--max-depth", "1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["affected"] > 1  # 0.1.1: 1 (the class itself)
    assert set(out["by_depth"]["1"]) == {"rich.padding", "rich.padding.Padding.__rich_console__",
                                         "rich.table", "rich.table.render"}
