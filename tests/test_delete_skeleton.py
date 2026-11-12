"""删除与密钥过滤的测试骨架（交回点甲：只有清单，没有实现；交回点乙把每一行换成真测试，行数不能少，名字就是将来的测试名）。

每一行：(组, 名字, 标签, 要断言什么)。标签是 Codex 核对的编号（R1–R6）或施工单的条目（SPEC）；现在跑的「骨架自检」只核：名字不重、六项 R 都有行、
施工单点名的几类（三个交错点、四个序号例子、重启恢复、十二个放回缺陷）都有行。真正的骨架行整份跳过（函数体是 pytest.fail，写完实现后不会自己变绿）。
"""
import pytest

G_FILTER, G_DELETE, G_VERSION, G_BARRIER, G_RECOVER, G_CHAT, G_CLI, G_MIGRATE, G_RULES, G_MUTATE = (
    "过滤", "删除核心", "版本与序号", "屏障与交错", "完成与恢复", "对话里删", "命令", "迁移与回放", "常驻规矩", "放回缺陷")

S = []


def s(group, name, tags, what):
    S.append((group, name, tags, what))


# ---- 一、正文密钥过滤（R6）
s(G_FILTER, "test_record_filters_user_and_assistant_text_before_fingerprint_slicing_and_pending", "R6", "用户和助手的话都在 record 里先过滤，再算指纹、切片、写待补；原话表、锚点、待补行里只有过滤后的文字")
s(G_FILTER, "test_fake_secret_not_in_raw_bytes_of_db_wal_pending_claim_review_or_upstream_request", "R6,SPEC", "带假密钥的消息记进去（直写和排队两条路）：库文件、WAL、待补、领取、待核的原始字节里找不到；发给假上游的整理请求里也找不到")
s(G_FILTER, "test_resend_same_message_same_fingerprint_no_conflict", "R6,SPEC", "同一条消息重送：不冲突、指纹不变")
s(G_FILTER, "test_resend_after_rules_upgrade_is_duplicate_not_conflict", "R6", "库里是 v1 规则过滤的、重送的原文按 v2 过滤：用「新规则过滤已存文字」判等，算重送，不算冲突；不靠保留明文")
s(G_FILTER, "test_status_counts_by_kind_and_pending_vs_stored_are_separate", "R6,SPEC", "status 里挡掉的处数按类型分；待补里的和已入库的分开数")
s(G_FILTER, "test_count_is_merged_intervals_per_message_and_not_counted_again_on_resend", "R6", "重送不重复计；同一个区间被几条规则命中只算一次；同一个值出现两次算两处")
s(G_FILTER, "test_redacted_message_is_marked_with_count_in_read_output_and_unredacted_output_is_byte_identical", "SPEC", "挡掉过的原话在读取输出里标「这条挡掉过 N 处」；没挡过的消息输出一字节不变")
s(G_FILTER, "test_filtered_text_is_the_only_stored_text_and_anchor_offsets_match_it", "R6", "引文偏移、锚点起止都按过滤后的正文")
s(G_FILTER, "test_filter_error_fails_closed_nothing_stored", "R6", "过滤本身出错：这条不入库、不进待补，回执报错（不放过）")
s(G_FILTER, "test_query_with_fake_secret_filtered_before_vector_request_cursor_and_any_persistent_record", "R6", "recall 的问题带假密钥：发给假向量服务的请求、游标里存的 query、任何持久记录里都找不到原串")
s(G_FILTER, "test_recall_keywords_filtered_like_query", "R6", "补充关键词同样过滤")
s(G_FILTER, "test_note_automatic_query_filtered", "R6", "便条（自动查询）的查询文字同样过滤")
s(G_FILTER, "test_query_without_secret_is_unchanged_so_replays_stay_byte_identical", "R6,SPEC", "没有命中的查询一字不变")
s(G_FILTER, "test_legacy_unfiltered_pending_line_is_filtered_before_flush_inserts_it", "R6", "旧待补行（没有 rv 键）补进库之前再过一遍过滤，库里是遮掉的")
s(G_FILTER, "test_legacy_claim_and_review_lines_are_never_inserted_raw", "R6", "旧领取批次、待核里的未过滤行不会原样入库")
s(G_FILTER, "test_secrets_scan_reports_counts_only_no_content_and_does_not_modify_the_db", "R6,SPEC", "secrets scan：只报数，不打印内容，不改库（库文件字节不变）")
s(G_FILTER, "test_secrets_scan_reports_scope_and_errors_unreadable_is_not_zero", "R6", "scan 报扫描的范围和出错的地方；打不开的库、坏行、解不开的 base64 不能算成零，退出码非零")
s(G_FILTER, "test_production_filter_passes_the_whole_rules_table_without_importing_tests", "R6,SPEC", "yuanwei.redact 过 tests/secret_rules_table.py 的每一行（test_secret_rules_table.py 里的金标组），源码不 import tests/")
s(G_FILTER, "test_rules_version_is_recorded_per_message_and_in_status", "R6", "规则版本记在每条消息上，status 里能看到")

