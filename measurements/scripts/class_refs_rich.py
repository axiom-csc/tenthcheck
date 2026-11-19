"""Do classes used without being called have dependents? rich 15.0.0, six widely used classes (0.1.2 check).

    python measurements/scripts/class_refs_rich.py <rich checkout at 6ac483c> [--out results/class_refs_rich.json]

For each class: hop-1 dependents from `dependents_radius`, the rich modules containing at least one, and the rich
modules whose source contains the class name as a word (`grep -rlw`), excluding the defining module. A grep hit
with no dependent is either a miss or a mention in a docstring/comment; those were read by hand (see README).
Not a labelled accuracy set: it counts modules, not call sites.
"""
import json
import os
import subprocess
import sys

from tenthcheck.parser import parse_package
from tenthcheck.topology import dependents_radius

CLASSES = ["rich.console.ConsoleOptions", "rich.console.ConsoleDimensions", "rich.segment.Segment",
           "rich.style.Style", "rich.text.Text", "rich.measure.Measurement"]


def main():
    co = os.path.abspath(sys.argv[1])
    head = subprocess.run(["git", "-C", co, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    g = parse_package(co, skip_syntax_errors=True)
    rows = []
    for sym in CLASSES:
        home = sym.rsplit(".", 1)[0]
        dep = dependents_radius(g, sym, max_depth=1)["by_depth"].get(1, [])
        mods = {".".join(x.split(".")[:2]) for x in dep if x.startswith("rich.")} - {home}
        out = subprocess.run(["grep", "-rlw", sym.rsplit(".", 1)[1], os.path.join(co, "rich")],
                             capture_output=True, text=True).stdout.split()
        grep = {os.path.relpath(p, co)[:-3].replace("/", ".") for p in out if p.endswith(".py")} - {home}
        rows.append({"class": sym, "direct_dependents": len(dep), "modules_with_dependent": len(mods),
                     "modules_naming_it": len(grep), "grep_only": sorted(grep - mods)})
        print(f"{sym}: {len(dep)} direct dependents, in {len(mods)} of {len(grep)} modules naming it; "
              f"grep-only {sorted(grep - mods)}")
    if "--out" in sys.argv:
        with open(sys.argv[sys.argv.index("--out") + 1], "w") as f:
            json.dump({"rich_commit": head, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    main()
