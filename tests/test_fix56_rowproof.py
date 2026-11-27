"""清单第 56 条第一批·第 5 节：行找不到、但宿主证明是用户开的。
a. 开头行找到了、后面有待办快照行（只有 _todo_snapshot_synthetic 这一个机器标记）：不算机器行，继续；别的机器行照旧整轮不记。
b. 开头行没找到（压缩改写了它，或宿主把便条并到了它前面）：只记开头句和回复。
假宿主模块，不 import 真宿主，不联网。"""
import fix56_fakehost as FH
import hermes_midturn_contract as MC
from test_fix55_midturn import Host, counts, skipped, stored_rows, turn
from test_hermes_adapter import mk, stored
from yuanwei import askcounts as AC
from yuanwei.hermes_adapter import HermesAdapter

OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]
TODO = "[Your active task list was preserved across context compression]\n- [>] 写口号 (in_progress)"
OPENING = "帮我想一句口号"


def host_rows(opening=OPENING, todo=True, after=(), flagged_opening=False):
    """S16 的行序：老轮次、开头行、助手调用待办、工具结果、（压缩时插的）待办快照行、助手、工具、最终回复。"""
    first = {"role": "user", "content": opening}
    if flagged_opening:
        first["_todo_snapshot_synthetic"] = True
    m = list(OLD) + [first,
                     {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "工具输出：这不该进记忆", "tool_call_id": "c1"}]
    if todo:
        m.append({"role": "user", "content": TODO, "_todo_snapshot_synthetic": True})
    m += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]}, {"role": "tool", "content": "工具输出二", "tool_call_id": "c2"}]
    m += list(after)
    m.append({"role": "assistant", "content": "事有原委，话有出处"})
    return m


def run(tmp_path, monkeypatch, messages, *, kind="user", opening=OPENING, user_content=None):
    mem, a = mk(tmp_path)
    ev = {"user": FH.user_event, "host": FH.host_event, None: None}[kind]
    FH.install(monkeypatch, ev(opening) if ev else None)
    turn(a, 1, opening)
    a.sync_turn(opening if user_content is None else user_content, "事有原委，话有出处", session_id="hs1", messages=messages)
    return mem, a


def got(mem):
    return [(r, c) for _s, r, c in stored(mem)]


# ================================================================== a. 待办快照行
def test_a_todo_snapshot_row_after_the_opening_row_is_recorded_when_the_host_says_user(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, host_rows())
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]
    assert skipped(a) == {} and counts(a)["totals"] == {}


def test_the_same_turn_is_not_recorded_when_the_origin_is_unknown_as_today(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, host_rows(), kind=None)
    assert got(mem) == [] and skipped(a) == {"machinery_row": 1}


def test_the_same_turn_is_not_recorded_when_the_host_says_it_started_it(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, host_rows(), kind="host")
    assert got(mem) == [] and skipped(a) == {"machinery_row": 1}


def test_another_machine_row_after_the_todo_row_still_drops_the_whole_turn(tmp_path, monkeypatch):
    for extra in ({"role": "user", "content": "后台完成", "display_kind": "process_complete"},
                  {"role": "user", "content": "催一下", "_empty_recovery_synthetic": True},
                  {"role": "user", "content": "催", "_dropped_toolcall_nudge": True}):
        mem, a = run(tmp_path / extra["content"], monkeypatch, host_rows(after=[extra]))
        assert got(mem) == [] and skipped(a) == {"machinery_row": 1}, extra


def test_a_todo_row_with_a_second_machine_marker_or_a_display_kind_is_not_ignored(tmp_path, monkeypatch):
    for i, bad in enumerate(({"display_kind": "process_complete"}, {"_empty_recovery_synthetic": True}, {"_custom_synthetic": True}, {"_merged_turn_prefix": "前面的话"},
                             {"_compressed_summary": True})):
        m = host_rows()
        todo = next(r for r in m if r.get("_todo_snapshot_synthetic"))
        todo.update(bad)
        mem, a = run(tmp_path / ("b%d" % i), monkeypatch, m)
        assert got(mem) == [] and skipped(a) == {"machinery_row": 1}, bad


