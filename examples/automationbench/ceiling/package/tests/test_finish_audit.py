import os
import importlib.util


def _load():
    p = os.path.join(os.path.dirname(__file__), '..', 'middlewares', 'finish_audit.py')
    spec = importlib.util.spec_from_file_location('finish_audit', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_blocks_first_finish(ctx):
    m = _load()
    r = m.finish_audit(ctx, {'final_message': 'All done.', 'turn': 5})
    assert r['action'] == 'feedback'
    assert m.MARK in r['message']


def test_allows_after_audit(ctx):
    m = _load()
    r = m.finish_audit(ctx, {'final_message': 'Summary...\nFINISH-AUDIT: done', 'turn': 9})
    assert r['action'] == 'continue'