# ---- 二、删除核心（R1、R2）
s(G_DELETE, "test_unique_marker_not_in_db_wal_pending_claim_review_tmp_bytes_after_delete", "R1,R4,SPEC", "独一无二的标记串当原话：删完，库文件、WAL、待补、领取、待核、.tmp 的原始字节里都找不到")
s(G_DELETE, "test_decoded_fields_are_checked_not_only_raw_bytes", "R1", "标记只在另一会话的判断请求和主线说明里、只在 .review 的 line_b64 里、只在遗留 .tmp 里：解码后的字段核过，不只搜明文")
s(G_DELETE, "test_recall_get_messages_attachments_note_directory_export_cannot_return_deleted", "SPEC", "recall、get_messages、attachments、便条、目录、导出都拿不到被删内容")
s(G_DELETE, "test_only_that_window_is_deleted_other_windows_and_parent_window_stay_readable", "SPEC", "别的对话、别的窗口、分叉前父窗口的消息都还在、还能读到")
s(G_DELETE, "test_fork_child_delete_keeps_parent_history_and_parent_delete_removes_parent_sessions_only", "SPEC", "删分叉出来的窗口只删它自己的会话号；删父窗口删父窗口的会话号")
s(G_DELETE, "test_compressed_session_ids_delete_as_one_conversation", "SPEC", "压缩换过会话号的一段对话整段删干净")
s(G_DELETE, "test_delete_covers_every_workspace_in_the_same_db", "SPEC", "同一个库里别的工作区里这个窗口记下的话也删")
s(G_DELETE, "test_partially_deleted_occasion_title_and_summary_become_tombstone_rest_stays_readable", "SPEC,R1", "一回里只删了一部分：标题、摘要每个版本都换成墓碑文案，剩下的原话还能读到")
s(G_DELETE, "test_occasion_fully_deleted_is_removed_and_thread_without_occasions_is_removed", "SPEC", "一回里的消息全被删：整回删；主线下一回都不剩：主线名、说明一起删")
s(G_DELETE, "test_every_text_field_of_every_org_table_and_every_version_is_covered", "R1", "org_rows 的每个文字字段、每个历史版本：被删对象删，受污染的换占位，清单（delete_trace_inventory）里写的每一处都被动到")
s(G_DELETE, "test_other_step_whose_request_showed_a_deleted_card_gets_placeholders_not_model_rewrites", "R1", "别的步的请求里展示过被删内容派生的卡片：它写的理由、名称、摘要清成占位；全程没有调模型")
s(G_DELETE, "test_taint_closure_is_transitive_through_later_steps_that_saw_cleared_objects", "R1", "污染沿「看过」传下去到不动点")
s(G_DELETE, "test_clean_retained_step_text_is_untouched", "R1", "没看过任何受污染对象的留下的步：文字一字不动")
s(G_DELETE, "test_pending_retained_step_judgments_are_deleted_and_the_step_reruns_on_the_post_delete_state", "R1", "还没结束的留下的步：判断记录整行删，之后按删除后的状态重新整理，卡片里没有被删的东西")
s(G_DELETE, "test_judgments_of_other_steps_include_failed_archives_and_feedback_and_errors", "R1", "失败归档 q1~f…、重问反馈、校验理由、错误文字都清")
s(G_DELETE, "test_steps_last_error_and_abandon_reason_cleared_only_when_tainted", "R1", "steps.last_error、操作收据 reason：受污染才清")
s(G_DELETE, "test_vectors_removed_only_when_no_remaining_anchor_or_query_uses_them_across_all_workspaces", "SPEC", "向量按全库剩余引用核；别处合法保留同一句话时向量留着；孤儿（含查询向量）删")
s(G_DELETE, "test_cursors_of_affected_workspaces_are_void_and_old_cursor_says_history_deleted", "R1,R2", "受影响工作区的全部游标作废（含查询原文），旧游标报「历史已删除」")
s(G_DELETE, "test_input_hash_columns_are_scrubbed_or_pseudonymized_for_deleted_turns", "R1", "inputs.fp、window_inputs.fp 清空；input_key、event_key 里的哈希换编号；窗口序号不变")
s(G_DELETE, "test_read_log_spans_and_fingerprints_of_deleted_messages_are_scrubbed", "R1", "提到被删消息编号的读取登记，spans 和 fingerprint 清掉；解析不了的行 detail 换成 {}")
s(G_DELETE, "test_receipt_and_cli_report_which_other_summaries_were_cleared_with_counts", "R1", "回执和预览里报清了几个别的回的摘要、几条主线名/说明、几步的理由（只给计数）")
s(G_DELETE, "test_tombstone_and_deletions_rows_contain_no_deleted_content", "SPEC", "墓碑、deletions、del_scope、del_keys、no_record_turns 里没有被删的内容（只有编号、计数、状态码、哈希）")
s(G_DELETE, "test_window_state_bindings_and_cost_ledger_are_kept_and_unchanged", "R2", "窗口开关状态、绑定、成本账不变；删除不借用窗口控制版本")
s(G_DELETE, "test_only_one_module_mutates_history_static_guard", "R2", "静态守卫（tests/test_delete_static.py 现在就跑）：只有 deletion.py 能删原话、整理结果")

