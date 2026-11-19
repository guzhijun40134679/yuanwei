"""37/38 work order: synthetic image turns through the real adapter and frozen Hermes pure functions."""
import ast
import os
from pathlib import Path

import pytest

from hermes_image_contract import SOURCES
from test_hermes_adapter import mk, stored, turn
from yuanwei import hermes_compat as HC

HERMES_SRC = Path(os.path.expanduser("~/.hermes/hermes-agent"))   # 导入时取（测试隔离改 HOME 之前），和别的测试同一个写法


def host_functions(sources=SOURCES):
    ns = {}
    exec('from __future__ import annotations\nfrom collections.abc import Mapping\n' + '\n'.join(sources.values()), ns)
    return ns


def content(text='', count=1):
    return ([{'type': 'text', 'text': text}] if text else []) + [
        {'type': 'image_url', 'image_url': {'url': '/synthetic/cache/image%d.png' % i}} for i in range(count)]


@pytest.mark.parametrize('text,count', [('', 1), ('图上是测试文字', 1), ('两张图测试', 2), ('  原字\n保留  ', 2)])
def test_image_turn_records_original_text_and_answer(tmp_path, text, count):
    mem, a = mk(tmp_path)
    f = host_functions()
    image = content(text, count)
    turn(a, 1, f['_memory_query_text'](image))
    a.sync_turn(f['_summarize_user_message_for_log'](image, sep='\n'), '这是截图测试回答',
                session_id='hs1', messages=[{'role': 'user', 'content': image}])
    rows = stored(mem)
    expected = (text + '\n' if text else '') + '〔带 %d 张图〕' % count
    assert rows == [('hs1', 'user', expected), ('hs1', 'assistant', '这是截图测试回答')]
    assert a.recorded_turns == 1
    assert '/synthetic/cache' not in str(rows)
    a.shutdown()


def test_literal_image_marker_cannot_match_a_different_pending_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, '测试文字')
    a.sync_turn('[1 image] 测试文字', '不能存', session_id='hs1',
                messages=[{'role': 'user', 'content': '[1 image] 测试文字'}])
    assert stored(mem) == []
    assert a.status()['skipped']['no_pending_turn'] == 1
    a.shutdown()


def test_literal_image_marker_is_preserved_as_plain_text(tmp_path):
    mem, a = mk(tmp_path)
    text = '[2 images] 字面不是图片'
    turn(a, 1, text)
    a.sync_turn(text, '普通回答', session_id='hs1', messages=[{'role': 'user', 'content': text}])
    assert stored(mem)[0][2] == text
    a.shutdown()


@pytest.mark.parametrize('value,reason', [([], 'unsupported_user_content'), ({}, 'unsupported_user_content'), ('', 'empty_user_content')])
def test_invalid_sync_content_has_visible_skip_reason(tmp_path, value, reason):
    mem, a = mk(tmp_path)
    a.sync_turn(value, '不能存', session_id='hs1', messages=[])
    assert a.status()['skipped'].get(reason) == 1
    a.shutdown()


def test_actual_installed_hermes_pure_functions_match_frozen_contract():
    root = HERMES_SRC
    if not root.is_dir():
        pytest.skip('没核：本机没有获准的 Hermes 安装源码')
    actual = {}
    for rel, source in SOURCES.items():
        wanted = ast.parse(source).body
        names = {n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in wanted}
        nodes = [n for n in ast.parse((root / rel).read_text()).body
                 if (n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) else '') in names]
        actual[rel] = ast.unparse(ast.Module(body=nodes, type_ignores=[]))
        assert ast.dump(ast.parse(actual[rel])) == ast.dump(ast.parse(source))
    f = host_functions(actual)
    assert [(f['_memory_query_text'](content(t, n)), f['_summarize_user_message_for_log'](content(t, n), sep='\n'))
            for t, n in [('', 1), ('图上是测试文字', 1), ('两张图测试', 2)]] == [
                ('', '[1 image]'), ('图上是测试文字', '[1 image] 图上是测试文字'), ('两张图测试', '[2 images] 两张图测试')]