def test_an_opening_row_that_itself_carries_the_todo_flag_is_still_a_machine_row(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, host_rows(flagged_opening=True, todo=False))
    assert got(mem) == [] and skipped(a) == {"machinery_row": 1}


def test_a_flagged_row_that_is_not_a_user_row_does_not_matter(tmp_path, monkeypatch):
    m = host_rows(todo=False)
    m.insert(-1, {"role": "tool", "content": "x", "_todo_snapshot_synthetic": True})
    mem, a = run(tmp_path, monkeypatch, m)
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]


# ================================================================== b. 开头行没找到
SUMMARY = {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] 前面聊过很多事", "_compressed_summary": True}


def lost_opening_rows(extra_before=(), after=()):
    """压缩把开头行并进了摘要：messages 里没有开头那句，只有摘要行、后面的助手和工具。"""
    return [SUMMARY] + list(extra_before) + [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "工具输出：这不该进记忆", "tool_call_id": "c1"}] \
        + list(after) + [{"role": "assistant", "content": "事有原委，话有出处"}]


def test_opening_row_gone_after_compression_records_the_opening_and_the_reply(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, lost_opening_rows())
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]
    assert skipped(a) == {}
    assert counts(a)["totals"] == {"opening_only:row_not_found": 1}


def test_opening_row_gone_with_unknown_or_host_origin_is_not_recorded_as_today(tmp_path, monkeypatch):
    for i, kind in enumerate((None, "host")):
        mem, a = run(tmp_path / ("k%d" % i), monkeypatch, lost_opening_rows(), kind=kind)
        assert got(mem) == []
        assert skipped(a) == ({"current_user_row_not_found": 1} if kind is None else {"machinery_row": 1})


def test_the_opening_only_turn_takes_no_midturn_words_and_no_card_answers(tmp_path, monkeypatch):
    from hermes_clarify_contract import make_result
    steer = MC.steer_row(MC.steer_join(["再加一条要求"]))
    card = [{"role": "assistant", "content": "", "tool_calls": [{"id": "q1", "type": "function", "function": {"name": "clarify", "arguments": "{}"}}]},
            {"role": "tool", "name": "clarify", "tool_call_id": "q1", "content": make_result([{"question": "要几个字？", "choices": ["四个", "六个"]}], {"q0": "六个"})}]
    mem, a = run(tmp_path, monkeypatch, lost_opening_rows(extra_before=card, after=[steer]))
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]               # 一个字都不带
    assert counts(a)["totals"] == {"opening_only:row_not_found": 1}


def test_opening_row_gone_and_the_last_user_row_is_a_machine_row_is_not_recorded(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, lost_opening_rows(after=[{"role": "user", "content": "后台完成", "display_kind": "process_complete"}]))
    assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


def test_a_todo_row_is_skipped_when_looking_for_the_last_user_row_but_another_machine_row_is_not(tmp_path, monkeypatch):
    todo = {"role": "user", "content": TODO, "_todo_snapshot_synthetic": True}
    mem, a = run(tmp_path, monkeypatch, lost_opening_rows(after=[todo]))
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]
    mem, a = run(tmp_path / "x", monkeypatch, lost_opening_rows(after=[todo, {"role": "user", "content": "唤醒", "_pre_verify_synthetic": True}]))
    assert got(mem) == []


def test_no_user_row_at_all_is_not_recorded(tmp_path, monkeypatch):
    m = [{"role": "assistant", "content": "好"}, {"role": "tool", "content": "x", "tool_call_id": "c"}]
    mem, a = run(tmp_path, monkeypatch, m)
    assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