# ---- 三、版本、序号、防重放（R2）
s(G_VERSION, "test_old_cut_is_rejected_with_history_deleted_not_silently_replaced", "R2", "删除后旧截点（新旧两种格式）、旧游标、返回票据：明确拒绝「历史已删除，请重新查询」，不静默换成新截点")
s(G_VERSION, "test_old_cut_rejected_in_a_fresh_process", "R2", "新开进程重取旧截点：同样拒绝")
s(G_VERSION, "test_cut_without_any_deletion_is_byte_identical_no_del_ver_key", "R2,SPEC", "没发生过删除的工作区：截点字典、游标、读取输出和现在一字节不差")
s(G_VERSION, "test_snapshot_cache_keyed_by_del_ver_and_never_serves_pre_delete_snapshot", "R2,R3", "快照缓存键带删除版本；删除后不会命中旧快照")
s(G_VERSION, "test_last_seq_org_seq_vclock_never_regress_and_org_seq_advances_once_per_affected_workspace", "R2", "last_seq、vclock 不回退；org_seq 只增（受影响工作区加一）")
s(G_VERSION, "test_step_thread_occasion_gap_numbers_are_not_reused_after_deleting_the_tail", "R2", "删掉尾部的步、主线、回、缺口，新建的编号接在高水位后面，不复用")
s(G_VERSION, "test_delete_last_step_then_new_step_does_not_collide_or_get_stuck_or_reorganize_old_events", "R2", "删最后一步后，新一步不撞号、不卡住、不把旧事件重新整理")
s(G_VERSION, "test_delete_unfinished_middle_step_does_not_block_later_steps_prefix_rule_with_tombstones", "R2", "删掉前面还没结束的步：步序检查把已删步号算作已结束（前缀规则），后面的步照常整理")
s(G_VERSION, "test_delete_all_data_then_keep_recording_and_organizing", "R2", "删光所有数据后继续记、继续整理")
s(G_VERSION, "test_old_host_message_resend_after_delete_is_blocked_by_key_tombstone", "R2", "旧宿主消息重送（同一个键）：被键哈希墓碑挡住，不再入库")
s(G_VERSION, "test_same_session_new_message_after_delete_is_recorded_normally", "R3,SPEC", "同一个会话号下删除之后新说的话照常记")
s(G_VERSION, "test_in_flight_org_commit_built_before_delete_fails_stale_base", "R2,R3", "删除前构造的整理变更集，删除后提交报 StaleBase / 删除中断，不入库")

