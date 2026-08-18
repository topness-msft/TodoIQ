---
description: Resolve and backfill canonical Microsoft 365 user identities
argument-hint: status | backfill --dry-run|--apply [--resume] [--batch-size N] | candidates --task ID [--person-index N] | confirm --task ID --person-index N --aad GUID|--email ADDRESS
---

Manage canonical user identity for task recall. WorkIQ calls happen only in this
command. The Tornado server and Python identity services never call WorkIQ.

## Safety rules

- `status` and `backfill --dry-run` never write.
- `backfill --apply` writes only `person`, `person_alias`, `task_person`, and
  `person_backfill_state` through `src.services.person_backfill`.
- Never change task content, status, priority, timestamps, or suggestions.
- Exact AAD object ID and exact email/UPN are authoritative.
- Display-name lookup returns candidates only. Name candidates are never persisted
  until a later explicit `confirm` command supplies an exact AAD ID or email.
- Never guess an alias from a display name. Never store WorkIQ payloads, candidate
  lists, message bodies, job titles, or office locations.
- Do not run WorkIQ while a SQLite write transaction is open.

## `status`

Run:

```python
from src.db import get_connection, init_db
from src.services.person_backfill import backfill_status

conn = get_connection()
init_db(conn)
print(backfill_status(conn))
conn.close()
```

## `backfill --dry-run`

Call `plan_batch(conn, batch_size=N)`. For every lookup in the returned plan:

1. When `aad_object_id` is present, fetch the exact profile:
   `/users/{exact_aad_id}?$select=id,displayName,mail,userPrincipalName`
2. For email/UPN, use an exact collection filter because Graph addresses a user
   resource by object ID or UPN, not every valid mail alias:
   `/users?$filter=mail eq '{email}' or userPrincipalName eq '{email}'&$select=id,displayName,mail,userPrincipalName&$top=2`
   Zero or multiple results fail closed.
3. Record only `display_name`, `email`, `upn`, `aad_object_id`, `lookup_kind`,
   the exact `query_value`, `person_index`, and `role` in the in-memory proposal.
   Every planned lookup must have one explicit outcome before `--apply`: an exact
   profile or a deferred entry with `not_found`, `ambiguous`,
   `external_unresolved`, or `mcp_unavailable`.

Print task count, exact lookup count, deferred count, marker revision, and last
task ID. Do not call `apply_exact_batch`.

## `backfill --apply`

Build the same plan, exact profile proposal, and explicit deferred outcomes
without holding a DB transaction.
Then call:

```python
from src.services.person_backfill import apply_exact_batch
result = apply_exact_batch(conn, plan, profiles_by_task, deferred_by_task)
```

`--resume` uses the persisted marker. Continue bounded batches until the requested
batch completes. An empty planned batch is applied once to mark completion.
After an applied exact batch, call
`person_identity.seed_audited_aliases(conn, commit=True)`;
it adds recall-only aliases only for canonical emails that already resolved and
never creates a person from an alias.

## `candidates`

For one unresolved task person, search exact full display-name candidates:

`/users?$filter=displayName eq '{escaped_name}'&$select=id,displayName,mail,userPrincipalName&$top=10`

Show all candidates with their exact AAD ID and primary email. Do not persist any
result and do not select the first result automatically.

## `confirm`

Require exactly one `--aad` or `--email` argument. Resolve an AAD ID through:

`/users/{exact_aad_id}?$select=id,displayName,mail,userPrincipalName`

Resolve email through the same exact `mail OR userPrincipalName` filter used by
backfill. Zero or multiple results fail closed.

Read the task fingerprint from `plan_batch`, then call
`person_backfill.confirm_candidate(...)` with the exact profile, exact
`lookup_kind`, exact `query_value` used for the selected candidate's MCP request,
and `confirmed_alias` set to the stale historical address being replaced. A
stale task, mismatched response, or ambiguous/missing exact identifier fails
closed. The historical address is stored as a user-confirmed alias with
provenance; it never becomes the canonical primary email.
When confirming a row from `person_backfill_deferred`, pass its `deferred_id`;
call `person_backfill.resolve_deferred_identity(...)`. The service verifies the
task slot/fingerprint, display name, and exact historical identity, then marks
that row resolved. When the selected exact profile differs from the historical
email, pass that historical email as `confirmed_alias`; omission or mismatch
fails closed.
