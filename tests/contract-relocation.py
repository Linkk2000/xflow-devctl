"""#18: correction changes location, never identity, meaning or permission."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import runpy
import tempfile
import json
import yaml
from concurrent.futures import ThreadPoolExecutor

h = runpy.run_path(str(Path(__file__).with_name('task-state.py')))
cli, approval, write = h['cli_module'], h['approval'], h['write']


def fixture(root, name):
    repo, state_path, review, target, ctx, args = h['capability_branch_start_fixture'](root, name, '718', 'relocate')
    # Model an already-activated task from the old runtime, not a new bypass.
    h['git'](repo, 'checkout', '-b', target)
    old = '.xflow/issues/issue-718/capability-contract.candidate.yaml'
    s = replace(h['state']('718', target), contract_file=old)
    write(state_path, h['render_task_state'](s))
    h['activate_task'](repo, '718')
    raw = yaml.safe_load((Path(__file__).parent / 'fixtures/contracts/valid.yaml').read_text())
    raw['status'] = 'draft'
    body = yaml.safe_dump(raw, allow_unicode=True)
    write(repo / old, body)
    new = repo / 'docs/requirements/example/contract.yaml'
    write(new, body)
    review.unlink()  # Test fixture's unused branch review is not this decision.
    return repo, ctx, old, new


def prepare(repo, ctx, new):
    with patch.object(cli, 'context', return_value=ctx):
        cli.run_task(SimpleNamespace(task_command='prepare-contract-relocation', issue='718', to=new))
    return repo / '.xflow/issues/issue-718/contract-relocation.json'


def execute(ctx, request):
    with patch.object(cli, 'context', return_value=ctx):
        return cli.run_task(SimpleNamespace(task_command='relocate-contract', issue='718', file=request))


def approve(repo):
    p = repo / '.xflow/issues/issue-718/approvals/local-review.md'
    write(p, p.read_text().replace('Approved: no', 'Approved: yes'))


def run():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        repo, ctx, old, new = fixture(root, 'ok')
        request = prepare(repo, ctx, new)
        before = (repo / old).read_bytes()
        try:
            execute(ctx, request)
        except ValueError:
            pass
        else:
            raise AssertionError('unapproved correction succeeded')
        approve(repo)
        assert execute(ctx, request) == 0
        assert h['check_task_binding'](repo, '718').contract_file == 'docs/requirements/example/contract.yaml'
        assert (repo / old).read_bytes() == new.read_bytes() == before
        # Same completed request is idempotent, not another mutation/approval.
        assert execute(ctx, request) == 0
        for case in ('changed-content', 'changed-request', 'late-state', 'outside', 'symlink'):
            repo, ctx, old, new = fixture(root, case)
            request = prepare(repo, ctx, new)
            approve(repo)
            if case == 'changed-content':
                write(new, new.read_text() + '\n# changed after review\n')
            elif case == 'changed-request':
                write(request, request.read_text() + '\n')
            elif case == 'late-state':
                s = replace(h['state']('718', ctx.repo_root.name), branch=h['resolve_bindings'](repo).branch,
                            contract_file=old, execution_state='S4_TDD_AND_IMPLEMENTATION')
                write(repo / '.xflow/issues/issue-718/task-state.md', h['render_task_state'](s))
            elif case == 'outside':
                data = json.loads(request.read_text())
                data['newFile'] = '../outside.yaml'
                write(request, json.dumps(data))
            else:
                outside = root / 'outside.yaml'
                outside.write_bytes(new.read_bytes())
                new.unlink()
                new.symlink_to(outside)
            try:
                execute(ctx, request)
            except ValueError:
                pass
            else:
                raise AssertionError(f'{case}: invalid relocation accepted')
        from xflow import contract_relocation as relocation
        for stop in (1, 2, 3):
            repo, ctx, old, new = fixture(root, f'interrupt-{stop}')
            request = prepare(repo, ctx, new)
            approve(repo)
            original_write = relocation.task_state._write_atomic
            calls = []
            def interrupted(path, content):
                original_write(path, content)
                calls.append(path)
                if len(calls) == stop:
                    raise RuntimeError('simulated process interruption')
            with patch.object(relocation.task_state, '_write_atomic', side_effect=interrupted):
                try:
                    execute(ctx, request)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('interruption was not exercised')
            if stop < 3:
                try:
                    h['check_task_binding'](repo, '718')
                except ValueError:
                    pass
                else:
                    raise AssertionError('partial relocation must fail task checks closed')
            assert execute(ctx, request) == 0
            assert h['check_task_binding'](repo, '718').contract_file == 'docs/requirements/example/contract.yaml'
        repo, ctx, old, new = fixture(root, 'retire-interruption')
        request = prepare(repo, ctx, new)
        approve(repo)
        with patch.object(approval, 'retire_live_review', side_effect=RuntimeError('retirement interrupted')):
            try:
                execute(ctx, request)
            except RuntimeError:
                pass
            else:
                raise AssertionError('retirement interruption not exercised')
        assert approval.default_approval_file(repo, '718').exists()
        assert execute(ctx, request) == 0
        assert not approval.default_approval_file(repo, '718').exists()
        repo, ctx, old, new = fixture(root, 'concurrent')
        request = prepare(repo, ctx, new)
        approve(repo)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: relocation.relocate(repo, '718', request), range(2)))
        assert results[0] == results[1]
        history = [r for r in approval._history_records(repo) if r['action'] == 'task-contract-relocate']
        assert len(history) == 1
        # A new task cannot first seal an Issue-local candidate path anymore.
        repo, state_path, review, target, ctx, args = h['capability_branch_start_fixture'](root, 'prevent', '719', 'prevent')
        s = replace(h['state']('719', target), contract_file='.xflow/issues/issue-719/candidate.yaml')
        write(state_path, h['render_task_state'](s))
        try:
            cli.run_git_start(ctx, args)
        except ValueError as exc:
            assert 'contracts.root' in str(exc), str(exc)
        else:
            raise AssertionError('new task sealed an invalid contract location')
        print('contract relocation: success, replay, rejection, three interruption points, concurrency and prevention passed')


if __name__ == '__main__':
    run()
