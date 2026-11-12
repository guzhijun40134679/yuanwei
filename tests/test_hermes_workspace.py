"""交回点乙·第 2b 步：Hermes 的工作区＝项目（稳定编号）或「生活」；读取范围是用户的配置（甲全隔开／乙生活读全部项目读自己／丙全打通）；写入只写当前那个工作区；
只记主人本人的话；agent_context 缺了不当成主对话；每一类跳过按原因计数。
Hermes 不把「当前是哪个项目」传给插件（initialize 的 agent_workspace 恒为 "hermes"；v0.23 起只传 cwd），项目由 cwd 对着 $HERMES_HOME/projects.db 里的文件夹归属推出（和 Hermes 自己的
hermes_cli/projects_db.project_for_path 同一条规则：文件夹等于或是祖先，最长的赢，归档的不算）。这里用合成的 projects.db（只有我们读的两张表），不碰真实的 ~/.hermes。"""
import json
import os
import sqlite3

import pytest

from helpers import Scripted, make_mem
from helpers2 import fill
from yuanwei import hermes_workspace as HW
from yuanwei.hermes_adapter import HermesAdapter

SCHEMA = """CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT, icon TEXT, color TEXT, board_slug TEXT, primary_path TEXT,
 created_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE project_folders (project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path));"""


def make_home(tmp_path, projects=None, name="home"):
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    if projects is not None:
        c = sqlite3.connect(str(home / "projects.db"))
        c.executescript(SCHEMA)
        for pid, pname, paths, archived in projects:
            c.execute("INSERT INTO projects (id, slug, name, created_at, archived) VALUES (?,?,?,?,?)", (pid, pname.lower(), pname, 1, archived))
            for p in paths:
                c.execute("INSERT INTO project_folders (project_id, path, added_at) VALUES (?,?,1)", (pid, p))
        c.commit()
        c.close()
    return home


def rename(home, pid, new):
    c = sqlite3.connect(str(home / "projects.db"))
    c.execute("UPDATE projects SET name=?, slug=? WHERE id=?", (new, new.lower() + "-x", pid))
    c.commit()
    c.close()


# ================================================================== 工作区怎么认
def test_no_projects_db_means_no_projects_everything_is_life(tmp_path):
    home = make_home(tmp_path)
    for cwd, why in (("/work/a", "cwd_given"), (None, "no_cwd_life")):
        r = HW.resolve(str(home), cwd, "cli")
        assert r["key"] == "life" and r["kind"] == "life" and r["label"] == "生活" and r["reason"] == why


def test_cwd_inside_a_project_folder_gives_the_stable_id_key_not_the_name(tmp_path):
    home = make_home(tmp_path, [("p-111", "甲项目", ["/work/a"], 0), ("p-222", "乙项目", ["/work/b"], 0)])
    r = HW.resolve(str(home), "/work/a/sub/dir", "cli")
    assert r["key"] == "proj:p-111" and r["kind"] == "project" and r["label"] == "甲项目"
    assert HW.resolve(str(home), "/work/a", "cli")["key"] == "proj:p-111"
    assert HW.resolve(str(home), "/work/b/x", "cli")["key"] == "proj:p-222"


def test_renaming_the_project_changes_the_label_but_never_the_key(tmp_path):
    home = make_home(tmp_path, [("p-111", "甲项目", ["/work/a"], 0)])
    k1 = HW.resolve(str(home), "/work/a", "cli")
    rename(home, "p-111", "新名字")
    k2 = HW.resolve(str(home), "/work/a", "cli")
    assert k1["key"] == k2["key"] == "proj:p-111" and k1["label"] == "甲项目" and k2["label"] == "新名字"


def test_innermost_folder_wins_and_sibling_prefix_is_not_inside(tmp_path):
    home = make_home(tmp_path, [("p-out", "外", ["/work"], 0), ("p-in", "内", ["/work/a"], 0)])
    assert HW.resolve(str(home), "/work/a/x", "cli")["key"] == "proj:p-in"
    assert HW.resolve(str(home), "/work/ab", "cli")["key"] == "proj:p-out"                       # /work/ab 不在 /work/a 里，落到外层
    home2 = make_home(tmp_path, [("p-a", "甲", ["/work/a"], 0)], name="h2")
    assert HW.resolve(str(home2), "/work/ab", "cli")["key"] == "life"                           # 没有任何项目管它：确定没挂项目 → 生活


