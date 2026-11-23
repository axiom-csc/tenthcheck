# Manual review: TUNE repos (used to tune; not a precision estimate)

## Before tuning (defaults as first written)
zulip: 48 exception sites in 24 patterns. All FALSE-POSITIVE: webhook test classes in `tests.py` (`*HookTests`, guard `self.check_webhook`)
reported as route handlers missing a guard; the "exceptions" are ignored-event tests and helper methods (`get_body`,
`verify_post_is_ignored`). Cause: `tests.py` was not recognised as a test file; `check_*` counted as a guard name.
redash, celery, requests, saleor: 0 findings.

## After tuning
| Repo | Finding | Label | Why |
|---|---|---|---|
| redash f561a53 | redash/handlers/destinations.py:81 `DestinationListResource.get` has no `@require_admin`; the other 5 get/post/put/delete/patch methods in the module do | INTENTIONAL (probable) | It returns `to_dict()` (without `all=True`), unlike the admin-only `DestinationResource.get` which returns full config; listing destinations is needed by non-admin users for alerts. I did not run redash to confirm. |
| zulip, celery, requests, saleor | none | n/a | |
