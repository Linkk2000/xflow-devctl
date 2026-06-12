# Academic XFlow Checks

On the `academic` product branch, devctl validates local academic workflow
artifacts before human approval and remote writes.

```bash
./devctl check academic-issue --issue 1
./devctl check tdd-result --issue 1
./devctl check claude-package --issue 1
./devctl check academic-mr --issue 1
./devctl check local-review --issue 1 --file .xflow/issue-1/tdd-result.md
```

`local-review` compares `Approved SHA256` in
`.xflow/issue-<id>/approvals/local-review.md` with the current hash of the
reviewed file. If the reviewed file changes, the prior local approval becomes
invalid.

These checks implement the first local gate for Academic XFlow. They do not
replace remote MR/PR review or branch protection.

Remote write gates use machine-readable `Approved Action` values:

- `issue-create`
- `issue-comment`
- `issue-close`
- `git-mr`
- `remote-write`