def test_image_turn_ambiguous_pending_is_not_guessed(tmp_path):
    mem, a = mk(tmp_path)
    image = content('相同文字')
    turn(a, 1, '相同文字')
    turn(a, 2, '相同文字')
    a.sync_turn('[1 image] 相同文字', '不能存', session_id='hs1', messages=[{'role': 'user', 'content': image}])
    assert stored(mem) == []
    assert a.status()['skipped'].get('ambiguous_pending_turn') == 1
    a.shutdown()


@pytest.mark.parametrize('messages,reason', [(None, 'no_messages'), ([], 'current_user_row_not_found'),
    ([{'role': 'user', 'content': content('测试'), '_todo_snapshot_synthetic': True}], 'machinery_row')])
def test_image_turn_rejections_have_status_reason(tmp_path, messages, reason):
    mem, a = mk(tmp_path)
    turn(a, 1, '测试')
    a.sync_turn('[1 image] 测试', '不能存', session_id='hs1', messages=messages)
    assert stored(mem) == []
    assert a.status()['skipped'].get(reason) == 1
    a.shutdown()


def test_doctor_checks_both_image_text_contracts():
    result = HC.doctor_from_reader(SOURCES.get)
    checks = {c['id']: c for c in result['checks']}
    assert checks['memory_query_text']['status'] == 'ok'
    assert checks['memory_sync_text']['status'] == 'ok'


@pytest.mark.parametrize('rel,old,new,dep', [
    ('agent/message_content.py', 'sep: str = "\\n"', 'sep: str = " "', 'memory_query_text'),
    ('agent/turn_context.py', 'return flatten_message_text(original_user_message)', 'return "changed"', 'memory_query_text'),
    ('agent/codex_responses_adapter.py', 'image_count = len(parts) - len(text_bits)', 'image_count = 0', 'memory_sync_text'),
])
def test_doctor_reports_changed_image_contract(rel, old, new, dep):
    sources = dict(SOURCES)
    assert old in sources[rel]
    sources[rel] = sources[rel].replace(old, new)
    checks = {c['id']: c for c in HC.doctor_from_reader(sources.get)['checks']}
    assert checks[dep]['status'] == 'changed'


def test_doctor_missing_image_contract_is_explicitly_unchecked():
    checks = {c['id']: c for c in HC.doctor_from_reader(lambda _: None)['checks']}
    for dep in ('memory_query_text', 'memory_sync_text'):
        assert checks[dep]['status'] == 'missing'
        assert '没核' in checks[dep]['detail']


@pytest.mark.parametrize('literal', ['[1 image] 测试文字', ['[1 image] 测试文字'], [{'type': 'text', 'text': '[1 image] 测试文字'}]])
def test_literal_marker_cannot_reuse_an_older_image_row(tmp_path, literal):
    mem, a = mk(tmp_path)
    turn(a, 1, '测试文字')
    rows = [{'role': 'user', 'content': content('测试文字')},
            {'role': 'assistant', 'content': '旧图片回答'},
            {'role': 'user', 'content': literal}]
    a.sync_turn('[1 image] 测试文字', '不能配成旧图片', session_id='hs1', messages=rows)
    assert stored(mem) == []
    assert a.status()['skipped'].get('no_pending_turn') == 1
    a.shutdown()


def test_image_with_literal_marker_in_its_text_keeps_that_text(tmp_path):
    mem, a = mk(tmp_path)
    text = '[1 image] 是我要讨论的字面标记'
    image = content(text)
    turn(a, 1, text)
    a.sync_turn('[1 image] ' + text, '回答', session_id='hs1', messages=[{'role': 'user', 'content': image}])
    assert stored(mem)[0][2] == text + '\n〔带 1 张图〕'
    a.shutdown()


