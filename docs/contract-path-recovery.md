# Contract path recovery (#18)

New capability tasks must predeclare a path under configured `contracts.root`
before `git start`. The formal file may be materialized later; an Issue-local
candidate is not the task's canonical contract identity.

For an existing misbinding, retain the candidate and materialize its exact bytes
at the formal destination. Only pre-design S2/S3 capability tasks qualify.

```text
devctl task prepare-contract-relocation --issue <id> --to <formal-contract.yaml>
```

The human reviews the generated Issue-local `contract-relocation.json` and
approves the exact `task-contract-relocate` local review. This action is never
unattended and does not accept the design or authorize implementation.

```text
devctl task relocate-contract --issue <id> --file .xflow/issues/issue-<id>/contract-relocation.json
devctl task status
```

The request binds both paths, identical contract bytes, identity, configuration,
repository/worktree/branch and before-state digests. Request, review and before
images are retained in `approvals/history/contract-relocations/<digest>/` before
any correction. Original approvals and both contract files are preserved.

An interruption is recovered by retrying the same command. Each current state
file must exactly equal its sealed before or derived after image. Never delete
authority or re-prepare while recovering. Unrelated changes fail closed.
Commit trackable process evidence separately; acceptance and development gates
remain required.