# ---- 四、屏障与交错（R3）
s(G_BARRIER, "test_interleave_pending_batch_read_into_memory_then_delete_then_flush_inserts_nothing", "R3", "交错点一：待补已读入内存、删除完成、放行旧补写：一条都插不进去，不恢复正文")
s(G_BARRIER, "test_interleave_model_returned_before_judgment_saved_then_delete_then_release_restores_nothing", "R3", "交错点二：模型已返回、尚未落判断记录；删除完成后放行：判断记录、整理提交都不写回，步回到 sealed 或被删")
s(G_BARRIER, "test_interleave_read_assembled_not_returned_then_delete_then_release_returns_history_deleted", "R3", "交错点三：读取已组装、尚未返回；删除完成后放行：不交旧结果，报「历史已删除」")
s(G_BARRIER, "test_late_assistant_reply_of_a_turn_started_before_delete_is_blocked_by_capture", "R3", "删除前开始的轮次的迟到助手回复：按捕获序号挡住")
s(G_BARRIER, "test_late_callback_never_gets_a_fresh_capture_number", "R3", "迟到的回调带原来的捕获序号，不在回调执行时补一个新的")
s(G_BARRIER, "test_message_started_before_delete_but_not_yet_inserted_is_in_scope", "R3,SPEC", "删前已经开始、还没入库的消息也在删除范围")
s(G_BARRIER, "test_barrier_checked_in_every_listed_place", "R3", "屏障清单里的每个检查位置（设计文档 3.6.4）各一条断言：add_message、待补追加与补写、save_judgment、update_judgment、bind_step、commit、end_step、读取快照、finish_read、export")
s(G_BARRIER, "test_other_connection_and_background_note_thread_check_deletion_version_before_return", "R3", "别的连接、后台便条和目录取材线程：返回前核删除版本，不对就丢弃")
s(G_BARRIER, "test_every_reader_and_memory_instance_in_the_process_drops_cached_snapshots", "R3", "同进程所有 Memory、Reader 实例清缓存")
s(G_BARRIER, "test_other_session_org_task_using_deleted_history_is_voided_not_only_the_target_step", "R3", "别的会话正在用被删历史整理的旧任务也作废")
s(G_BARRIER, "test_new_turn_after_delete_is_recorded", "R3,SPEC", "删后新一轮成功记入")
s(G_BARRIER, "test_lock_order_has_no_deadlock_under_concurrent_record_flush_run_and_delete", "R3", "并发的 record、补写、整理、删除：按 L1→L3→L2→L4 的顺序，不死锁（带超时的线程测试）")
s(G_BARRIER, "test_deleter_never_takes_the_run_lock", "R3", "删除不等整理进程：在途整理被屏障作废，而不是被删除阻塞")

# ---- 五、完成与清理中、重启恢复（R4）
s(G_RECOVER, "test_logical_revoke_and_task_registration_are_one_transaction_fault_in_between_leaves_nothing", "R4", "在登记前、登记后、提交前后注入故障：要么什么都没发生，要么撤销和任务一起在；不停在一半")
s(G_RECOVER, "test_crash_after_db_commit_before_files_resume_finishes_without_resurrecting_anything", "R4", "数据库逻辑删除后中断：重启先做完，不复活、不假报成功，其他对话仍保留")
s(G_RECOVER, "test_crash_before_and_after_each_file_replace_resume_finishes", "R4", "文件替换前后各中断一次：重启恢复；残留行不被补进库")
s(G_RECOVER, "test_checkpoint_busy_reports_cleaning_and_never_complete", "R4", "checkpoint(TRUNCATE) 返回 busy：状态是清理中，状态码固定，不报已删干净")
s(G_RECOVER, "test_not_enough_disk_space_reports_cleaning_with_code_and_changes_nothing_else", "R4", "磁盘空间不够（注入 disk_free）：清理中 + E_NO_SPACE，已撤销的不复活")
s(G_RECOVER, "test_vacuum_failure_reports_cleaning_with_code", "R4", "VACUUM 失败：清理中 + 状态码")
s(G_RECOVER, "test_secure_delete_is_verified_on_and_refuses_to_start_when_it_cannot_be", "R4", "secure_delete 先设后核实读回为 1；核实不了：整个删除不开始，不留半状态")
s(G_RECOVER, "test_complete_requires_all_five_conditions", "R4", "完成条件：受控入口不可读、无迟到写回（屏障在）、受管文件清理成功、WAL 为 0 字节、没有未完成任务；缺一不报完成")
s(G_RECOVER, "test_receipt_text_is_cleaning_until_complete_and_never_says_clean", "R4", "回执措辞：清理中时不出现「已删干净」")
s(G_RECOVER, "test_memory_open_resumes_unfinished_deletion_before_reads_and_writes_open", "R4", "打开 Memory 时先恢复未完成的删除；恢复不了的工作区读被拒（deletion_incomplete）、写排队")
s(G_RECOVER, "test_recovery_log_and_state_codes_contain_no_old_text", "R4", "恢复日志、状态码、计数里没有旧正文")
s(G_RECOVER, "test_canary_self_check_after_vacuum_finds_planted_residue_and_blocks_complete", "R4", "金丝雀自检：删前取的片段，清完在受管文件里再扫一遍；往 .tmp 里塞一个残留，完成被挡住并报计数")
s(G_RECOVER, "test_unattributable_pending_lines_are_dropped_and_counted_not_silently_kept", "R1,R4", "无法解析、无法归属的待补/待核行：不当作「没有目标内容」跳过；按设计文档的决定清掉并计数")
s(G_RECOVER, "test_boundary_statement_present_in_receipt_docs_not_claiming_forensic_erasure", "R4", "回执和文档写明边界：磁盘介质、备份、Hermes 会话、导出、已发给模型的内容不归原委管；不出现无范围的「彻底删除」")