def native_content(tmp_path, text, count=1):
    """Call Hermes's actual builder with synthetic local paths; pixel conversion is replaced only to avoid image I/O."""
    path = HERMES_SRC / 'agent' / 'image_routing.py'
    if not path.is_file():
        pytest.skip('没核：没有获准的 Hermes 图片构造源码')
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'build_native_content_parts')
    ns = {'Path': Path, '_file_to_data_url': lambda _: 'data:image/png;base64,c3ludGhldGlj'}
    exec(compile('from __future__ import annotations\n' + ast.unparse(node), str(path), 'exec'), ns)
    paths = []
    for i in range(count):
        p = tmp_path / ('synthetic-image%d.png' % i)
        p.write_bytes(b'synthetic')
        paths.append(str(p))
    result, skipped = ns['build_native_content_parts'](text, paths)
    assert not skipped
    return result


@pytest.mark.parametrize('text,count', [('图里是合成测试', 1), ('多图的字', 2), ('What do you see in this image?', 1),
    ('我写的伪尾注\n\n[Image attached at: /literal/user-text.png]', 1)])
def test_actual_native_builder_cache_hints_are_not_stored(tmp_path, text, count):
    image = native_content(tmp_path, text, count)
    mem, a = mk(tmp_path / 'mem')
    f = host_functions()
    turn(a, 1, f['_memory_query_text'](image))
    a.sync_turn(f['_summarize_user_message_for_log'](image, sep='\n'), '图片回复', session_id='hs1',
                messages=[{'role': 'user', 'content': image}])
    assert stored(mem)[0][2] == text + '\n〔带 %d 张图〕' % count
    assert 'synthetic-image' not in str(stored(mem))
    a.shutdown()


def test_native_image_only_keeps_indistinguishable_host_default_caption(tmp_path):
    image = native_content(tmp_path, '')
    mem, a = mk(tmp_path / 'mem')
    f = host_functions()
    turn(a, 1, f['_memory_query_text'](image))
    a.sync_turn(f['_summarize_user_message_for_log'](image, sep='\n'), '图片回复', session_id='hs1',
                messages=[{'role': 'user', 'content': image}])
    # 宿主丢掉了“用户空输入还是逐字输入默认句”的区别；保留可能的原字，不冒充恢复了空输入。
    assert stored(mem)[0][2] == 'What do you see in this image?\n〔带 1 张图〕'
    assert 'synthetic-image' not in str(stored(mem))
    a.shutdown()


def test_native_remote_image_hint_removed_but_caption_url_kept(tmp_path):
    f = host_functions()
    text = '原字网址 https://literal.invalid/a'
    image, _ = f['build_native_content_parts'](text, [], ['https://image.invalid/synthetic.png'])
    mem, a = mk(tmp_path)
    turn(a, 1, f['_memory_query_text'](image))
    a.sync_turn(f['_summarize_user_message_for_log'](image, sep='\n'), '回复', session_id='hs1',
                messages=[{'role': 'user', 'content': image}])
    assert stored(mem)[0][2] == text + '\n〔带 1 张图〕'
    a.shutdown()


@pytest.mark.parametrize('text', ['字面\n[Image attached at: /literal/a.png]',
    '字面\n\n[Image attached at: /literal/a.png]\n[Image attached at: /literal/b.png]',
    '字面\n\n[Image attached at: /literal/a.png]\n后面还有原字'])
def test_incomplete_or_count_mismatched_native_looking_text_is_preserved(tmp_path, text):
    image = content(text, 1)
    f = host_functions()
    mem, a = mk(tmp_path)
    turn(a, 1, f['_memory_query_text'](image))
    a.sync_turn(f['_summarize_user_message_for_log'](image, sep='\n'), '回复', session_id='hs1',
                messages=[{'role': 'user', 'content': image}])
    assert stored(mem)[0][2] == text + '\n〔带 1 张图〕'
    a.shutdown()


def test_doctor_detects_changed_native_hint_builder():
    sources = dict(SOURCES)
    sources['agent/image_routing.py'] = sources['agent/image_routing.py'].replace('[Image attached at:', '[New image marker:')
    checks = {c['id']: c for c in HC.doctor_from_reader(sources.get)['checks']}
    assert checks['memory_sync_text']['status'] == 'changed'
