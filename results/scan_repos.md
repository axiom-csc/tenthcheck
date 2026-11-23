# Phase 7 pre-registration (written before any of these repos was scanned)

Date: 2026-11-22. Shallow clones (`--depth 1`) of the default branch; the commit below is what was scanned.
Tool: tenthcheck v1 at the commit that contains this file's parent. Defaults: min_support 5, min_consistency 0.80, tests/docs/examples excluded.

| # | Repo | Commit | Split |
|---|------|--------|-------|
| 1 | getredash/redash | f561a53dc55eeb3f20615be951560dc78bb23f4c | TUNE |
| 2 | zulip/zulip | 7bb11b60d91def4644f6c74be6611fcc85488b06 | TUNE |
| 3 | celery/celery | 29b14de6d95de5aa9032ca2c364e23b8979e8642 | TUNE |
| 4 | psf/requests | 3c7baaa05ab4aa49cc819ce3a819d260de19c157 | TUNE |
| 5 | saleor/saleor | e1ccf4d580025b06c2ff01441a1e798dc4635f24 | TUNE |
| 6 | apache/superset | 11f4e382764640d9bcd04b2e230755f3d8ac7fbc | HELD-OUT |
| 7 | mealie-recipes/mealie | 17e0ad3614578bec8f9663d6cae28bd0538678ea | HELD-OUT |
| 8 | netbox-community/netbox | 7d897955c5a3185bae6e33a8fa47f5fe2e2375fe | HELD-OUT |
| 9 | frappe/frappe | 58dd9ca7eb3f2321a56573172c9c745ad72a3ba0 | HELD-OUT |
| 10 | wagtail/wagtail | 9599540ea5648de7d5ecf09eb4b9794fb084b4b6 | HELD-OUT |

## Procedure (fixed in advance)
1. Scan the 5 TUNE repos, review every finding by reading the code, label each TRUE-INCONSISTENCY / INTENTIONAL / FALSE-POSITIVE.
2. Tune thresholds and filters ONLY from the TUNE reviews. Commit the tuned version, and record the changes here.
3. Only then scan the 5 HELD-OUT repos once with the frozen tuned settings, and review every finding.
4. Precision = (TRUE-INCONSISTENCY + INTENTIONAL) / all findings is reported alongside TRUE-INCONSISTENCY / all findings.
   "TRUE-INCONSISTENCY" means the code really differs from the pattern with no visible reason; it is not a claim of a bug.
5. If a finding looks like a genuine bug: write it up in results/, open nothing upstream.