# ---- 六、对话里删（R5）
s(G_CHAT, "test_agent_requests_then_user_confirm_deletes_only_after_exact_confirm", "R5,SPEC", "agent 申请（不带参数）以后，用户回正好「确认删除」才删")
s(G_CHAT, "test_non_confirm_inputs_void_the_pending_and_delete_nothing_parametrized_by_confirm_table", "R5,SPEC", "tests/delete_confirm_table.py 里每个不算确认的句子（含禁读判表的 98 句）都不删、待确认作废")
s(G_CHAT, "test_tool_param_tool_result_attachment_subagent_cron_flush_cannot_request_or_confirm", "R5,SPEC", "工具参数、工具结果、附件、子代理、定时任务、记忆刷新：确认不了，也申请不了")
s(G_CHAT, "test_open_and_delete_pendings_are_mutually_exclusive_and_words_do_not_cross", "R5", "开启待确认和删除待确认互相排他；「确认开启」打不开删除，「确认删除」打不开开启")
s(G_CHAT, "test_confirmation_consumption_and_task_registration_commit_together", "R5,R4", "确认被消费和删除任务登记在同一个事务里；中间注入故障：两者要么都在要么都不在")
s(G_CHAT, "test_repeat_confirm_returns_same_result_and_never_deletes_twice", "R5", "重复确认只返回同一个结果，不删第二次；重启后重复也一样")
s(G_CHAT, "test_pending_is_bound_to_window_db_identity_request_input_and_op_id", "R5", "申请绑定窗口、库身份、申请时的输入序号、待确认操作号；别的窗口、别的库、过期序号都消费不了")
s(G_CHAT, "test_pending_after_restart_is_valid_only_for_the_next_input", "R5,SPEC", "重启以后，待确认仍然只对下一条输入有效")
s(G_CHAT, "test_closed_window_can_still_request_and_confirm_delete_without_reading_history", "SPEC", "窗口关着也能申请和确认；删除不读历史、不触发整理、不调模型")
s(G_CHAT, "test_confirm_turn_is_not_recorded_user_and_assistant_across_restart_duplicate_event_compression_and_late_reply", "R3,R5,SPEC", "确认那一轮（用户那句和助手回复）不记；重启、重复事件、压缩换号、迟到助手回复都盖住")
s(G_CHAT, "test_next_real_turn_after_the_confirm_turn_is_recorded_normally", "R3,SPEC", "下一条真正的新输入照常记，不是把会话号永久封死")
s(G_CHAT, "test_prompt_states_scope_and_counts_but_never_text", "R5", "提示写明范围：窗口压缩前后的记录、涉及几个工作区、截到这次确认之前、父窗口不删、之后照常记；只有计数，没有原话")
s(G_CHAT, "test_delete_prompt_and_receipts_are_not_deferred_to_the_next_prefetch", "R5", "删除的提示和回执不走「等下一次预取才送」：工具结果当场带回；确认轮的回执在同一轮的预取里送；预取没被调用时 window_state 工具能查到")
s(G_CHAT, "test_receipt_says_what_is_not_deleted_assistant_already_saw_hermes_own_transcript", "SPEC", "回执固定文案：这一轮助手已经看到的、Hermes 自己的会话记录，原委删不掉")
s(G_CHAT, "test_agent_without_chat_authority_but_with_shell_is_documented_as_able_to_bypass", "R5", "文档和提示里不出现「agent 绝对删不了」；写明只指对话里这组工具的约定")
s(G_CHAT, "test_resident_rule_sentence_in_tools_note_directory_after_window_rule_before_reading_tips", "SPEC", "常驻规矩加的那一句：三种模式都带，位置在 WINDOW_RULE 后、READING_TIPS 前；READING_TIPS 一字不改仍在最后")