def test_archived_owner_or_ambiguity_is_unresolved_never_life(tmp_path):
    home = make_home(tmp_path, [("p-old", "旧", ["/work/a"], 1)])
    r = HW.resolve(str(home), "/work/a/x", "cli")
    assert r["key"] is None and r["reason"] == "project_archived"
    home2 = make_home(tmp_path, [("p-out", "外", ["/work"], 0), ("p-oldin", "旧内层", ["/work/a"], 1)], name="h2")
    r2 = HW.resolve(str(home2), "/work/a/x", "cli")
    assert r2["key"] is None and r2["reason"] == "project_archived"                              # 归档的内层更近：说不清，不落到外层、更不落进生活


def test_unreadable_or_foreign_projects_db_is_unresolved(tmp_path):
    home = make_home(tmp_path)
    (home / "projects.db").write_bytes(b"this is not sqlite" * 50)
    assert HW.resolve(str(home), "/work/a", "cli") == {"key": None, "kind": None, "label": None, "reason": "projects_db_unreadable"}
    home2 = make_home(tmp_path, name="h2")
    c = sqlite3.connect(str(home2 / "projects.db"))
    c.execute("CREATE TABLE other (x)")
    c.commit()
    c.close()
    assert HW.resolve(str(home2), "/work/a", "cli")["reason"] == "projects_db_schema_changed"


def test_missing_or_odd_cwd_with_a_projects_db(tmp_path):
    home = make_home(tmp_path, [("p-a", "甲", ["/work/a"], 0)])
    assert HW.resolve(str(home), None, "telegram")["key"] == "life"                              # 网关聊天（手机等）没有工作目录：没挂项目
    assert HW.resolve(str(home), None, "cli")["key"] == "life"                                    # 没给目录、也取不到进程目录：生活（2026-11-11 改：不再说不清，见下面那张表）
    assert HW.resolve(str(home), "relative/path", "cli")["reason"] == "cwd_not_absolute"
    assert HW.resolve(str(home), 5, "cli")["reason"] == "cwd_invalid"


def test_labels_file_remembers_each_workspace_and_updates_on_rename(tmp_path):
    home = make_home(tmp_path, [("p-a", "甲", ["/work/a"], 0)])
    HW.remember(str(home), HW.resolve(str(home), "/work/a", "cli"))
    HW.remember(str(home), HW.resolve(str(home), "/elsewhere", "cli"))
    assert HW.known(str(home)) == {"proj:p-a": {"kind": "project", "label": "甲"}, "life": {"kind": "life", "label": "生活"}}
    rename(home, "p-a", "甲改名")
    HW.remember(str(home), HW.resolve(str(home), "/work/a", "cli"))
    assert HW.known(str(home))["proj:p-a"]["label"] == "甲改名"


def test_readable_set_for_the_three_scopes():
    known = {"life": {"kind": "life", "label": "生活"}, "proj:a": {"kind": "project", "label": "甲"}, "proj:b": {"kind": "project", "label": "乙"}}
    assert HW.readable("isolated", "life", known) == ["life"] and HW.readable("isolated", "proj:a", known) == ["proj:a"]
    assert HW.readable("life_all", "life", known) == ["life", "proj:a", "proj:b"] and HW.readable("life_all", "proj:a", known) == ["proj:a"]
    assert HW.readable("all_open", "proj:a", known) == ["proj:a", "life", "proj:b"]
    assert HW.readable("typo", "life", known) == ["life"]                                         # 认不得的值按最严
    assert HW.readable("all_open", "proj:zzz", known)[0] == "proj:zzz"                            # 当前这个就算登记簿里还没有，也在范围里


# ================================================================== 适配器：写入只写当前工作区；读取范围由配置定
TXT = {"life": ["今天在家煮了咖喱饭", "咖喱里放了土豆"], "proj:p-a": ["甲项目的发布会定在周五", "发布会要准备演示稿"], "proj:p-b": ["乙项目要重构数据库", "数据库迁移用蓝绿"]}
SHARED = "shared.sqlite"


