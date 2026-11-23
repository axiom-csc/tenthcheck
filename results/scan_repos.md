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

## Tuning log (TUNE repos only; recorded before the held-out run)
First run (defaults as first written) on the 5 TUNE repos: zulip gave 48 exception sites in 24 patterns, all in Zulip's own
webhook test classes (files named `tests.py`, classes `*HookTests`, guard `self.check_webhook`). Every one of them is a
false positive for "route handler guard" (they are test helpers/ignored-event tests). Redash, celery, requests, saleor: 0.
Changes made because of that, nothing else:
1. Treat `tests.py`, `test.py`, `testing.py`, `*_tests.py` as test files; skip classes whose name contains "test" and methods
   named `test*`/`setUp`/`tearDown`.
2. Body-call guards must match the strict access-control name set (auth/login/permission/admin/require/role/csrf/scope/owner/
   access/can_/jwt/staff/current_user). Generic `check_*`, `validate_*`, `ensure_*`, `verify_*`, `allow*`, `token*` no longer count.
3. Added two groups so common frameworks are visible at all: methods named get/post/put/delete/patch in one module (Flask-RESTful /
   DRF style: redash) and top-level `def view(request, ...)` functions in one module (Django style: zulip).
Thresholds unchanged: min_support 5, min_consistency 0.80.
After tuning, TUNE repos: redash 1 exception site (redash/handlers/destinations.py:81, see results/review_tune.md), zulip 0, celery 0,
requests 0, saleor 0.

## Held-out set 2 (pre-registered before scanning; for the v1.1 post-hoc fixes)
The first held-out set (repos 6-10) was reviewed and its false positives were then used to make v1.1 (handlers must be
decorated, non-hook methods; `protect_read` counts as `protect`; `@before_request` etc. are not handlers). Those five repos are
therefore no longer held out for v1.1. These five fresh repos, not looked at before, are:

| # | Repo | Commit |
|---|------|--------|
| 11 | paperless-ngx/paperless-ngx | f722730db75495d0ee8d26d77c8730c95fff3219 |
| 12 | goauthentik/authentik | 7e5db84abecb04e48adca2166dec83236fc060f0 |
| 13 | pretix/pretix | 526a79c7d175a37421242a8167821373c357773d |
| 14 | apache/airflow | fecbb16aa07c5aa7d03a454dda9577545a1df9ab |
| 15 | frappe/erpnext | 88fa0a7c5fbadfaf7f558c231fbcd4a2dce6219c |

v1.1 settings frozen at the commit that contains this section. One run, every finding reviewed, no further tuning.
Set-1 re-run with v1.1 (tainted, for information only): superset 3 sites (download_xlsx, ensure_thumbnails_enabled [now excluded as a
hook], ensure_alert_reports_enabled [likewise]); all other set-1 repos 0 (redash still 1).
