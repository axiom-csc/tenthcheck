# Manual review: HELD-OUT repos, frozen tuned version (one run, results not used for tuning)

Scanned with defaults (min_support 5, min_consistency 0.80) at the commits in results/scan_repos.md. Each site was read in the source.

| # | Repo | Site | Label | Reason |
|---|------|------|-------|--------|
| 1 | superset | superset/dashboards/api.py:573 `pre_get_list` | FALSE-POSITIVE | Flask-AppBuilder hook override, no `@expose`: not an endpoint |
| 2 | superset | superset/dashboards/api.py:2248 `get_method_permission` | FALSE-POSITIVE | permission-mapping helper, not an endpoint |
| 3 | superset | superset/dashboards/api.py:2261 `download_xlsx` | INTENTIONAL | real endpoint with no `@protect`; code comment at :2248 says it is deliberately login-free (job id is the credential) |
| 4 | superset | superset/datasets/api.py:565 `get_list_headless` | FALSE-POSITIVE | hook override, not an endpoint |
| 5 | superset | superset/datasets/api.py:2467 `render_dataset_fields` | FALSE-POSITIVE | `@staticmethod` helper |
| 6 | superset | superset/charts/api.py:239 `ensure_thumbnails_enabled` | FALSE-POSITIVE | `@before_request` helper |
| 7 | superset | superset/charts/api.py:508 `pre_get_list` | FALSE-POSITIVE | hook override |
| 8 | superset | superset/charts/api.py:580 `get_list_headless` | FALSE-POSITIVE | helper, called by the guarded endpoint above it |
| 9 | superset | superset/themes/api.py:193 `pre_get` | FALSE-POSITIVE | hook override |
| 10 | superset | superset/themes/api.py:200 `pre_get_list` | FALSE-POSITIVE | hook override |
| 11 | superset | superset/semantic_layers/api.py:1374 `connections` | FALSE-POSITIVE | endpoint guarded by `@protect_read(...)`, a sibling of `@protect()`; the miner treated the names as different guards |
| 12 | superset | superset/datasource/api.py:1122 `combined_list` | FALSE-POSITIVE | endpoint guarded by `@protect_read(...)`, same reason |
| 13 | wagtail | wagtail/rich_text/feature_registry.py:108 `function_as_entity_handler` | FALSE-POSITIVE | `@staticmethod` helper in a class whose methods call `_scan_for_features` (not a guard) |

mealie, netbox, frappe: 0 findings. Wagtail: 2 more patterns mined with 0 exceptions.

## Result (held-out, one run)
- Findings (exception sites): 13
- TRUE-INCONSISTENCY (unexplained, looks like a real gap): **0**
- INTENTIONAL (real difference, deliberate and documented): **1**
- FALSE-POSITIVE: **12**
- Precision, counting INTENTIONAL as correct detections: 1/13 = **8%**. Counting only unexplained true inconsistencies: **0/13**.
- Recall was not measured. Three of five repos produced nothing.

## Causes of the false positives (not fixed in this run; held-out stays frozen)
1. Class-group members include methods that are not endpoints at all (no route decorator): 10 of 12.
2. Guard names from one family (`protect`, `protect_read`) counted as different guards: 2 of 12.
3. `@staticmethod` helper: 1 (counted in 1).