class World:
    """同一个库文件、不同工作区（和真实情况一样：库跟着 hermes_home 走，工作区是库里的一个 ns）。"""

    def __init__(self, tmp_path, scope="isolated", owners=None, seed=True, texts=None, threads=False, mem_cfg=None):
        self.texts, self.threads, self.mem_cfg = texts or TXT, threads, dict(mem_cfg or {})
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.home = make_home(tmp_path, [("p-a", "甲项目", ["/work/a"], 0), ("p-b", "乙项目", ["/work/b"], 0)])
        self.mems, self.cfg = {}, {"mode": "tools_only", "spawn": "off", "read_scope": scope, "owners": owners or {}}
        if seed:
            for key, texts in self.texts.items():
                fill(self.mem(key), "seed", texts, host="hermes")
                HW.remember(str(self.home), {"key": key, "kind": "life" if key == "life" else "project", "label": {"life": "生活", "proj:p-a": "甲项目", "proj:p-b": "乙项目"}[key], "reason": None})

    def mem(self, key):
        if key not in self.mems:
            if self.threads:                                                                              # 整理成主线：给「只给目录」模式用
                from helpers import out_thread, sids_of
                nm = {"life": "生活的主线", "proj:p-a": "甲项目的主线", "proj:p-b": "乙项目的主线"}[key]
                prov = Scripted(lambda request, n, nm=nm: out_thread(sids_of(request["text"]), name=nm))
            else:
                prov = Scripted("skip")
            self.mems[key] = make_mem(self.tmp, ws=key, name=SHARED, provider=prov, **self.mem_cfg)
        return self.mems[key]

    def adapter(self, cwd, platform="cli", session="s1", **kw):
        a = HermesAdapter(dict(self.cfg), executor="inline", memory_factory=self.mem)
        a.initialize(session, platform=platform, agent_context="primary", hermes_home=str(self.home), cwd=cwd, **kw)
        return a