# ---- 七、命令（R5）
s(G_CLI, "test_conversations_list_shows_id_host_dates_count_and_head_of_first_user_message", "SPEC", "list：编号、宿主、起止日期、条数、第一句用户的话的开头十几个字（过滤后的）")
s(G_CLI, "test_preview_does_not_write_spawn_organize_or_call_a_model", "R5", "不带 --yes：只列要删什么；库文件字节不变、不拉起整理、假提供方一次都没被调")
s(G_CLI, "test_delete_without_yes_deletes_nothing_with_yes_deletes", "SPEC", "不带 --yes 不删；带 --yes 才删，和 setup 一样")
s(G_CLI, "test_unbound_legacy_conversations_are_listed_by_exact_host_session_branch_no_parent_guess", "R5", "没绑过窗口的旧记录：命令只按精确的（宿主、会话、分支）删，不猜父子关系")
s(G_CLI, "test_cli_and_chat_use_the_same_conversation_ids", "R5", "命令和对话工具用同一套逻辑窗口编号")
s(G_CLI, "test_conversations_status_and_resume_report_codes_and_counts_only", "R4", "status／resume：只给状态码和计数")

# ---- 八、迁移、回放
s(G_MIGRATE, "test_v11_opens_any_older_db_v1_to_v10", "SPEC", "v1–v10 的旧库各一条，都能打开升到 v11，老数据读得出")
s(G_MIGRATE, "test_hwm_columns_backfilled_and_captures_seeded", "R2", "step_hwm 回填成现存最大 k；captures 种 0；store_meta 里有库身份")
s(G_MIGRATE, "test_replay_v1_v2_v3_v1b_byte_identical_when_no_deletion_and_no_filter_hit", "R2,SPEC", "第一段回放 V1–V3、V1b：没发生删除、过滤也没命中时逐字节一致")
s(G_MIGRATE, "test_full_existing_suite_still_passes", "SPEC", "现有 1129 条全过（只有为新常驻规矩改的指纹断言）")

# ---- 九、放回缺陷（最多 12 个，在副本上跑；每个写明哪条测试必须红）
s(G_MUTATE, "mutation_01_barrier_check_removed_from_add_message", "R3", "去掉 add_message 里的屏障检查 → 交错点一、迟到助手回复、旧宿主重送必须红")
s(G_MUTATE, "mutation_02_capture_taken_at_insert_time_instead_of_event_entry", "R3", "捕获序号改成入库时才取 → 删前已开始的消息、迟到回调的测试必须红")
s(G_MUTATE, "mutation_03_del_ver_not_checked_before_return_in_finish_read", "R3", "去掉 finish_read 的删除版本核对 → 交错点三必须红")
s(G_MUTATE, "mutation_04_cut_without_del_ver_accepted_after_deletion", "R2", "删除后接受不带 del_ver 的旧截点 → 旧截点拒绝、新进程拒绝必须红")
s(G_MUTATE, "mutation_05_step_number_taken_as_existing_max_plus_one", "R2", "步号改回「现存最大号加一」→ 不复用、删最后一步后新步的测试必须红")
s(G_MUTATE, "mutation_06_expected_k_uses_max_ended_without_tombstones", "R2", "步序检查不算已删步号 → 删中间未结束步后卡住的测试必须红")
s(G_MUTATE, "mutation_07_taint_closure_stops_at_first_level", "R1", "污染只做一层不做传递 → 传递闭包的测试必须红")
s(G_MUTATE, "mutation_08_other_steps_judgments_not_scrubbed", "R1", "不清别的步的判断记录 → 「标记只在另一会话的判断请求里」必须红")
s(G_MUTATE, "mutation_09_vacuum_and_checkpoint_skipped_but_reports_complete", "R4", "跳过 VACUUM/checkpoint 仍报完成 → 完成条件、WAL 为 0、金丝雀必须红")
s(G_MUTATE, "mutation_10_delete_confirm_checks_first_line_only", "R5", "确认删除改成只看第一行 → 判表里的第二行类句子、全流程的非确认必须红")
s(G_MUTATE, "mutation_11_confirm_turn_not_marked_durably_only_in_memory", "R3,R5", "确认轮的不记标记只放内存 → 重启、压缩换号、迟到助手回复必须红")
s(G_MUTATE, "mutation_12_query_not_filtered_before_cursor_persist", "R6", "查询存游标前不过滤 → 查询路径的测试必须红")


