# Orchestration schema and lifecycle rules

`003_orchestration.sql` is applied after `001_init.sql` and `002_facebook.sql`.
It is the authority for state transitions: every lifecycle table has a database
trigger which rejects transitions absent from `orchestration_transition_allowed`.
Workers must create a new batch/run for a retry instead of reopening a terminal
record.

| Entity | Initial state | Allowed progression | Terminal states |
| --- | --- | --- | --- |
| Source | `draft` | `draft → active`; active may pause, require human verification, disable, or retire | `retired` |
| Browser profile | `provisioned` | provision, ready, in use, verification, quarantine | `retired` |
| Batch | `planned` | planned, queued, running, verification | succeeded, failed, cancelled |
| Batch run / acquisition run | `queued` | queued, running, human verification | succeeded, partial, failed, stopped, cancelled |
| Verification job | `requested` | requested, active | verified, rejected, expired, cancelled |
| Post/comment/profile | collected/discovered | normalise and analyse/relevance classification | expired/purged/rejected as applicable |
| Finding/delivery | `draft` / `queued` | ready, sending, delivered or retry delivery failure | dismissed, superseded, delivered/cancelled |

Safety invariants enforced by the migration:

- Batches hold at most 20 ordered items and only one item may be running or
  awaiting human verification at a time.
- A source has at most one active acquisition run. Browser profile platform
  must match the source/batch platform; `facebook_connector` is Facebook-only.
- An unresolved verification job is unique per source and job type.
- Post/comment ingestion and finding delivery use uniqueness keys for
  idempotency. The findings payload is JSONB, preserving original links and
  vertical-specific fields without losing structured delivery data.
- Sources and browser profiles use `deleted_at` soft deletion. Profile extracts
  have `expires_at` and terminal `purged` state for retention jobs.
- Audit rows are append-only. Set `app.actor` in the transaction with
  `set_config('app.actor', '<worker-or-user>', true)` to attribute changes.

The migration enables RLS with no permissive policies, matching the existing
service-role-only Supabase deployment.
