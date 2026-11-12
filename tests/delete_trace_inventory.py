"""删除的留痕清单（删除与密钥过滤·交回点甲，R1）。**唯一来源**：库里、库文件旁边的每一处，凡是可能带着原话、或者从原话转写出来的文字，逐处写删除时怎么处理。
设计文档第二部分的表是从这里抄的。tests/test_delete_inventory.py 现在就跑：v1–v10 的每一张表、每一列、org_rows 的每个字段（lstate.RECORD_FIELDS）、
判断记录里真实出现的每个键，都必须在这里有一行；以后谁加了表、加了列、加了字段，没在这里登记，测试就红。

三类（cls）：
  A 原文副本：和用户或助手说过的话一字不差（或能还原成原话的编码）。删除时整行/整个文件行删掉。
  B 派生文字：模型或程序从原话转写、概括、挑选出来的文字（摘要、标题、理由、主线名、模型输出）。
    没法证明和被删内容无关的，清成无内容的占位，不调模型重写。判断「有没有关」的办法见设计文档第 3.3 节（污染闭包）。
  C 纯编号和计数：只有编号、序号、计数、状态码、时间、固定文案。可以留；留着的要在这里逐项写明「确实没有原文」。
  H 内容哈希：原文（或含原文的请求）的哈希或哈希前缀。不是原文，但短句可以拿字典猜；能擦的擦，擦不掉的写进文档的残留清单。

action（怎么处理）：
  delete_rows    这一行跟着被删的消息、步、回一起删
  scrub          行留着，这一列清成固定值（空串、空列表、占位文字）
  placeholder    行留着，文字换成「来源已删除」的占位（见 TOMBSTONES）
  pseudonymize   行留着（键要用来防重放），哈希换成不可逆的编号
  void           行留着，内容作废（游标整个作废）
  keep           不动（C 类，或不受影响的行）
  gc_orphans     不再被任何别处引用的才删（向量按文字哈希全库核）
  file_rewrite   文件里的这类行要重写掉

since：这一列/这张表是 v11 才有的（计划中的，现在的库里没有；乙加上以后测试会核对它真的存在）。
"""

A, B, C, H = "A", "B", "C", "H"
CLASSES = (A, B, C, H)
ACTIONS = ("delete_rows", "scrub", "placeholder", "pseudonymize", "void", "keep", "gc_orphans", "file_rewrite")

TOMBSTONES = {
    "occ_title": "〔标题：来源对话已删除〕",
    "occ_summary": "这条摘要的来源被删了",
    "thread_name": "〔主线名称：来源对话已删除〕",
    "thread_desc": "〔主线说明：来源对话已删除〕",
    "reason": "〔理由已清除：来源对话已删除〕",
    "judgment": {"status": "void_by_deletion"},
}


def col(cls, action, note="", since=None):
    return {"cls": cls, "action": action, "note": note, "since": since}


def tbl(action, columns, note="", since=None, json_fields=None):
    return {"action": action, "columns": columns, "note": note, "since": since, "json": json_fields or {}}


