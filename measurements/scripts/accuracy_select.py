"""Pick held-out symbols by call frequency and dump grep candidates. Never computes or prints Tenthcheck edges."""
import ast, json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, sys.argv[1])  # products/tenthcheck/scripts
from accuracy_candidates import candidates, enclosing_defs
from tenthcheck.parser import iter_py_files, module_name_for
pkg = Path(sys.argv[2]).resolve(); prefix = "starlette"
defs = defaultdict(list)
for path in iter_py_files(pkg):
    rel = path.relative_to(pkg).as_posix()
    module, _ = module_name_for(rel, pkg.name)
    module = f"{prefix}.{module}" if module != pkg.name else prefix
    for _, _, q in enclosing_defs(ast.parse(path.read_text()), module):
        name = q.rsplit(".", 1)[-1]
        if not (name.startswith("__") and name.endswith("__")):
            defs[name].append(q)
counts = {n: len(candidates(pkg, prefix, n)) for n in defs}
pool = sorted([n for n, c in counts.items() if 3 <= c <= 12], key=lambda n: (-counts[n], n))[:14]
out = {n: {"definitions": defs[n], "hits": counts[n], "candidates": candidates(pkg, prefix, n)} for n in pool}
json.dump(out, open(sys.argv[3], "w"), indent=1)
for n in pool: print(n, counts[n], defs[n])
