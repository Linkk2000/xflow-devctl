"""Regression for #16: retain sealed draft history without allowing other dirt."""
from pathlib import Path
import runpy
import tempfile
import yaml

helpers = runpy.run_path(str(Path(__file__).with_name('task-state.py')))
approval = helpers['approval']
cli = helpers['cli_module']
write = helpers['write']


def fixture(root, name):
    repo, state, review, target, ctx, args = helpers['capability_branch_start_fixture'](
        root, name, '716', 'history')
    legacy = repo / '.xflow/current-task.md'
    write(legacy, '# XFlow Current Task\nIssue: draft\nState: S1_LOCAL_ISSUE_DRAFT\n\n## Allowed Actions\n- prepare\n\n## Forbidden Actions\n- implement\n')
    body = repo / '.xflow/issues/issue-draft/issue-draft.md'
    write(body, 'Reviewed issue body\n')
    draft_review = approval.prepare(repo, 'draft', 'issue-create', body)
    write(draft_review, draft_review.read_text().replace('Approved: no', 'Approved: yes'))
    grant = approval.require_remote(repo, 'issue-create', body, 'draft')
    reservation = approval.reserve_remote_action(repo, grant)
    confirmed = approval.confirm_remote_action(repo, reservation, target_issue='716',
        provider_receipt={'number': '716', 'html_url': 'https://example.test/issues/716'})
    history = approval.complete_remote_action(repo, confirmed)
    legacy.unlink()
    body.unlink()
    record = yaml.safe_load(history.read_text())
    return repo, target, ctx, args, history, record


def run():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        repo, target, ctx, args, history, record = fixture(root, 'valid')
        artifacts = [history] + [repo / record[k] for k in
            ('approvedReviewFile', 'approvedSnapshotFile', 'remoteClaimFile')]
        before = {p: p.read_bytes() for p in artifacts}
        assert cli.run_git_start(ctx, args) == 0
        assert helpers['check_task_binding'](repo, '716').branch == target
        assert all(p.read_bytes() == content for p, content in before.items())
        for case in ('extra', 'snapshot', 'review', 'claim', 'missing', 'missing-claim',
                     'other-worktree', 'other-repository', 'other-issue', 'symlink',
                     'unconfirmed', 'outside-path', 'provider-issue'):
            repo, target, ctx, args, history, record = fixture(root, case)
            if case == 'extra':
                write(repo / '.xflow/issues/issue-draft/unrelated.md', 'not this task')
            elif case in ('snapshot', 'review'):
                key = 'approvedSnapshotFile' if case == 'snapshot' else 'approvedReviewFile'
                p = repo / record[key]
                write(p, p.read_text() + '\ntampered')
            elif case == 'missing':
                (repo / record['approvedSnapshotFile']).unlink()
            elif case == 'missing-claim':
                (repo / record['remoteClaimFile']).unlink()
            elif case == 'symlink':
                p = repo / record['approvedSnapshotFile']
                external = root / 'outside-snapshot'
                external.write_bytes(p.read_bytes())
                p.unlink()
                p.symlink_to(external)
            else:
                p = repo / record['remoteClaimFile']
                claim = yaml.safe_load(p.read_text())
                if case == 'claim': claim['state'] = 'remote-confirmed'
                elif case == 'unconfirmed': claim['state'] = 'outcome-unknown'
                elif case == 'outside-path': claim['approvedSnapshotFile'] = '../outside-snapshot'
                elif case == 'provider-issue': claim['providerReceipt'] = '{"number":"999"}'
                else:
                    field = {'other-worktree': 'worktree', 'other-repository': 'repository', 'other-issue': 'targetIssue'}[case]
                    claim[field] = '999' if field == 'targetIssue' else 'a' * 64
                p.write_bytes(approval._remote_claim_bytes(claim))
            try:
                cli.run_git_start(ctx, args)
            except ValueError:
                pass
            else:
                raise AssertionError(f'{case}: unsafe branch creation accepted')
            assert helpers['resolve_bindings'](repo).branch == 'main'
        print('issue-create branch: success and 13 rejection cases passed')


if __name__ == '__main__':
    run()