def test_the_hosts_own_fixed_heads_are_still_not_recorded_on_this_path(tmp_path, monkeypatch):
    for i, head in enumerate(("[/plan — plan mode]\n计划…", "[/learn] The user wants…", "[/init] The user wants you to…", "Set up the 'x' automation for me", "[Continuing toward your standing goal]\n…",
                              "[IMPORTANT: MCP servers have been reloaded.]\n\n你好")):
        mem, a = run(tmp_path / ("h%d" % i), monkeypatch, lost_opening_rows(), opening=head)
        assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}, head


def test_a_redirect_pairing_is_not_this_path(tmp_path, monkeypatch):
    """改口配对（T1）找不到开头行：和今天一样整轮不记。"""
    mem, a = mk(tmp_path)
    h = Host(OPENING).tool().redirect("要四个字").final()
    del h.m[h.m.index({"role": "user", "content": OPENING})]
    FH.install(monkeypatch, FH.user_event(OPENING))
    turn(a, 1, OPENING)
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert got(mem) == []
    assert skipped(a) == {"current_user_row_not_found": 1} and counts(a)["totals"] == {}


def test_the_image_turn_is_not_this_path(tmp_path, monkeypatch):
    """带图：文字里有宿主追加的图片提示行，或最后一条用户行是带图片块的列表，都不走这条路（图片提示带本机路径，不存）。"""
    mem, a = run(tmp_path, monkeypatch, lost_opening_rows(), opening="看图\n\n[Image attached at: /tmp/x.png]")
    assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}
    m = lost_opening_rows()
    m[0] = {"role": "user", "content": [{"type": "text", "text": "压缩后的另一段字"}, {"type": "image_url", "image_url": {"url": "x"}}]}
    mem, a = run(tmp_path / "x", monkeypatch, m, opening="看图")
    assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


# ================================================================== 合并行（/reload-mcp 之后的下一句，清单第 7 行）
NOTE = "[IMPORTANT: MCP servers have been reloaded. Tools may have changed.]"


def merged_rows(words=OPENING):
    row = {"role": "user", "content": NOTE + "\n\n" + words, "_merged_turn_prefix": NOTE}
    return list(OLD) + [row, {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "工具输出", "tool_call_id": "c1"}, {"role": "assistant", "content": "事有原委，话有出处"}]


def test_a_merged_note_row_records_only_the_users_words_without_the_note(tmp_path, monkeypatch):
    mem, a = run(tmp_path, monkeypatch, merged_rows())
    assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")]
    assert all("MCP servers" not in c for _r, c in got(mem))


def test_a_merged_row_when_the_event_is_the_hosts_or_unknown_is_not_recorded(tmp_path, monkeypatch):
    mem, a = run(tmp_path / "h", monkeypatch, merged_rows(), kind="host")
    assert got(mem) == [] and skipped(a) == {"machinery_row": 1}
    mem, a = run(tmp_path / "n", monkeypatch, merged_rows(), kind=None)
    assert got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


def test_a_merged_row_whose_tail_is_a_machine_turn_is_not_recorded(tmp_path, monkeypatch):
    """宿主合并时丢了后一行的 display_kind，但事件说是用户开的、最后一条用户行没有机器标记……这里最后一条带着标记时不记。"""
    m = merged_rows()
    m[2]["display_kind"] = "process_complete"
    mem, a = run(tmp_path, monkeypatch, m)
    assert got(mem) == []


# ================================================================== 今天记得对的不变
def test_a_normal_turn_whose_row_is_found_is_unchanged_with_any_origin(tmp_path, monkeypatch):
    for i, kind in enumerate(("user", None)):
        m = list(OLD) + [{"role": "user", "content": OPENING}, {"role": "assistant", "content": "事有原委，话有出处"}]
        mem, a = run(tmp_path / ("n%d" % i), monkeypatch, m, kind=kind)
        assert got(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")] and skipped(a) == {} and counts(a)["totals"] == {}