# ================================================================ 骨架自检（现在就跑）
def test_skeleton_names_are_unique_and_every_row_has_text():
    names = [n for _g, n, _t, _w in S]
    assert len(names) == len(set(names))
    for g, n, t, w in S:
        assert n.startswith(("test_", "mutation_")) and len(w) >= 8 and t, n


def test_every_codex_item_r1_to_r6_has_skeleton_rows():
    for r in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert sum(1 for _g, _n, t, _w in S if r in t.split(",")) >= 4, r


def test_named_scenarios_from_the_work_order_are_all_present():
    names = " ".join(n for _g, n, _t, _w in S)
    for key in ("interleave_pending_batch", "interleave_model_returned", "interleave_read_assembled", "delete_last_step", "delete_all_data", "fresh_process", "old_host_message_resend",
                "crash_after_db_commit", "checkpoint_busy", "not_enough_disk_space", "unattributable", "v11_opens_any_older_db", "replay_v1_v2_v3_v1b", "decoded_fields"):
        assert key in names, key


def test_at_most_twelve_mutations_and_every_group_present():
    assert sum(1 for g, *_ in S if g == G_MUTATE) <= 12
    assert {g for g, *_ in S} >= {G_FILTER, G_DELETE, G_VERSION, G_BARRIER, G_RECOVER, G_CHAT, G_CLI, G_MIGRATE, G_MUTATE}


def test_the_work_order_test_list_is_covered():
    """施工单「交回点乙：测试至少覆盖」的每个点，在骨架里至少有一行（按关键词核）。"""
    text = " ".join(g + " " + n + " " + w for g, n, _t, w in S)
    for kw in ("原始字节", "重送", "status", "scan", "recall", "别的窗口", "墓碑", "压缩", "确认", "子代理", "确认那一轮", "重启", "迟到", "新说的话照常记",
               "v1–v10", "回放", "放回缺陷"):
        assert kw in text, kw


def test_row_count_is_not_tiny():
    assert len(S) >= 100


# ================================================================ 乙已经转成真测试的行：核「映射的测试函数确实存在」
import os
import re

from delete_skeleton_map import MAP

_HERE = os.path.dirname(__file__)


def test_every_mapped_skeleton_row_points_at_an_existing_real_test():
    names = {n for _g, n, _t, _w in S}
    assert set(MAP) <= names
    for row, target in MAP.items():
        f, fn = target.split("::")
        src = open(os.path.join(_HERE, f), encoding="utf-8").read()
        assert re.search(r"^def %s\(" % re.escape(fn), src, re.M), (row, target)


UNMAPPED = [(g, n, t, w) for g, n, t, w in S if n not in MAP and g != G_MUTATE]


# ================================================================ 还没转成真测试的骨架行：整份跳过，函数体是 pytest.fail
@pytest.mark.skip(reason="还没转成真测试的骨架行（交回报告里列出）")
@pytest.mark.parametrize("group,name,tags,what", UNMAPPED, ids=[n for _g, n, _t, _w in UNMAPPED])
def test_skeleton_row(group, name, tags, what):
    pytest.fail("骨架：%s｜%s" % (name, what))