def turn(a, n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


def call(a, name, args=None):
    return json.loads(a.handle_tool_call(name, args or {}))


def rows(mem):
    return [(r["role"], r["content"]) for r in mem.store.list_messages(mem.ns) if r["session_id"] != "seed"]


def msgs(text):
    return [{"role": "user", "content": text}, {"role": "assistant", "content": "答"}]


def test_write_goes_only_to_the_current_workspace_under_every_scope(tmp_path):
    for scope in ("isolated", "life_all", "all_open"):
        w = World(tmp_path / scope, scope=scope)
        for cwd, key in (("/work/a/x", "proj:p-a"), ("/somewhere/else", "life"), ("/work/b", "proj:p-b")):
            a = w.adapter(cwd)
            turn(a, 1, "写入测试 %s" % key)
            a.sync_turn("写入测试 %s" % key, "答", session_id="s1", messages=msgs("写入测试 %s" % key))
        for key in TXT:
            assert [t for r, t in rows(w.mem(key)) if r == "user"] == ["写入测试 %s" % key], (scope, key)


def test_scope_A_isolated_nobody_reads_anybody_else(tmp_path):
    w = World(tmp_path, scope="isolated")
    life, pa, pb = w.adapter("/elsewhere"), w.adapter("/work/a"), w.adapter("/work/b")
    for a in (life, pa, pb):
        turn(a, 1, "普通")
    assert "咖喱" in call(life, "recall", {"query": "咖喱"})["text"] and "发布会" not in call(life, "recall", {"query": "发布会 数据库"})["text"]
    assert "发布会" in call(pa, "recall", {"query": "发布会"})["text"]
    assert "咖喱" not in call(pa, "recall", {"query": "咖喱 数据库"})["text"] and "数据库" not in call(pa, "recall", {"query": "数据库"})["text"]
    assert "发布会" not in call(pb, "recall", {"query": "发布会 咖喱"})["text"]


def test_scope_B_life_reads_everything_projects_read_only_themselves(tmp_path):
    w = World(tmp_path, scope="life_all")
    life, pa = w.adapter("/elsewhere"), w.adapter("/work/a")
    turn(life, 1, "普通")
    turn(pa, 1, "普通")
    r = call(life, "recall", {"query": "咖喱 发布会 数据库"})
    assert r["status"] == "ok" and all(x in r["text"] for x in ("咖喱", "发布会", "数据库"))
    assert "咖喱" not in call(pa, "recall", {"query": "咖喱 数据库"})["text"] and "发布会" in call(pa, "recall", {"query": "发布会"})["text"]


def test_scope_C_everything_open(tmp_path):
    w = World(tmp_path, scope="all_open")
    pa = w.adapter("/work/a")
    turn(pa, 1, "普通")
    r = call(pa, "recall", {"query": "咖喱 发布会 数据库"})
    assert all(x in r["text"] for x in ("咖喱", "发布会", "数据库"))


def test_multi_workspace_materials_say_where_each_comes_from_and_the_receipt_lists_what_was_read(tmp_path):
    w = World(tmp_path, scope="life_all")
    life = w.adapter("/elsewhere")
    turn(life, 1, "普通")
    t = call(life, "recall", {"query": "咖喱 发布会 数据库"})["text"]
    assert t.startswith("〔读取范围：生活、甲项目、乙项目〕")
    for label in ("生活", "甲项目", "乙项目"):
        assert "〔工作区：%s〕" % label in t
    head, rest = t.split("〔工作区：生活〕", 1)
    life_part = rest.split("〔工作区：", 1)[0]
    assert "咖喱" in life_part and "发布会" not in life_part and "数据库" not in life_part                 # 每条原话在自己工作区的那一节里
    one = call(w.adapter("/work/a", session="s9"), "recall", {"query": "发布会"})
    assert "〔读取范围" not in one["text"] and "〔工作区：" not in one["text"]                              # 只读自己：和以前一样，不加标记


def test_multi_workspace_get_messages_and_cursor_route_to_the_right_workspace(tmp_path):
    w = World(tmp_path, scope="all_open")
    life = w.adapter("/elsewhere")
    life.cfg["recall_page_chars"] = 50
    for m in w.mems.values():
        m.cfg.recall_page_chars = 60
    turn(life, 1, "普通")
    first = call(life, "recall", {"query": "咖喱 发布会 数据库 土豆 演示稿 蓝绿"})["text"]
    import re
    cursors = re.findall(r"只给 cursor=(\S+)", first)
    assert cursors and all(c.startswith("W") and "~" in c for c in cursors)                              # 游标带着工作区前缀
    nxt = call(life, "recall", {"cursor": cursors[0]})
    assert nxt["status"] == "ok"
    ids = re.findall(r"〔[^｜〕]+｜([^〕]+)〕", first)
    got = call(life, "get_messages", {"ids": ids[:3]})
    assert got["status"] == "ok" and "〔工作区：" in got["text"]


def test_model_cannot_widen_the_scope_by_arguments_or_by_naming_a_workspace(tmp_path):
    w = World(tmp_path, scope="isolated")
    pa = w.adapter("/work/a")
    turn(pa, 1, "普通")
    for extra in ({"workspace": "life"}, {"scope": "all_open"}, {"read_scope": "all_open"}, {"workspaces": ["life", "proj:p-b"]}, {"workspace": "proj:p-b", "history_allowed": True}):
        r = call(pa, "recall", dict({"query": "咖喱 数据库"}, **extra))
        assert "咖喱" not in r["text"] and "数据库" not in r["text"], extra
    assert pa.status()["read_scope"] == "isolated"
    for tool, args in (("get_messages", {"ids": [r["msg_id"] for r in w.mem("life").store.list_messages(w.mem("life").ns)][:1], "workspace": "life"}), ("attachments", {"message_id": "x", "workspace": "life"})):
        out = call(pa, tool, args)
        assert "咖喱" not in out["text"]


def test_unresolvable_project_means_no_read_no_write_and_never_falls_into_life(tmp_path):
    w = World(tmp_path, scope="all_open")
    (w.home / "projects.db").write_bytes(b"garbage" * 100)
    a = w.adapter("/work/a")
    turn(a, 1, "普通")
    r = call(a, "recall", {"query": "咖喱 发布会"})
    assert r["status"] == "refused" and r["reason"] == "workspace_unresolved" and "咖喱" not in r["text"]
    assert a.prefetch("普通", session_id="s1") == ""
    a.sync_turn("普通", "答", session_id="s1", messages=msgs("普通"))
    assert all(rows(m) == [] for m in w.mems.values())                                              # 谁的库里都没多一条
    st = a.status()
    assert st["workspace"]["key"] is None and st["workspace"]["reason"] == "projects_db_unreadable" and st["skipped"] == {"workspace_unresolved": 1}


def test_session_switch_compression_and_new_keep_the_workspace_and_resume_on_a_gateway_platform_is_unknown(tmp_path):
    w = World(tmp_path, scope="isolated", owners={"telegram": ["42"]})
    a = w.adapter("/work/a", platform="telegram")                                                      # 网关平台：恢复会话照原来的规则（本机平台见 test_hermes_local_cwd.py）
    a.on_session_switch("s1-c", parent_session_id="s1", reset=False, reason="compression")
    assert a.status()["workspace"]["key"] == "proj:p-a"
    a.on_session_switch("s2", parent_session_id="", reset=True, reason="new_session")
    assert a.status()["workspace"]["key"] == "proj:p-a"
    a.on_session_switch("s2-b", parent_session_id="s2", reset=False, reason="branch")
    assert a.status()["workspace"]["key"] == "proj:p-a"                                                # 分叉：同一个对话的分支，工作目录不变
    a.on_session_switch("s3", parent_session_id="s2", reset=False, reason="resume")
    st = a.status()["workspace"]
    assert st["key"] is None and st["reason"] == "workspace_unknown_after_switch_resume"             # 网关平台恢复的会话工作目录插件看不到：说不清
    turn(a, 1, "普通")
    assert call(a, "recall", {"query": "发布会"})["reason"] == "workspace_unresolved"


# ================================================================== 只记主人本人的话
def test_local_sessions_are_the_owner_no_config_needed(tmp_path):
    w = World(tmp_path)
    for plat in ("cli", "local"):
        a = w.adapter("/elsewhere", platform=plat)
        turn(a, 1, "我自己说的")
        a.sync_turn("我自己说的", "答", session_id="s1", messages=msgs("我自己说的"))
        assert a.status()["recorded_turns"] == 1 and a.status()["owner_basis"] == "local_session"


def test_gateway_platform_without_a_configured_owner_is_unknown_not_owner(tmp_path):
    w = World(tmp_path)
    a = w.adapter(None, platform="telegram", user_id="u1", chat_type="dm")
    turn(a, 1, "私信里的话", author_id="u1", author_name="某人")
    assert call(a, "recall", {"query": "咖喱"})["status"] == "refused"                               # 不建立读取许可
    a.sync_turn("私信里的话", "答", session_id="s1", messages=msgs("私信里的话"), turn_author={"id": "u1", "name": "某人", "is_bot": False})
    assert rows(w.mem("life")) == [] and a.status()["skipped"] == {"owner_unknown": 1}


def test_gateway_owner_recorded_stranger_and_other_real_people_in_a_group_are_not(tmp_path):
    w = World(tmp_path, owners={"telegram": ["u-owner"]})
    a = w.adapter(None, platform="telegram", user_id="u-owner", chat_type="group")
    turn(a, 1, "主人在群里说的", author_id="u-owner", author_name="主人")
    assert call(a, "recall", {"query": "咖喱"})["status"] == "ok"
    a.sync_turn("主人在群里说的", "答", session_id="s1", messages=msgs("主人在群里说的"), turn_author={"id": "u-owner", "name": "主人", "is_bot": False})
    turn(a, 2, "群里另一个真人说的", author_id="u-friend", author_name="朋友")
    assert call(a, "recall", {"query": "咖喱"})["status"] == "refused"
    a.sync_turn("群里另一个真人说的", "答", session_id="s1", messages=msgs("群里另一个真人说的"), turn_author={"id": "u-friend", "name": "朋友", "is_bot": False})
    turn(a, 3, "陌生人私信", author_id="u-stranger")
    a.sync_turn("陌生人私信", "答", session_id="s1", messages=msgs("陌生人私信"))
    assert [t for r, t in rows(w.mem("life")) if r == "user"] == ["主人在群里说的"] and a.status()["skipped"] == {"not_owner": 2}
    turn(a, 4, "主人又说", author_id="u-owner")
    assert call(a, "recall", {"query": "咖喱"})["status"] == "ok"                                      # 别人的轮次没有把主人的许可冲掉也没有借走


def test_owner_can_be_matched_by_the_alt_id_and_other_platforms_ids_do_not_count(tmp_path):
    w = World(tmp_path, owners={"whatsapp": ["+8613800000000"], "telegram": ["u1"]})
    a = w.adapter(None, platform="whatsapp", user_id="lid-abc", user_id_alt="+8613800000000", chat_type="dm")
    turn(a, 1, "x", author_id="lid-abc")
    assert a.status()["owner_basis"] == "configured_owner_alt_id"
    b = w.adapter(None, platform="discord", user_id="u1", chat_type="dm", session="s2")
    turn(b, 1, "y", author_id="u1")                                                                  # 编号对得上 telegram 的主人，但这是 discord：不算
    assert call(b, "recall", {"query": "咖喱"})["status"] == "refused"


def test_authorized_user_is_not_the_same_as_owner_hermes_allowlist_and_pairing_are_not_consulted(tmp_path):
    w = World(tmp_path, owners={})
    a = w.adapter(None, platform="telegram", user_id="approved-by-pairing", chat_type="dm")
    turn(a, 1, "被配对批准的用户", author_id="approved-by-pairing")
    assert a.status()["skipped"] == {} and call(a, "recall", {"query": "咖喱"})["status"] == "refused"   # Hermes 的配对／白名单只说「可以用机器人」，不是主人


# ================================================================== agent_context 缺了不当成主对话
def test_missing_agent_context_is_inactive_not_primary(tmp_path):
    w = World(tmp_path)
    a = HermesAdapter(dict(w.cfg), executor="inline", memory_factory=w.mem)
    a.initialize("s1", platform="cli", hermes_home=str(w.home), cwd="/elsewhere")
    assert a.status()["active"] is False and a.status()["inactive_reason"] == "agent_context_missing"
    turn(a, 1, "普通")
    a.sync_turn("普通", "答", session_id="s1", messages=msgs("普通"))
    assert rows(w.mem("life")) == []


# ================================================================== 每一类跳过按原因计数
def test_every_kind_of_skip_is_counted_under_its_own_reason(tmp_path):
    w = World(tmp_path)
    a = w.adapter("/elsewhere")
    turn(a, 1, "A")
    a.sync_turn("A", "答", session_id="s1")                                                          # 没有 messages
    turn(a, 2, "B")
    a.sync_turn("B", "答", session_id="s1", messages=[{"role": "user", "content": "完全不同"}])         # 找不到当前用户行
    turn(a, 3, "C")
    m = msgs("C")
    m[0]["display_kind"] = "process_complete"
    a.sync_turn("C", "答", session_id="s1", messages=m)                                               # 自注入
    a.sync_turn("没有待配对", "答", session_id="s1", messages=msgs("没有待配对"))                         # 没配上
    turn(a, 4, "机器人", author_is_bot=True)
    a.sync_turn("机器人", "答", session_id="s1", messages=msgs("机器人"))
    assert a.status()["skipped"] == {"no_messages": 1, "current_user_row_not_found": 1, "machinery_row": 1, "no_pending_turn": 1, "bot_author": 1}
    assert a.status()["recorded_turns"] == 0


# ================================================================== 默认读取范围＝乙（用户定）；甲、丙是可选设置
def test_default_read_scope_is_B_life_reads_everything_projects_read_only_themselves(tmp_path):
    w = World(tmp_path, scope="isolated")
    del w.cfg["read_scope"]                                                                           # 用户没配：用默认
    life = w.adapter("/elsewhere")
    assert life.status()["read_scope"] == "life_all"
    turn(life, 1, "问")
    t = call(life, "recall", {"query": "咖喱"})["text"]
    assert "〔读取范围：" in t and "甲项目" in t and "乙项目" in t and "生活" in t
    proj = w.adapter("/work/a")
    assert proj.status()["read_scope"] == "life_all"
    turn(proj, 1, "问")
    t = call(proj, "recall", {"query": "咖喱"})["text"]
    assert "乙项目" not in t and "〔工作区：生活〕" not in t


def test_an_unknown_read_scope_value_falls_to_the_strictest_not_to_the_default(tmp_path):
    w = World(tmp_path, scope="everything")
    assert w.adapter("/work/a").status()["read_scope"] == "isolated"                                   # 认不得的值按最严（默认值只在没配时用）
