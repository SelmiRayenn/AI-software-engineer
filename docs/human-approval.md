# Human Patch Approval

Generated patches are never considered publishable automatically. A human reviewer must approve or
reject each `GeneratedPatch` before later export or pull request creation workflows can use it.

## API

```text
POST /patches/{patch_id}/approve
POST /patches/{patch_id}/reject
GET /patches/{patch_id}/review
```

Approval request:

```json
{
  "reviewer_name": "Ada",
  "review_notes": "Patch is focused and tests pass."
}
```

Rejection request:

```json
{
  "reviewer_name": "Grace",
  "review_notes": "Patch changes unrelated behavior."
}
```

`review_notes` are optional for approvals and required for rejections.

## Response

Review endpoints return the patch review state:

```json
{
  "generated_patch_id": "00000000-0000-0000-0000-000000000000",
  "review_status": "approved",
  "export_eligible": true,
  "review": {
    "id": "00000000-0000-0000-0000-000000000001",
    "generated_patch_id": "00000000-0000-0000-0000-000000000000",
    "decision": "approved",
    "reviewer_name": "Ada",
    "review_notes": "Patch is focused and tests pass.",
    "reviewed_at": "2026-01-01T00:00:00Z"
  }
}
```

Pending patches have `review_status: "pending"`, `export_eligible: false`, and `review: null`.

## Duplicate Reviews

Each generated patch can have one `HumanReview`. A second approve or reject request returns a
conflict unless the request explicitly includes:

```json
{
  "update_existing": true
}
```

Updating replaces the decision, reviewer name, notes, and review timestamp on the existing review
row.

## Agent Boundary

Approval endpoints are not part of the controlled agent tool system. Requests marked with
`X-Actor-Type: agent` are rejected, and no `approve`, `reject`, or `review` tool is exposed to the
agent orchestrator.

## Export Eligibility

Future export and pull request creation workflows must call the approval service before publishing:

- `approved` patches are eligible.
- `pending` patches are blocked.
- `rejected` patches are blocked.

The current code includes the service-level export guard but does not implement publishing or pull
request creation yet.
