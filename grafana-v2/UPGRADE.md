# Upgrade requirements: preserve the Terminal capture-lock remedy

## Version record

| Item | Recorded version |
| --- | --- |
| Remedy ID and Git tag | `capture-lock-fix-2026-11-09` |
| First fixed source commit | `580f448f380956cf1d1e47c0cabbdc7ff4f9ddd1` |
| Incident baseline observed before repair | `e615dec5265431abada04658c6f1fee1c28f5769` |
| Verified deployed image | `sha256:1957854e2052d1cdb9fee4a078ad4df47e17d9ddebc6368bd7fc5ce52df7b950` |
| Verification date | 2026-11-09 |

This remedy belongs to **MCP Observatory v2's capture gateway and inspector**,
which sit between the Ubuntu Terminal OAuth gateway and Terminal MCP server.
The terminal server's own version string does not identify whether this remedy
is installed. Check the Observatory source and running container image instead.
The named tag identifies the original repair, not every future compatible build.

Incident symptoms: Terminal MCP itself responds on localhost `:8900`, while
the capture proxy on `:18900` stalls and ChatGPT times out. Capture logs show
`sqlite3.OperationalError: database is locked`; the inspector may be unresponsive.
An unchanged capture file could leave an offset-update transaction open, and
synchronous storage waits blocked the proxy event loop. Historical capture
linking also repeatedly scanned requests without a matching command index.

## Requirements for every replacement version

Preserve these behaviors when merging, rebasing, replacing dependencies, building
a new image, or upgrading the terminal integration:

- `app/capture.py`: commit ingestion offsets even when zero new records arrive;
  a completed ingestion pass must not retain a writer transaction.
- `app/main.py`: roll back collector transactions on failure, and create
  `idx_requests_mcp_command` on `(mcp, cmd_request_id, delivered_at)`.
- `app/gateway.py`: bound capture database lock waits to 50 ms, rather than the
  previous 30 seconds. Keep the wait short if the storage implementation changes.
- `app/capture.py`: storage failures must preserve successful upstream responses
  and release failed transactions. Telemetry persistence must not take down MCP.
- `app/capture.py`: reuse unmatched request candidates during a linking pass;
  invalidate the cache after linking so later captures see current candidates.

For a normal Git upgrade, confirm the repair is in the candidate's history:

```bash
git merge-base --is-ancestor 580f448f380956cf1d1e47c0cabbdc7ff4f9ddd1 HEAD
```

A successful ancestry check is only a starting point: later changes can revert
the remedy. A failed check can also mean the fix was cherry-picked or rewritten;
review the behavior and run the tests rather than applying the old patch blindly.

## Test the new candidate, not an old installed image

From this repository's `grafana-v2` directory, set `candidate_image` to the
**newly built candidate's immutable image ID**, then run the current tests against
the application code inside that image:

```bash
candidate_image=sha256:REPLACE_WITH_NEW_CANDIDATE_IMAGE_ID
docker run --rm --pull never --network host \
  -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python "$candidate_image" \
  -m unittest discover -s tests -p test_terminal_monitor_overlay.py -v
```

The tests use disposable databases and loopback servers. Do not mount the host's
`app` directory over `/app/app`: that could test working-tree code instead of the
candidate image. Do not use the historical image ID in the version table as proof
that a new release works.

These regression tests must remain meaningful and pass, or have equivalent
coverage if the implementation is replaced:

- `test_locked_capture_database_preserves_proxy_responsiveness`
- `test_unchanged_capture_file_releases_writer_lock`
- `test_unmatched_backlog_reuses_candidate_query`

The original deployment passed ten tests in this test module. Future releases
may have more tests; the historical count is not a substitute for current results.

For inventory changes, also run `test_mcp_inventory.py` against the candidate
image, mounting current `tests`, `scripts` and `prometheus` directories read-only.
Run `scripts/generate_prometheus_config.py --check` and the current
`tests/inventory_ui.cjs` browser check against an isolated inspector. Keep stable
MCP keys for renamed OAuth versions; list retired versions under their active
replacement and never turn old listeners back on to satisfy a dashboard check.

## Deploy, verify, and record the new version

1. Retain the current verified **fixed** gateway and inspector images for rollback.
   Back up persistent data using a SQLite-consistent backup before schema changes.
   Never commit database contents, tokens, bootstrap credentials or raw logs.
2. Recreate only gateway and inspector when deploying an Observatory-only fix.
   Preserve original terminal PTYs, OAuth configuration and tunnel processes.
3. Compare the running image IDs with the tested candidate, confirm the lookup
   index exists, and verify inspector health. Check live initialization and tool
   discovery through the existing local OAuth startup-discovery mechanism;
   anonymous access must remain HTTP 401.
4. Through the live local proxy, open a disposable terminal, run a harmless marker
   command with exit code zero, retrieve its monitor resource, and close it.
   Check ChatGPT execution separately before claiming remote verification.
5. Record the new source commit, immutable image IDs, date, test results and
   rollback image in the Ubuntu architecture registry's
   `registry/services/mcp-observatory-v2.yaml` and changelog. Preserve this remedy
   record as historical provenance; do not call the old image the latest release.

The pre-repair rollback images named `rollback-before-lock-fix-20261109` restore
the old failure mode. They are emergency historical artifacts, not the preferred
rollback target for future upgrades. Use a verified post-fix release instead.

The original repair evidence is recorded in architecture registry commit
`70123deb2d676bcc6daeb579ff1cc4448274259e`.