# ================================================================ 一、库里的表（v1–v10 现有，加 v11 计划）
TABLES = {
    "workspaces": tbl("keep", {
        "ns": col(C, "keep"), "created_at": col(C, "keep"), "store_kind": col(C, "keep"), "store_version": col(C, "keep"), "index_version": col(C, "keep"),
        "last_seq": col(C, "keep", "不回退：序号不复用"), "org_seq": col(C, "keep", "不回退；删除时每个受影响的工作区加一，让在途的整理提交作废"), "vclock": col(C, "keep", "不回退"),
        "paused": col(C, "keep"), "paused_at": col(C, "keep"), "consec_abandons": col(C, "keep"), "budget_day": col(C, "keep"),
        "config": col(C, "keep", "只有写入提示词的名字（{write_prompt: …}），没有原文"),
        "del_ver": col(C, "keep", "删除版本：只增", since=11), "step_hwm": col(C, "keep", "步号高水位：只增", since=11), "thread_hwm": col(C, "keep", since=11),
        "occ_hwm": col(C, "keep", since=11), "gap_hwm": col(C, "keep", since=11), "redact_json": col(C, "keep", "挡掉的处数，按类型：只有计数", since=11),
    }, "工作区一行：只有编号、计数、状态"),
    "messages": tbl("delete_rows", {
        "ns": col(C, "delete_rows"), "msg_id": col(C, "delete_rows"), "host": col(C, "delete_rows"), "session_id": col(C, "delete_rows"), "branch": col(C, "delete_rows"),
        "host_msg_id": col(C, "delete_rows", "Hermes 的助手消息编号里有回复文字哈希的前 8 位；行删掉；防重放留的是整个键的哈希（del_keys）"),
        "seq": col(C, "delete_rows"), "role": col(C, "delete_rows"), "content": col(A, "delete_rows", "原话本体"), "msg_time": col(C, "delete_rows"), "time_precision": col(C, "delete_rows"),
        "received_at": col(C, "delete_rows"), "fingerprint": col(H, "delete_rows", "原话的哈希；行删掉"), "is_long": col(C, "delete_rows"), "step_id": col(C, "delete_rows"), "visible_at": col(C, "delete_rows"),
        "capture_seq": col(C, "delete_rows", "捕获序号（删除边界用）", since=11), "redacted": col(C, "delete_rows", "这条挡掉过几处", since=11), "redact_ver": col(C, "delete_rows", "过滤规则版本", since=11),
    }, "范围内的消息整行删（按逻辑窗口绑定的宿主、会话、分支，不分工作区）"),
    "anchors": tbl("delete_rows", {
        "ns": col(C, "delete_rows"), "anchor_id": col(C, "delete_rows"), "msg_id": col(C, "delete_rows"), "ord": col(C, "delete_rows"), "seq": col(C, "delete_rows"),
        "text": col(A, "delete_rows", "切句后的原话片段"), "start": col(C, "delete_rows"), "end": col(C, "delete_rows"), "index_version": col(C, "delete_rows"),
    }, "跟着被删消息整行删"),
    "steps": tbl("delete_rows", {
        "ns": col(C, "delete_rows"), "step_id": col(C, "delete_rows"), "k": col(C, "delete_rows", "步号不复用：步号高水位 + 已删步登记"), "host": col(C, "delete_rows"), "session_id": col(C, "delete_rows"),
        "branch": col(C, "delete_rows"), "status": col(C, "delete_rows"), "msg_ids": col(C, "delete_rows"), "long_msg_ids": col(C, "delete_rows"), "sealed_at": col(C, "delete_rows"),
        "sealed_reason": col(C, "delete_rows"), "ended_at": col(C, "delete_rows"), "vclock_end": col(C, "delete_rows"), "prompt_version": col(C, "delete_rows"), "prompt_sha256": col(C, "delete_rows"),
        "config_version": col(C, "delete_rows"), "failures": col(C, "delete_rows"),
        "last_error": col(B, "scrub", "失败原因：repr(异常)。属于被删对话的步整行删；留下的步里，若这一步受污染（见设计文档 3.3）清成固定码，否则不动"),
        "note": col(C, "keep", "固定短码：abandoned、all_long"), "last_failed_at": col(C, "delete_rows"), "config_snapshot": col(C, "delete_rows", "写入配置快照，没有原话"),
    }, "属于被删对话的步整行删；留下的步不动（只清 last_error 的受污染情形）"),
    "bindings": tbl("delete_rows", {
        "ns": col(C, "delete_rows"), "step_id": col(C, "delete_rows"),
        "data": col(H, "delete_rows", "{step_id, session_id, k, msg_ids, msg_fp_digest}：编号加消息指纹的摘要哈希，没有模型复述。别的步的绑定不用动（Codex R1 表：不必误删）"),
    }, "只删被删对话自己的步的绑定"),
    "org_rows": tbl("scrub", {
        "id": col(C, "keep", "自增行号，保持读取顺序；不复用靠 AUTOINCREMENT"), "ns": col(C, "keep"), "tbl": col(C, "keep"), "key": col(C, "keep", "键里只有编号、版本号"),
        "created_seq": col(C, "keep"), "data": col(B, "scrub", "整行的 JSON：每个字段的处理见下面 json 里的 org_rows.<表> 清单"),
    }, "按表按字段处理：被删句子的行删；回、主线按「引用」判；受污染的派生文字清成占位", json_fields={}),
    "judgments": tbl("scrub", {
        "ns": col(C, "keep"), "step_id": col(C, "keep"), "question": col(C, "keep", "含 q1~f1 这类失败归档名"), "attempt": col(C, "keep"),
        "data": col(A, "scrub", "完整请求（含原话、历史卡片里的原话节选）、模型输出、校验理由、重问反馈、错误：见下面 json 里的 judgments 清单"),
    }, "被删对话的步：整行删。留下的步：请求里出现过受污染对象的，data 清成 {status: void_by_deletion}（还没结束的步整行删，让它按删除后的状态重新来）"),
    "events": tbl("keep", {
        "id": col(C, "keep"), "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "kind": col(C, "keep", "end、abort"),
        "at_seq": col(C, "keep"), "ts": col(C, "keep"),
    }, "只有编号和时间：留（会话号不是原话）"),
    "cost_ledger": tbl("keep", {
        "id": col(C, "keep"), "ns": col(C, "keep"), "step_id": col(C, "keep", "指向已删步的编号：只是编号"), "ts": col(C, "keep"), "day": col(C, "keep"), "model": col(C, "keep"),
        "fingerprint": col(C, "keep", "模型服务的指纹，不是原话的哈希"), "usage": col(C, "keep", "词元数 JSON"), "cny": col(C, "keep"), "unknown": col(C, "keep"),
        "note": col(C, "keep", "只有两句固定文案（核过 memory.py：网络失败按最坏估计记、回应里没有用量）"),
        "currency": col(C, "keep", "币种 CNY／USD／NONE（v12）：金额不混币种；NONE＝没有价格表，只记用量", since=12),
    }, "成本账：花出去的钱不能抹，没有原文：留"),
    "cursors": tbl("void", {
        "cursor_id": col(C, "keep", "随机号"), "ns": col(C, "keep"), "kind": col(C, "keep"), "created_at": col(C, "keep"),
        "data": col(A, "void", "续读游标：query、queries（查询原文）、chunks（整段原话）、cut。受影响工作区的全部游标整个作废（data 换成 {void: history_deleted}），不猜归属"),
    }, "受影响工作区的全部游标作废；旧游标再来读，报「历史已删除，请重新查询」"),
    "vector_identities": tbl("keep", {"fp": col(C, "keep"), "data": col(C, "keep", "编码身份：提供方、模型、维度，没有原文")}, "留"),
    "vectors": tbl("gc_orphans", {
        "fp": col(C, "keep"), "text_sha": col(H, "gc_orphans", "实际送进模型的文字的 sha256（锚点文字或查询文字）"), "dim": col(C, "keep"),
        "vec": col(B, "gc_orphans", "嵌入向量：文字的数值表示，不是文字，但能部分还原语义"),
    }, "删完以后，全库已经没有任何锚点还用到的 text_sha（含查询的向量）整行删；还有别处用到的不删"),
    "turn_marks": tbl("keep", {"ns": col(C, "keep"), "turn_id": col(C, "keep"), "refused": col(C, "keep"), "created_at": col(C, "keep")}, "留"),
    "read_settings": tbl("keep", {"ns": col(C, "keep"), "scope": col(C, "keep"), "key": col(C, "keep", "会话号"), "allowed": col(C, "keep"), "set_at": col(C, "keep")}, "留"),
    "current_turn": tbl("keep", {"ns": col(C, "keep"), "turn_id": col(C, "keep"), "session_id": col(C, "keep"), "at": col(C, "keep")}, "留"),
    "turns": tbl("keep", {
        "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "turn_id": col(C, "keep"), "gen": col(C, "keep"), "state": col(C, "keep"),
        "origin": col(C, "keep"), "refused": col(C, "keep"), "refuse_reason": col(C, "keep", "固定原因码"), "persistent": col(C, "keep"),
        "control_kind": col(C, "keep"), "control_span": col(C, "keep", "口令在输入里的起止位置，两个数，不是原句（Codex 建议 2 核过）"),
        "created_at": col(C, "keep"), "completed_at": col(C, "keep"), "closed_at": col(C, "keep"),
    }, "留（只有编号、状态、数字范围）"),
    "read_log": tbl("scrub", {
        "id": col(C, "keep"), "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "turn_id": col(C, "keep"), "gen": col(C, "keep"),
        "call_id": col(C, "keep"), "entry": col(C, "keep"), "kind": col(C, "keep"),
        "detail": col(H, "scrub", "JSON：消息编号和片段范围（spans）、返回文字的哈希前缀（fingerprint）、字数、固定原因码。提到被删消息编号的行，spans 和 fingerprint 清掉；解析不了的行整个 detail 换成 {}"),
        "ts": col(C, "keep"),
    }, "读取登记：只有编号、范围、字数、哈希前缀；提到被删消息的清 spans 和 fingerprint"),
    "connections": tbl("keep", {
        "handle_sha": col(C, "keep", "随机句柄的哈希"), "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "created_at": col(C, "keep"),
        "claimed_by": col(C, "keep"), "claimed_at": col(C, "keep"), "closed_at": col(C, "keep"), "turn_id": col(C, "keep"),
    }, "留"),
    "dir_state": tbl("keep", {
        "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "epoch": col(C, "keep"), "state": col(C, "keep"), "reason": col(C, "keep", "固定原因码"),
        "directory_version": col(C, "keep", "dir-o<整理序号> 这样的版本串，没有目录正文"), "updated_at": col(C, "keep"),
    }, "目录正文不存库，只存版本：留。删除让整理序号加一，目录版本随之变，下一次会重新交"),
    "turn_tokens": tbl("pseudonymize", {
        "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"),
        "event_key": col(H, "pseudonymize", "Hermes：「轮次序号:输入文字哈希前 12 位」。被删窗口里的轮次，哈希部分换成不可逆编号（防重放靠 turn_id 和 del_keys，不靠它）"),
        "turn_id": col(C, "keep"), "created_at": col(C, "keep"),
    }, "被删窗口的轮次：event_key 的哈希前缀换编号"),
    "inputs": tbl("scrub", {
        "ns": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "turn_id": col(C, "keep"), "seq": col(C, "keep"),
        "input_key": col(H, "pseudonymize", "没有事件编号时是「输入文字哈希前 16 位#第几次」"),
        "fp": col(H, "scrub", "输入文字的完整 sha256：清成空串"), "state": col(C, "keep"), "label": col(C, "keep"), "ctl_kind": col(C, "keep"), "ctl_span": col(C, "keep", "两个数"),
        "ban_reason": col(C, "keep"), "ban_persistent": col(C, "keep"), "created_at": col(C, "keep"), "resolved_at": col(C, "keep"),
    }, "被删窗口的输入：fp 清空，input_key 的哈希换编号"),
    "tool_calls": tbl("keep", {
        "ns": col(C, "keep"), "host": col(C, "keep"), "call_id": col(C, "keep", "宿主给的调用编号"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "turn_id": col(C, "keep"),
        "gen": col(C, "keep"), "conflict": col(C, "keep"), "created_at": col(C, "keep"),
    }, "留"),
    "turn_messages": tbl("delete_rows", {
        "ns": col(C, "delete_rows"), "host": col(C, "delete_rows"), "session_id": col(C, "delete_rows"), "branch": col(C, "delete_rows"), "turn_id": col(C, "delete_rows"), "msg_id": col(C, "delete_rows"),
    }, "绑定到被删消息的行删"),
    "windows": tbl("keep", {
        "window_id": col(C, "keep"), "state": col(C, "keep"), "control_version": col(C, "keep", "窗口开关版本：删除不借用它，也不改它"), "ctl_source": col(C, "keep"), "ctl_op_id": col(C, "keep"),
        "ctl_at": col(C, "keep"), "origin": col(C, "keep"), "parent_window_id": col(C, "keep"), "host": col(C, "keep"), "created_at": col(C, "keep"), "input_seq": col(C, "keep"),
    }, "窗口本身不删：它的开关状态、绑定都留；删除只清窗口下面记下的原话和整理"),
    "window_bindings": tbl("keep", {
        "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "window_id": col(C, "keep"), "bound_via": col(C, "keep"),
        "host_parent_session_id": col(C, "keep"), "bound_at": col(C, "keep"),
    }, "留（删除要靠它找到窗口名下的会话号）"),
    "window_inputs": tbl("scrub", {
        "window_id": col(C, "keep"), "win_seq": col(C, "keep", "窗口内输入序号：不复用"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "turn_id": col(C, "keep"),
        "input_key": col(H, "pseudonymize", "同 turn_tokens.event_key"), "fp": col(H, "scrub", "输入文字的完整 sha256：清成空串"), "ns": col(C, "keep"),
        "result": col(C, "keep", "InputControl 的 JSON：只有状态、原因码、固定文案、数字"), "created_at": col(C, "keep"),
    }, "被删窗口的输入：fp 清空、input_key 哈希换编号；序号留着（防重放、防过期的旧关闭生效）"),
    "window_ops": tbl("keep", {
        "op_id": col(C, "keep"), "window_id": col(C, "keep"), "kind": col(C, "keep"), "source": col(C, "keep"), "input_win_seq": col(C, "keep"), "version_before": col(C, "keep"),
        "version_after": col(C, "keep"), "outcome": col(C, "keep"), "reason": col(C, "keep", "固定原因码"), "created_at": col(C, "keep"), "confirmed_at": col(C, "keep"),
    }, "留"),
    "window_permits": tbl("keep", {
        "permit_sha": col(C, "keep"), "kind": col(C, "keep"), "window_id": col(C, "keep"), "host": col(C, "keep"), "install_id": col(C, "keep", "hermes_home 路径"), "scope_set": col(C, "keep"),
        "session_id": col(C, "keep"), "branch": col(C, "keep"), "ns": col(C, "keep"), "turn_id": col(C, "keep"), "turn_gen": col(C, "keep"), "input_win_seq": col(C, "keep"),
        "tool_name": col(C, "keep"), "params_fp": col(H, "scrub", "工具参数的哈希：读取许可的 params_fp 含查询文字的哈希。被删窗口名下已经用完或过期的许可清成空串（没用完的在窗口控制版本变时已作废）"),
        "control_version": col(C, "keep"), "call_id": col(C, "keep"), "issued_at": col(C, "keep"), "expires_at": col(C, "keep"), "state": col(C, "keep"), "claimed_at": col(C, "keep"),
        "finished_at": col(C, "keep"), "void_reason": col(C, "keep"),
    }, "被删窗口名下的许可：params_fp 清空，其余留（许可 2 分钟过期）"),
    "window_pending": tbl("keep", {
        "pending_id": col(C, "keep"), "window_id": col(C, "keep"), "control_version": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep"), "ns": col(C, "keep"),
        "turn_id": col(C, "keep"), "request_win_seq": col(C, "keep"), "requested_at": col(C, "keep"), "state": col(C, "keep"), "void_reason": col(C, "keep"), "resolved_at": col(C, "keep"),
        "resolved_win_seq": col(C, "keep"),
    }, "「待确认开启」：留。删除确认另起一张表（delete_pending，v11）"),
    "schema_meta": tbl("keep", {"version": col(C, "keep")}, "留"),
    "sqlite_sequence": tbl("keep", {"name": col(C, "keep"), "seq": col(C, "keep", "AUTOINCREMENT 的高水位：org_rows、cost_ledger、events、read_log 的行号不复用")}, "SQLite 内部表"),
    # ---- v11 新表（计划）
    "store_meta": tbl("keep", {"k": col(C, "keep"), "v": col(H, "gc_orphans", "库身份 db_id、规则版本、向量补算的状态（退避、探针向量、签名）；vec_skip:* 是「算不了」文字的哈希，删除时按剩余锚点清（_gc_vectors）")}, "库的身份（防确认被拿到别的库里用）", since=11),
    "captures": tbl("keep", {"id": col(C, "keep"), "n": col(C, "keep", "全库单调递增的捕获序号")}, "事件进来时取号，划删除边界用", since=11),
    "deletions": tbl("keep", {
        "del_id": col(C, "keep"), "window_id": col(C, "keep"), "conv_kind": col(C, "keep"), "conv_sha": col(C, "keep", "对话编号的哈希"), "source": col(C, "keep", "command｜chat"), "state": col(C, "keep"),
        "code": col(C, "keep", "固定状态码"), "floor_capture": col(C, "keep"), "requested_at": col(C, "keep"), "registered_at": col(C, "keep"), "completed_at": col(C, "keep"),
        "counts": col(C, "keep", "各类删了几条的计数 JSON"), "plan": col(C, "scrub", "进行中才有：要清的编号清单（只有编号，没有文字）；完成后清成 NULL"),
        "pending_id": col(C, "keep"), "input_win_seq": col(C, "keep"), "db_id": col(C, "keep"),
    }, "墓碑和删除任务：只有编号、计数、状态码", since=11),
    "del_scope": tbl("keep", {"del_id": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"), "branch": col(C, "keep")}, "屏障：哪些（宿主、会话、分支）在哪个捕获序号之前的写入作废", since=11),
    "del_keys": tbl("keep", {"key_sha": col(H, "keep", "（宿主|会话|分支|宿主消息号）的哈希：防重放，不含原话"), "del_id": col(C, "keep")}, "被删消息的键哈希", since=11),
    "no_record_turns": tbl("keep", {
        "turn_id": col(C, "keep"), "host": col(C, "keep"), "window_id": col(C, "keep"), "reason": col(C, "keep"), "del_id": col(C, "keep"), "capture_seq": col(C, "keep"),
    }, "「确认那一轮不记」的持久标记", since=11),
    "deleted_steps": tbl("keep", {"ns": col(C, "keep"), "k": col(C, "keep"), "del_id": col(C, "keep")}, "已删步号登记：步序检查把它算作「已结束」，步号不复用", since=11),
    "delete_pending": tbl("keep", {
        "pending_id": col(C, "keep"), "window_id": col(C, "keep"), "db_id": col(C, "keep"), "control_version": col(C, "keep"), "host": col(C, "keep"), "session_id": col(C, "keep"),
        "branch": col(C, "keep"), "ns": col(C, "keep"), "turn_id": col(C, "keep"), "request_win_seq": col(C, "keep"), "request_capture": col(C, "keep"), "requested_at": col(C, "keep"),
        "state": col(C, "keep"), "void_reason": col(C, "keep"), "resolved_at": col(C, "keep"), "resolved_win_seq": col(C, "keep"), "del_id": col(C, "keep"),
    }, "「待确认删除」", since=11),
}

# ================================================================ 二、JSON 列里的字段（逐个）
# org_rows.data：按 tbl 分表，字段就是 lstate.RECORD_FIELDS（测试核全）+ key、created_seq
ORG_FIELDS = {
    "threads": {"thread_id": col(C, "keep"), "created_by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "thread_names": {"thread_id": col(C, "keep"), "version": col(C, "keep"), "name": col(B, "placeholder", "TOMBSTONES.thread_name：主线由被删的步创建、或创建它的步受污染时"), "by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "thread_descs": {"thread_id": col(C, "keep"), "version": col(C, "keep"), "text": col(B, "placeholder", "TOMBSTONES.thread_desc：同上"), "by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "occasions": {"occasion_id": col(C, "keep"), "thread_id": col(C, "keep"), "created_by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "occ_titles": {"occasion_id": col(C, "keep"), "version": col(C, "keep"), "title": col(B, "placeholder", "TOMBSTONES.occ_title：这一回有被删的句子挂在它下面、或标题由被删/受污染的步写的：每个版本都换"), "by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "occ_summaries": {"occasion_id": col(C, "keep"), "version": col(C, "keep"), "text": col(B, "placeholder", "TOMBSTONES.occ_summary：同上，每个版本都换"), "by": col(C, "keep"),
                      "reason": col(B, "placeholder", "改摘要的理由：TOMBSTONES.reason"), "evidence_sentences": col(C, "scrub", "依据句编号：被删句子的编号去掉"), "request_fp": col(H, "scrub", "请求哈希：清成 null"),
                      "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "sentences": {"sentence_id": col(C, "delete_rows"), "msg_id": col(C, "delete_rows"), "session_id": col(C, "delete_rows"), "start": col(C, "delete_rows"), "end": col(C, "delete_rows"),
                  "text": col(A, "delete_rows", "句子正文：原话副本"), "role": col(C, "delete_rows"), "date": col(C, "delete_rows"), "ord": col(C, "delete_rows"), "created_by": col(C, "delete_rows"),
                  "key": col(C, "delete_rows"), "created_seq": col(C, "delete_rows")},
    "attachments": {"sentence_id": col(C, "keep"), "kind": col(C, "keep"), "target_id": col(C, "keep"), "confidence": col(C, "keep"),
                    "candidates": col(C, "scrub", "候选编号：指向被删掉的回/主线的去掉"), "reason": col(B, "placeholder", "挂接理由（模型写的，可能复述原话）：被删句子的挂接整行删；留下的句子，所在的步受污染时换 TOMBSTONES.reason"),
                    "created_by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "gaps": {"gap_id": col(C, "keep"), "sentence_ids": col(C, "scrub", "被删句子的编号去掉；去完为空的整行删"), "layer": col(C, "keep"), "known_thread": col(C, "keep"),
             "words": col(B, "scrub", "补找用词（从句子里抠出来的词）：被删对话的缺口整行删；留下的步受污染时清成空列表"), "found": col(C, "scrub", "找回的编号：指向被删对象的去掉"),
             "outcome": col(C, "keep"), "created_by": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep")},
    "receipts": {"kind": col(C, "keep"), "commit_seq": col(C, "keep"), "key": col(C, "keep"), "created_seq": col(C, "keep"), "step_id": col(C, "keep", "被删步的收据整行删（步号由 deleted_steps 记着）；留着的步不动"),
                 "session_id": col(C, "keep"), "k": col(C, "keep"), "base_seq": col(C, "keep"), "msg_fp_digest": col(H, "keep", "消息指纹的摘要哈希：留着的步的收据不动；被删步的收据整行删"),
                 "input_fp": col(H, "scrub", "任务指纹：含候选卡片的请求的哈希。受污染的步清成空串；其余不动（只在提交恢复时比对一次，之后不用）"), "touched_threads": col(C, "scrub", "编号：已删主线的去掉"), "q1_sends": col(C, "keep"), "q2_sends": col(C, "keep"), "sentences": col(C, "keep"),
                 "attachments": col(C, "keep"), "gaps": col(C, "keep"), "rejudged": col(C, "keep"), "new_threads": col(C, "keep"), "new_occasions": col(C, "keep"),
                 "summary_edits_proposed": col(C, "keep"), "summary_edits_effective": col(C, "keep"),
                 "op": col(C, "keep"), "op_id": col(C, "keep"), "op_fp": col(H, "scrub", "操作内容的哈希（内容含 reason）。受污染的步清成空串；其余不动"), "operator": col(C, "keep"),
                 "reason": col(B, "placeholder", "操作收据（放弃一步）的 reason：拼进了模型输出的校验问题，可能引用原话。被删步的整行删；留下的步受污染时换 TOMBSTONES.reason")},
}

# judgments.data 的键（ask.py 的 record；测试用真实跑出来的行核）
JUDGMENT_KEYS = {
    "task_fp": col(H, "scrub"), "request_fp": col(H, "scrub"), "request": col(A, "scrub", "整份请求：见 REQUEST_KEYS"), "feedback": col(B, "scrub", "重问时带的拒收理由"),
    "status": col(C, "keep"), "raw_output": col(B, "scrub", "模型原始输出"), "validation": col(B, "scrub", "{ok, reasons, notes}：理由可能引用输出里的文字"), "error": col(B, "scrub", "repr(异常)"),
    "base_seq": col(C, "keep"), "k": col(C, "keep"), "step_id": col(C, "keep"), "session_id": col(C, "keep"), "attempt": col(C, "keep"), "question": col(C, "keep"), "feedback_version": col(C, "keep"),
}
REQUEST_KEYS = {
    "question": col(C, "scrub"), "phase": col(C, "scrub"), "text": col(A, "scrub", "完整请求文字：提示词、候选卡片（含历史原话节选）、这次对话的全部句子"),
    "cards_text": col(A, "scrub", "卡片文字：历史原话节选、摘要"), "shown_threads": col(C, "scrub", "展示过的主线编号：污染判断靠它"), "shown_occasions": col(C, "scrub", "展示过的回编号：污染判断靠它"),
    "dropped": col(C, "scrub"), "not_listed": col(C, "scrub"), "cards_tokens": col(C, "scrub"), "total_tokens": col(C, "scrub"), "session_date": col(C, "scrub"), "allow_summary_edit": col(C, "scrub"),
    "summaries": col(B, "scrub", "卡片里每回的摘要"), "found_threads": col(C, "scrub"), "found_occasions": col(C, "scrub"), "found_occasion_map": col(C, "scrub"), "judge_sids": col(C, "scrub"),
    "feedback": col(B, "scrub"), "task_fp": col(H, "scrub"), "request_fp": col(H, "scrub"), "step_id": col(C, "scrub"), "feedback_version": col(C, "scrub"),
}

# cursors.data 的键（reader.py、paging.py、note.py 里 put_cursor 写的）
CURSOR_KEYS = {
    "query": col(A, "void", "查询原文"), "queries": col(A, "void", "查询原文加补充关键词"), "cut": col(C, "void"), "chunks": col(A, "void", "整段原话（content）和指纹"), "delivered": col(C, "void"),
    "budget": col(C, "void"), "exclude": col(C, "void"), "config": col(C, "void"), "routes": col(C, "void"), "stats": col(C, "void"), "origin": col(C, "void"),
}

# 待补文件、领取文件、待核文件里一行的键
PENDING_LINE_KEYS = {
    "host": col(C, "file_rewrite"), "session_id": col(C, "file_rewrite"), "message_id": col(C, "file_rewrite"), "role": col(C, "file_rewrite"), "text": col(A, "file_rewrite", "原话本体"),
    "time": col(C, "file_rewrite"), "branch": col(C, "file_rewrite"), "time_precision": col(C, "file_rewrite"), "stable_id": col(C, "file_rewrite"), "turn_id": col(C, "file_rewrite"),
    "received_at": col(C, "file_rewrite"), "capture": col(C, "file_rewrite", "捕获序号：v11 起写；没有这一键的行一定是 v11 之前写的，按「早于任何删除」处理", since=11),
    "kinds": col(C, "file_rewrite", "挡掉的类型名列表", since=11),
    "rv": col(C, "file_rewrite", "过滤规则版本：v11 起写；没有或低于当前版本的行，补进库之前再过一遍过滤", since=11), "redacted": col(C, "file_rewrite", "这条挡掉过几处", since=11),
}
REVIEW_RECORD_KEYS = {
    "reason": col(B, "file_rewrite", "repr(异常)：冲突、解析错、校验错；可能带出一小段文字，被删窗口的待核行整行去掉"), "at": col(C, "file_rewrite"),
    "line": col(A, "file_rewrite", "原行（一整行待补 JSON）"), "line_b64": col(A, "file_rewrite", "原行的 base64（解不开 UTF-8 的字节）：解码以后按 line 同样处理"),
}

# ================================================================ 三、库文件旁边的东西
FILES = {
    "<库>.sqlite": ("A/B/H", "表里的内容：按上面逐表处理，然后 secure_delete（先设后核实）、VACUUM、wal_checkpoint(TRUNCATE)"),
    "<库>.sqlite-wal": ("A/B/H", "WAL 里留着改前的页：checkpoint(TRUNCATE) 并核实 WAL 为 0 字节；busy 就不算完成"),
    "<库>.sqlite-shm": ("C", "WAL 索引，没有行内容：不处理"),
    "<库>.sqlite.d/pending-<工作区哈希>.jsonl": ("A", "待补当前文件：重写，去掉范围内的行（含无法解析、无法归属的行）"),
    "<库>.sqlite.d/pending-<工作区哈希>.jsonl.claim-<12 位数字>": ("A", "已领取的批次：同上"),
    "<库>.sqlite.d/pending-<工作区哈希>.jsonl.claim-<8 位十六进制>": ("A", "0c6a58b 留下的随机名领取文件：同上"),
    "<库>.sqlite.d/pending-<工作区哈希>.jsonl.review": ("A", "待核：整行（含 line_b64 解码后的内容）按同样的范围判"),
    "<库>.sqlite.d/*.tmp、*.claim-*.tmp": ("A", "补写中断留下的临时副本：同上（重写或整个删掉）；现在补写者认不出这种名字，会一直留着"),
    "<库>.sqlite.d/*.lock、*.flush.lock": ("C", "空文件，只用来加锁：不处理"),
    "<库>.sqlite.run.lock": ("C", "空文件：不处理"),
    "<库>.sqlite.d/delete.lock": ("C", "删除锁（v11 新增），空文件"),
    "<库>.sqlite.vec.lock": ("C", "向量补算的锁，空文件：不处理"),
    "<库>.sqlite.spawn.log": ("B", "后台子进程的 stderr（异常类别和固定文案，读出来时还要过密钥过滤）：删除第 2 阶段清空，不猜归属"),
    "<hermes_home>/yuanwei/status.json": ("C", "运行时的数和原因，没有原话（hermes_adapter._dump_status）：不处理"),
    "<hermes_home>/yuanwei/workspaces.json": ("C", "工作区标签（项目名）：不是对话内容，不处理"),
}
MEMORY = {
    "Reader._snaps（每个 Reader、每个 Memory 实例）": ("A", "同进程里所有 Reader 实例都清（Reader 登记表）；别的进程：快照键带删除版本，下次读发现版本变了就丢弃并重建。别的进程里已经读出来的内存不能保证（写进边界）"),
    "Snapshot.rows / anchors / index（BM25、向量矩阵）": ("A", "随快照一起丢"),
    "ModeRunner 后台取材线程里的快照和结果": ("A", "线程每次开新连接、新 Reader；返回前核删除版本，不对就丢弃"),
    "Memory.last_flush_errors": ("B", "repr(异常) 列表：删除完成时清空"),
    "HermesAdapter._pending / _cur / _cur_ref": ("H", "只有轮次标记和输入指纹（Access.text_fp）：删除完成后清掉这个窗口的条目"),
    "WindowService._suspect": ("C", "三元组集合：不处理"),
}
EXTERNAL_BOUNDARY = (
    "Hermes 自己的会话记录、日志、会话数据库（原委不碰）",
    "用户自己导出的文件（yuanwei export --out 的输出、record --input 的 JSON）",
    "已经发给模型服务的内容（整理请求、回答请求、助手已经看到的内容）",
    "磁盘介质上已经释放的块、文件系统快照、Time Machine 等系统备份、交换分区、内存转储",
    "别的进程（Hermes 以外的 MCP 服务、后台整理进程）自己内存里已经读出来的东西",
    "终端滚屏、宿主界面上已经显示过的文字",
    "有本机 shell 权限的 agent：能自己跑删除命令、能直接读写库文件（对话里「agent 自己删不了」只指对话里那组工具的约定）",
)
