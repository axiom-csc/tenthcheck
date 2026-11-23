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

---

# Held-out set 2 (fresh repos 11-15, v1.1, pre-registered in results/scan_repos.md; one run, no tuning afterwards)

Per repo: paperless-ngx 0 findings, authentik 0, pretix 0 (3 patterns mined, no exceptions), airflow 4 sites in 2 patterns, erpnext 1.
Totals: 4 sites in 2 patterns with exceptions (airflow 2 patterns, erpnext 1 = 3 patterns; 4 sites).

| # | Repo | Site | Label | Reason |
|---|------|------|-------|--------|
| 14 | airflow fecbb16 | airflow-core/.../routes/public/connections.py:166 `get_connection_test` | FALSE-POSITIVE | the 8 other handlers use `Depends(requires_access_connection(...))`; this one has no such dependency but authorizes in the body with `get_auth_manager().is_authorized_connection(...)` (lines ~178-185). Guard present, different form |
| 15 | airflow | same file:441 `enqueue_connection_test` | FALSE-POSITIVE | same: in-body `is_authorized_connection` check at ~line 463 |
| 16 | airflow | providers/google/.../vertex_ai/feature_store.py:327 `fetch_feature_values` | FALSE-POSITIVE | the "guard" is `self.get_feature_online_store_admin_service_client` (a client factory whose name contains `admin`), not an access check |
| 17 | erpnext 88fa0a7 | erpnext/manufacturing/doctype/job_card/job_card.py:288 `get_material_shortages` | INTENTIONAL | `@frappe.whitelist()` method without `frappe.has_permission(... "write" ...)` like 6 siblings, but it is read-only (computes shortages) and frappe/handler.py `run_doc_method` enforces read permission before calling it |

Result: 4 findings, 0 TRUE-INCONSISTENCY, 1 INTENTIONAL, 3 FALSE-POSITIVE. Precision counting INTENTIONAL as a correct detection: 1/4 = 25%; unexplained true inconsistencies: 0/4.

## Combined view (honest summary)
| Set | Version | Findings | True (unexplained) | Intentional | False positive |
|---|---|---|---|---|---|
| held-out 1 (superset, mealie, netbox, frappe, wagtail) | v1.0 frozen | 13 | 0 | 1 | 12 |
| held-out 2 (paperless-ngx, authentik, pretix, airflow, erpnext) | v1.1 | 4 | 0 | 1 | 3 |

No findings at all on: mealie, netbox, frappe, wagtail (v1.1), paperless-ngx, authentik, pretix, celery, requests, saleor, zulip.
**No genuine bug was found in any repo, so nothing is written up for reporting.**
Mined-pattern quality is not what is measured here; only the exceptions were graded.

## What this says
- The miners find few patterns (most scans: 0-3 exceptions), so the false-positive count is small in absolute terms but the
  fraction is high. At 25% (v1.1, 4 findings) the sample is far too small to claim a rate.
- The route-guard miner is the only one that fired on these repos. call-pair, guard-mutation and sql-scope produced exception
  sites on **zero** of the 15 repos at default thresholds; they are tested only on synthetic fixtures (tests/test_mining.py).
- Remaining failure modes seen: equivalent guard in a different form (body check vs decorator/dependency), and a "guard" name
  that is not an access check (`*admin*` client factory).
