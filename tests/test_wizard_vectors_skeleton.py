"""安装向导、预设与供应商、密钥、本机向量的测试骨架（交回点甲：只有清单，没有实现；交回点乙把每一行换成真测试，行数不能少，名字就是将来的测试名）。

每一行：(组, 名字, 标签, 要断言什么)。标签 SPEC＝施工单条目，NEW＝11-11 加的三样。现在跑的「骨架自检」只核：名字不重、施工单「测试至少覆盖」的每一点和新增三样都有行、
放回缺陷不超过 12 个。真正的骨架行整份跳过（函数体是 pytest.fail，写完实现后不会自己变绿）。设计见 圆桌/产品打包/设计-安装向导与本机向量-2026-11-11.md。
"""
import pytest

(G_WIZ, G_ENV, G_PRESET, G_PROV, G_KEY, G_COST, G_VEC, G_MISS, G_BACK, G_IDENT, G_SAME, G_DOCS, G_MUTATE) = (
    "向导", "环境检查", "预设与参数", "供应商与参数", "密钥", "费用账", "向量检测与核验", "缺向量", "后台补算", "编码身份", "逐字节不变", "文档与助手说明", "放回缺陷")

S = []


def s(group, name, tags, what):
    S.append((group, name, tags, what))


# ---- 一、向导（SPEC）
s(G_WIZ, "test_wizard_scripted_answers_and_all_flags_change_exactly_the_same_files", "SPEC", "一问一答（模拟输入）和全参数写法，最后写出的文件、改的那一行设置、配置内容逐字节相同")
s(G_WIZ, "test_without_yes_nothing_changes_only_the_plan_is_printed", "SPEC", "参数写法不加 --yes：家目录、库、配置、密钥目录的字节都不变，不下载，不调假命令的下载")
s(G_WIZ, "test_cancel_at_every_step_changes_nothing", "SPEC", "每一步输入 q、Ctrl-C、EOF：什么都不改、不下载、不存密钥；退出码 1，说「什么都没改」")
s(G_WIZ, "test_legacy_setup_hermes_tests_pass_unchanged_and_plan_output_has_no_new_keys", "SPEC", "现有的 setup hermes 测试原样通过；不带新参数时输出的字段一个不多")
s(G_WIZ, "test_setup_without_host_in_non_tty_refuses_with_exit_2", "SPEC", "没给助手名又不在终端里：拒绝，退出码 2，提示用参数")
s(G_WIZ, "test_future_hosts_claude_code_codex_pi_are_listed_but_not_selectable", "SPEC", "第 2 步显示「后续更新」；--host claude-code／codex／pi 退出码 2")
s(G_WIZ, "test_plan_lists_every_choice_provider_model_params_key_source_vectors_files_and_the_one_setting_line", "SPEC,NEW", "计划里列：供应商、模型、参数、标签、费用口径、密钥来源、向量服务和模型、是否下载、库位置、文件、memory.provider 一行；全部来自选择，没有漏项")
s(G_WIZ, "test_execution_order_download_verify_key_install_config_and_stops_at_first_failure", "SPEC", "执行顺序固定；①②失败时配置、密钥、memory.provider 一个字都没写")
s(G_WIZ, "test_wizard_prints_restart_hint_and_does_not_restart_hermes", "SPEC", "装完提示用户自己重启，并说怎么看状态；假 hermes 命令没被调用过重启")
s(G_WIZ, "test_status_hermes_shows_vectors_and_provider_sections", "SPEC,NEW", "status hermes 里有「向量」「整理供应商」两段；供应商段带标签、密钥来源（不含值）、费用口径")
s(G_WIZ, "test_user_given_config_toml_is_never_modified_lines_are_printed_instead", "SPEC", "用户用 --config 给了自己的 TOML：不改，只打印要加的几行")
s(G_WIZ, "test_generated_toml_roundtrips_through_tomllib_and_config_loads_it", "SPEC", "手写的 yuanwei.toml 用 tomllib 读回来，Config.load 能用，值和选择一致")
s(G_WIZ, "test_uninstall_removes_toml_when_unmodified_and_keeps_key_file_and_db", "SPEC,NEW", "卸载：没改过的 yuanwei.toml 删；密钥文件、记忆库不删，提示 yuanwei key delete")

# ---- 二、环境检查（SPEC）
s(G_ENV, "test_python_too_old_gives_clear_message_and_stops", "SPEC", "假的 Python 版本低于 3.11：清楚的提示，退出码 2，什么都不改")
s(G_ENV, "test_hermes_not_found_gives_clear_message_and_stops", "SPEC", "找不到 Hermes：提示用 --hermes-home，退出码 2")
s(G_ENV, "test_hermes_compat_drift_is_listed_and_flagged_in_plan", "SPEC", "hermes_compat 报有几项变了：逐项列出，计划里标出来")
s(G_ENV, "test_missing_key_gives_clear_message_and_three_choices", "SPEC,NEW", "没找到密钥：说明名字、没有会怎样、现在输入／自己去设／以后再说")
s(G_ENV, "test_fake_key_value_never_appears_in_any_output_log_file_or_exception", "SPEC,NEW", "拿一串假密钥放在 .env 里、环境里、密钥文件里：stdout、stderr、日志、status、配置、安装记录、异常文字里找不到值（密钥文件本身除外）")
s(G_ENV, "test_dotenv_scan_returns_only_a_bool_and_does_not_keep_values", "SPEC", "扫 Hermes 的 .env 只返回「有／没有」；返回值和对象里没有值")
s(G_ENV, "test_own_environment_missing_but_dotenv_has_name_is_reported_as_found_not_unset", "SPEC", "向导自己的环境里没有、.env 里有这个名字：报「Hermes 的设置文件里有」，不报「没设」（助手终端工具的环境会剔除供应商密钥）")
s(G_ENV, "test_unreadable_dotenv_or_external_secret_source_is_reported_as_not_checked_not_as_unset", "SPEC", "读不了 .env、外部密钥源：写「没核到」，不写「没设」，不当成没问题")
s(G_ENV, "test_numpy_missing_in_hermes_python_is_reported_before_enabling_vectors", "SPEC", "假的 Hermes Python 里没有 NumPy：第 6 步说明并给出命令；找不到 Hermes 的 Python 写「没核到」")

# ---- 三、预设与参数（NEW）
s(G_PRESET, "test_four_presets_exist_as_data_with_labels", "NEW", "预设表有 recommended、light、custom、local，每行有名称、标签、provider、vectors；加一行假预设不改代码就出现在向导里")
s(G_PRESET, "test_preset_flag_selects_defaults_and_explicit_flags_override_them", "NEW", "--preset 给默认值；显式参数盖过它")
s(G_PRESET, "test_custom_preset_without_provider_flags_reports_exactly_what_is_missing", "NEW", "参数写法下 custom 缺什么就列什么，退出码 2，不猜")
s(G_PRESET, "test_light_preset_downloads_nothing_and_sets_keyword_retrieval", "NEW", "light：不下载、不调任何向量命令，retrieval=keyword")
s(G_PRESET, "test_recommended_preset_requires_vectors_choice_in_non_interactive_mode", "NEW", "不问话写法下 recommended 的 vectors=auto 必须同时给 --vectors，否则报错")
s(G_PRESET, "test_local_preset_allows_only_loopback_addresses_for_llm_and_embedding", "NEW", "local：整理和向量地址只许 127.0.0.1／localhost／[::1]；别的拒绝")
s(G_PRESET, "test_vectors_none_writes_no_embedding_section", "SPEC", "--vectors none：配置里没有向量段，retrieval=keyword")

# ---- 四、供应商与参数（NEW）
s(G_PROV, "test_provider_table_rows_have_name_url_model_params_label", "NEW", "表里每行都有名称、地址、模型、参数、标签；标签只有 测过／试过／没测过／实验")
s(G_PROV, "test_deepseek_request_bytes_identical_to_current_write_request_bytes", "NEW,SPEC", "deepseek 一行发出的请求体和现在 write_request_bytes 逐字节相同")
s(G_PROV, "test_luna_row_sends_reasoning_effort_and_max_completion_tokens_and_never_temperature", "NEW", "gpt-6-luna 一行：有 reasoning_effort=high、max_completion_tokens=65536；请求体里没有 temperature、max_tokens")
s(G_PROV, "test_only_listed_params_are_sent_nothing_else", "NEW", "表里没写的参数一个都不发")
s(G_PROV, "test_untested_rows_are_labeled_and_show_the_not_tested_sentence", "NEW", "没测过的行：向导里带「整理的提示词只在 DeepSeek 上调过」；地址未核的行带「地址未核」")
s(G_PROV, "test_manual_provider_asks_url_model_and_params_and_validates_https_for_non_local", "NEW", "手填：非本机地址必须 https；本机可以 http；模型名不能空")
s(G_PROV, "test_provider_param_override_parses_json_values_and_rejects_garbage", "NEW", "--provider-param k=v 的 v 按 JSON 读；读不了报错")
s(G_PROV, "test_non_loopback_hostname_resolving_to_a_lan_address_is_not_local", "NEW", "指向局域网的别名不算本机")

# ---- 五、密钥（NEW）
s(G_KEY, "test_key_set_refuses_when_stdin_or_stdout_is_not_a_terminal", "NEW", "不是终端：拒绝，退出码 2，提示「请在你自己的终端里运行」；密钥文件没有被创建")
s(G_KEY, "test_key_input_is_not_echoed_uses_getpass_like_input", "NEW", "输入走不回显的接口（假的 getpass），不是 input()；输出里没有输入的值")
s(G_KEY, "test_key_file_is_0600_in_0700_dir_and_written_atomically", "NEW", "文件权限 0600、目录 0700；先写临时文件再改名；中途失败不留半个文件")
s(G_KEY, "test_key_file_with_loose_permissions_or_foreign_owner_is_refused_and_status_says_so", "NEW", "权限不对或属主不是自己：不使用，status 说「密钥文件权限不对」，不显示值")
s(G_KEY, "test_key_value_validation_rejects_whitespace_non_ascii_too_short_without_echo", "NEW", "含空白、非 ASCII、太短：拒绝，只说原因类别，不回显任何字符")
s(G_KEY, "test_key_confirmation_mismatch_retries_up_to_three_times_then_cancels", "NEW", "两次输入不一致：重来，最多 3 次，之后取消且什么都没写")
s(G_KEY, "test_lookup_order_file_then_env_and_status_reports_source_only", "NEW", "两处都有：用文件；只有环境：用环境；都没有：ConfigError 文字里只有变量名；status 只写来源")
s(G_KEY, "test_param_mode_has_no_api_key_flag_and_api_key_env_reads_only_the_named_variable", "NEW", "参数写法里没有收密钥本身的参数；--api-key-env 只认变量名；用假的 --api-key 报未知参数")
s(G_KEY, "test_key_test_asks_before_sending_and_declines_sends_nothing", "NEW", "「试一下」先问；回答否：假上游一次请求都没收到")
s(G_KEY, "test_key_test_reports_only_works_or_not_works_or_unverified", "NEW", "假上游返回 200／401／403／429／500／超时：只报 能用／不能用／没验成（401、403 才是不能用）；不回显对方正文")
s(G_KEY, "test_background_process_reads_key_file_itself_not_from_argv_or_environment", "NEW", "后台子进程的命令行和环境里都没有密钥值；它自己从文件读到并能发请求（假上游收到 Authorization）")
s(G_KEY, "test_key_value_not_in_db_wal_pending_toml_install_json_export_or_secrets_scan_output", "NEW", "整套流程跑完：库、WAL、待补、配置、安装记录、导出、scan 输出里都找不到假密钥")
s(G_KEY, "test_key_delete_requires_yes_and_uninstall_does_not_remove_key_file", "NEW", "key delete 不带 --yes 不删；卸载不删密钥文件")
s(G_KEY, "test_windows_like_platform_falls_back_to_env_only_and_says_so", "NEW", "不支持 0600 的系统（用假的 os 属性）：不存文件，说只认环境变量")

# ---- 六、费用账（NEW）
s(G_COST, "test_ledger_gets_currency_column_v12_and_old_rows_read_as_cny", "NEW", "schema 升到 v12；旧库的行 currency 都是 CNY，金额不变；v1–v11 的旧库都能打开")
s(G_COST, "test_deepseek_cost_rows_are_cny_and_luna_rows_are_usd_and_never_summed_together", "NEW", "两家各记一些调用：cost_sum 的 by_currency 分开（CNY、USD）；任何一个总数都不混币种")
s(G_COST, "test_cost_sum_legacy_keys_keep_meaning_cny_only", "NEW", "cost_sum 原有三个键的含义不变：cny 只加 CNY 行")
s(G_COST, "test_unpriced_model_records_real_token_counts_with_zero_amount_and_no_invented_number", "NEW", "没有价格表的模型：记真实词元数，金额 0，currency=NONE；任何地方都没有估出来的钱数")
s(G_COST, "test_unpriced_model_network_failure_records_unknown_without_pricing", "NEW", "没价格表的模型网络失败：记 NONE、unknown=1、备注「不估价」")
s(G_COST, "test_unpriced_model_never_pauses_or_blocks_even_when_cap_is_set", "NEW", "没价格表且设了 daily_cap_cny：整理照常跑，不暂停；最坏估计返回 None，上限检查被跳过")
s(G_COST, "test_status_says_budget_not_enforced_for_unpriced_model_and_wizard_warns", "NEW", "status 写「预算上限对这一家不起作用」和原因；向导选没测过的供应商时说「费用估算和预算上限不准」")
s(G_COST, "test_daily_cap_cny_only_counts_cny_rows", "NEW", "只有 daily_cap_cny（人民币）；daily_cap_usd 这一单不加（主会话裁定 15）")
s(G_COST, "test_existing_deepseek_cost_numbers_unchanged_after_the_change", "NEW,SPEC", "现有的费用测试原样通过；DeepSeek 的金额和以前一样")
s(G_COST, "test_cost_ledger_currency_column_is_in_the_delete_trace_inventory", "NEW", "新列登记进删除清单（保留类）；清单完整性测试通过")

# ---- 七、向量检测、下载、核验（SPEC）
s(G_VEC, "test_detect_ollama_present_service_responding", "SPEC", "假 PATH 里有假 ollama、假服务响应：报有 Ollama")
s(G_VEC, "test_detect_lmstudio_present_service_responding", "SPEC", "假 lms 脚本、假服务：报有 LM Studio")
s(G_VEC, "test_detect_neither_gives_official_urls_and_offers_keyword_only_or_cancel", "SPEC", "都没有：只给官方网址，不安装，选「先不开」后 retrieval=keyword，其余照装")
s(G_VEC, "test_detect_command_present_but_service_not_responding_tells_user_to_start_it_and_starts_nothing", "SPEC", "命令在、服务没响应：提示用户自己启动；假命令没有被调用过启动")
s(G_VEC, "test_both_present_lists_lmstudio_first_and_says_ollama_is_not_the_tested_runtime", "SPEC", "两个都有：先列 LM Studio（测过的），说明 Ollama 没测过")
s(G_VEC, "test_detection_is_read_only_no_install_no_start_no_download", "SPEC", "检测阶段：假命令只收到 which／GET；没有 pull、serve、install")
s(G_VEC, "test_user_declines_download_nothing_is_downloaded", "SPEC", "用户不同意：假 pull 一次都没被调；回到「先不开」")
s(G_VEC, "test_yes_alone_is_not_consent_to_download_requires_download_model_flag", "SPEC", "参数写法：只有 --yes 没有 --download-model，模型不在就报错退出，不下载、不改任何东西")
s(G_VEC, "test_download_happens_only_after_plan_confirmation_and_cancel_downloads_nothing", "SPEC", "向导里同意下载后又在计划处取消：没有下载")
s(G_VEC, "test_verification_failures_write_no_config", "SPEC", "假服务返回 768 维（默认模型期望 1024）：不写配置、不存密钥、不改 memory.provider，原因原样报出")
s(G_VEC, "test_verification_checks_determinism_and_related_vs_unrelated_similarity", "SPEC", "同一句两次的余弦低于 0.9999、或相近句不比无关句更近：核验失败")
s(G_VEC, "test_identity_has_all_ten_fields_and_weak_identity_is_marked_when_file_sha_unavailable", "SPEC", "组装出的身份十项齐全；取不到的项（如模型文件摘要）不放行，原因写明")
s(G_VEC, "test_probe_vectors_are_stored_for_later_drift_detection", "SPEC", "核验通过后探针向量记进 store_meta")
s(G_VEC, "test_remote_embedding_url_without_allow_remote_flag_is_refused", "SPEC", "远端地址不加 --allow-remote-embedding：拒绝；加了：计划里有「索引的文字和查询会发给那个服务」")
s(G_VEC, "test_commands_and_urls_come_from_the_runtime_table_and_can_be_replaced", "SPEC", "命令、地址、模型名都来自配置表；换一行假的，检测和下载用的就是假的")
s(G_VEC, "test_embedding_client_is_built_from_config_in_memory_open_and_in_adapter", "SPEC", "Memory.open 和 HermesAdapter 按配置造出向量客户端（现在没有）")

# ---- 八、缺向量（SPEC）
s(G_MISS, "test_partial_vectors_hybrid_uses_vectorized_anchors_and_keyword_only_for_the_rest", "SPEC", "池里一部分没向量：有向量的走关键词＋向量，没有的只靠关键词进候选；不报错")
s(G_MISS, "test_missing_vector_is_registered_in_read_log_with_used_reason_and_counts", "SPEC", "read_log 里有 kind=retrieval，detail 有 used、reason、pool、with_vector、missing")
s(G_MISS, "test_status_counts_reads_by_used_and_by_reason", "SPEC", "status 里各种 used、各种原因分开数")
s(G_MISS, "test_current_turn_messages_without_vectors_do_not_count_as_missing", "SPEC", "本轮被排除的消息没有向量：不算缺，used 仍是 hybrid")
s(G_MISS, "test_zero_vectors_in_pool_is_keyword_fallback_not_an_empty_vector_path", "SPEC", "池里一条向量都没有：按关键词走，登记 keyword_fallback／no_vectors_yet")
s(G_MISS, "test_service_down_makes_this_read_keyword_only_and_status_says_down_and_why", "SPEC", "假服务连不上：这一次只用关键词；status.service.state=down，原因只有类别")
s(G_MISS, "test_circuit_breaker_stops_reconnecting_for_30_seconds_then_tries_again", "SPEC", "失败后 30 秒内不再连（假时钟）；之后下一次读取再试一次")
s(G_MISS, "test_numpy_missing_falls_back_to_keyword_and_is_registered", "SPEC", "假装没有 NumPy：读取降成关键词，登记 numpy_missing；不抛 ImportError")
s(G_MISS, "test_strict_is_the_library_default_and_keeps_old_behavior", "SPEC", "strict（库默认）仍然抛 MissingVector；现有 7 条相关测试一条不改")
s(G_MISS, "test_wizard_writes_partial_and_library_default_stays_strict", "SPEC", "向导写的配置是 partial；Config() 默认是 strict")
s(G_MISS, "test_recall_pages_and_cursors_do_not_issue_new_vector_calls_in_partial_mode", "SPEC", "翻页不重搜、不发新的向量请求（现有约定）在 partial 下依然成立")
s(G_MISS, "test_degrade_info_is_not_in_material_text_only_in_structured_field_and_log", "SPEC", "材料正文里没有降级说明；字节和同样检索结果的非降级路径一致")
s(G_MISS, "test_query_is_redacted_before_it_reaches_the_vector_service_in_every_read_path", "SPEC", "recall 的问题、补充关键词、便条查询带假密钥：假向量服务收到的请求里没有原串")

# ---- 九、后台补算（SPEC）
s(G_BACK, "test_new_original_gets_a_vector_in_the_background", "SPEC", "新原话：记一条以后（inline 模式同步跑）它的锚点文字在向量表里有向量，身份指纹对")
s(G_BACK, "test_record_does_not_wait_for_the_vector_service_even_when_it_hangs", "SPEC", "假服务挂着不回应：record 的耗时不受影响（用计数的假时钟或超时断言）；回执正常")
s(G_BACK, "test_worker_holds_no_write_transaction_while_waiting_for_the_service", "SPEC", "假服务回应之前，别的连接能 BEGIN IMMEDIATE 写入（说明补算没占着写事务）")
s(G_BACK, "test_service_down_backs_off_and_resumes_after_recovery_without_recomputing", "SPEC", "服务断：失败计数加一、写退避；退避没到不发请求；服务恢复、到点以后接着补，已有的不重算")
s(G_BACK, "test_backoff_schedule_10_30_120_600_1800_and_success_resets", "SPEC", "失败 1–6 次的等待是 10、30、120、600、1800、1800；成功一次清零；进程重启后退避还在")
s(G_BACK, "test_no_duplicate_computation_for_texts_that_already_have_vectors_or_repeat_across_workspaces", "SPEC", "已有向量的不再算；同一句话在两个工作区只算一次")
s(G_BACK, "test_deleted_text_is_never_computed_after_deletion", "SPEC", "删除之后跑补算：被删的话没有向量、假服务没收到它")
s(G_BACK, "test_deletion_between_read_phase_and_write_phase_discards_the_whole_batch", "SPEC", "读阶段之后、写阶段之前发生删除：这一批一条都不写；discarded_by_delete 加一")
s(G_BACK, "test_interleaving_worker_commit_before_deletion_then_gc_removes_the_vector", "SPEC", "补算先提交、删除后到：刚写的向量被 _gc_vectors 清掉")
s(G_BACK, "test_worker_does_not_start_a_batch_while_a_deletion_is_incomplete", "SPEC", "有未完成的删除：补算退出，不挡 VACUUM／checkpoint")
s(G_BACK, "test_only_redacted_text_is_sent_and_unredacted_legacy_anchor_is_skipped_and_counted", "SPEC", "旧库里没过滤过的含假密钥的锚点：不发给服务、计入 skipped_secret、读取时当缺向量；假服务的请求里找不到原串")
s(G_BACK, "test_unembeddable_text_is_isolated_by_bisecting_and_listed_not_retried_forever", "SPEC", "一批里有一条被服务确定性拒绝（超长）：二分隔离，其余照算；那条进算不了名单，不再重试")
s(G_BACK, "test_skip_list_is_pruned_by_deletion_for_texts_no_longer_in_any_anchor", "SPEC", "算不了名单里的 sha：对应文字被删后一起清掉（删除模块里做）")
s(G_BACK, "test_same_key_different_float_values_keep_the_first_and_do_not_raise", "SPEC", "同一文字再算一次值略有不同：保留先到的，不抛错")
s(G_BACK, "test_lock_prevents_two_workers_and_second_exits_immediately", "SPEC", "两个补算同时起：第二个拿不到锁，立刻退出，不排队")
s(G_BACK, "test_spawn_failure_is_reported_in_receipt_and_never_breaks_record", "SPEC", "拉起失败：回执里有 vec_spawn_error；原话照存")
s(G_BACK, "test_spawn_passes_pythonpath_from_src_path_and_child_can_import_yuanwei", "SPEC", "子进程环境里有 PYTHONPATH=src_path；在没装 yuanwei 的 Python 里也能跑起来（假 src）")
s(G_BACK, "test_cheap_signature_skips_spawn_when_nothing_changed", "SPEC", "签名没变不拉起；新增锚点、删除、身份变了都会让签名变")
s(G_BACK, "test_status_reports_pending_last_error_next_try_and_stale_identities", "SPEC", "status 里有：还没算好几条、最近失败原因（只有类别）、下次重试时间、旧身份向量数")
s(G_BACK, "test_backfill_newest_first", "SPEC", "补算顺序：最近入库的先")
s(G_BACK, "test_prune_old_identity_vectors_goes_through_deletion_module_and_static_guard_allows_only_it", "SPEC", "vectors prune --yes 经 deletion.py；静态守卫里把 prune_vectors 列进允许名单，别处仍不许")

# ---- 十、编码身份（SPEC）
s(G_IDENT, "test_changing_model_quantization_or_runtime_changes_fp_and_old_vectors_are_not_used", "SPEC", "换模型、量化、运行方式：指纹变；读取不用旧指纹下的向量")
s(G_IDENT, "test_identity_change_recomputes_everything_under_the_new_fp_and_status_explains", "SPEC", "身份变后补算全部重算；status 写「已更换，旧的 N 条不再使用，已完成 x／y」")
s(G_IDENT, "test_during_recompute_reads_are_partial_hybrid_never_mixing_old_vectors", "SPEC", "重算期间读取是部分向量，且一条旧指纹的向量都没混进来")
s(G_IDENT, "test_service_drift_detected_by_worker_probe_pauses_backfill_and_degrades_queries", "SPEC", "服务换了模型（假服务换回应）：补算暂停；查询降成关键词，原因 identity_drift；不自动接受新身份")
s(G_IDENT, "test_vectors_setup_command_turns_vectors_on_later_and_reverify_rewrites_identity", "SPEC", "vectors setup --reverify：核验通过才写新身份")
s(G_IDENT, "test_dimension_change_is_caught_on_the_query_path_without_a_probe", "SPEC", "服务换成别的维度：查询路径的维度核对挡住，降级并登记")

# ---- 十一、逐字节不变（SPEC）
s(G_SAME, "test_all_vectors_present_materials_byte_identical_between_strict_and_partial", "SPEC", "向量齐全：取材和现在逐字节相同（对照改动前跑出的固定样本）")
s(G_SAME, "offline_replay_v1_v2_v3_v1b_byte_identical", "SPEC", "第一段回放 V1–V3、V1b 逐字节一致")
s(G_SAME, "offline_150_hybrid_requests_reproduce_110_of_110", "SPEC", "把 yuanwei/src 放最前，在库副本上照离线对比脚本拼请求：110/110 相同（读不了 exam-run 就写明没验，不当成过）")
s(G_SAME, "offline_keyword_mode_and_hybrid_materials_unchanged_vs_head", "SPEC", "关键词模式的输出一字节不变")
s(G_SAME, "offline_full_existing_suite_still_passes", "SPEC", "现有 1985 条全过（6 条骨架跳过除外）")

# ---- 十二、文档与助手说明（SPEC）
s(G_DOCS, "test_assistant_install_doc_starts_with_the_user_only_sentence_and_has_all_required_rules", "SPEC", "让助手来装.md 开头那一句、先列计划再 --yes、密钥由用户自己设、不替装 Ollama／LM Studio、不自己重启、出错就停、不改别的配置文件，每条都在")
s(G_DOCS, "test_assistant_doc_only_uses_commands_that_the_cli_actually_accepts", "SPEC", "文档里的每条命令都能被 argparse 解析（不执行）")
s(G_DOCS, "test_assistant_doc_never_asks_user_to_paste_a_key_and_says_not_to_run_key_set_itself", "NEW", "文档里没有「把密钥发给我」这类话；写明不要自己运行 yuanwei key set")
s(G_DOCS, "test_install_to_hermes_doc_has_wizard_and_all_flags_section", "SPEC", "装到Hermes上.md 有向导写法和「全参数写法」一节")
s(G_DOCS, "test_product_rules_table_hybrid_paragraph_rewritten_no_stale_all_vectors_required_sentence", "SPEC", "产品规则表里不再有「整次报缺」的旧句（strict 的说明除外）；有补算、缺向量、编码身份三段")
s(G_DOCS, "test_docs_say_download_only_after_consent_and_never_install_or_start_services", "SPEC", "文档里写「同意了才下载」，不再写「不联网」「不自动下载」的旧句")

# ---- 十三、放回缺陷（最多 12 个，在副本上跑；每个写明哪条测试必须红）
s(G_MUTATE, "mutation_01_write_phase_does_not_recheck_deletion_signature", "SPEC", "补算写阶段不核屏障签名 → 读写之间删除、被删的话算回来的测试必须红")
s(G_MUTATE, "mutation_02_worker_sends_text_without_redact_check", "SPEC", "补算不过 redact 就发 → 旧库含假密钥的锚点被发出去的测试必须红")
s(G_MUTATE, "mutation_03_service_call_inside_write_transaction", "SPEC", "把服务调用挪进 BEGIN IMMEDIATE 里 → 不占写事务的测试必须红")
s(G_MUTATE, "mutation_04_reader_accepts_vectors_of_old_identity", "SPEC", "读取也取旧指纹下的向量 → 身份变了不用旧向量的测试必须红")
s(G_MUTATE, "mutation_05_partial_mode_silently_falls_back_without_registering", "SPEC", "降级时不写 read_log、status 不数 → 登记和 status 的测试必须红")
s(G_MUTATE, "mutation_06_backoff_not_persisted_or_not_checked", "SPEC", "退避不存或不检查 → 退避没到不发请求、重启后还在的测试必须红")
s(G_MUTATE, "mutation_07_yes_implies_download_consent", "SPEC", "--yes 当成同意下载 → 「只有 --yes 不下载」的测试必须红")
s(G_MUTATE, "mutation_08_remote_embedding_url_allowed_without_flag", "SPEC", "远端向量地址不加开关也放行 → 拒绝远端的测试必须红")
s(G_MUTATE, "mutation_09_key_value_echoed_in_output_or_error_text", "NEW", "密钥值进了某条输出或错误文字 → 假密钥不出现在任何输出的测试必须红")
s(G_MUTATE, "mutation_10_luna_row_sends_temperature_and_max_tokens", "NEW", "luna 一行发了 temperature 或 max_tokens → 白名单参数的测试必须红")
s(G_MUTATE, "mutation_11_cost_sum_adds_usd_into_cny", "NEW", "费用汇总把美元加进人民币 → 分币种的测试必须红")
s(G_MUTATE, "mutation_12_unpriced_model_gets_a_worst_case_number_and_can_pause", "NEW", "没价格的模型也算出一个最坏估计并可能暂停 → 不卡住、不报假数的测试必须红")


# ================================================================ 骨架自检（现在就跑）
def test_skeleton_names_are_unique_and_every_row_has_text():
    names = [n for _g, n, _t, _w in S]
    assert len(names) == len(set(names))
    for g, n, t, w in S:
        assert n.startswith(("test_", "mutation_", "offline_")) and len(w) >= 8 and t, n


def test_at_most_twelve_mutations_and_every_group_present():
    assert sum(1 for g, *_ in S if g == G_MUTATE) <= 12
    assert {g for g, *_ in S} >= {G_WIZ, G_ENV, G_PRESET, G_PROV, G_KEY, G_COST, G_VEC, G_MISS, G_BACK, G_IDENT, G_SAME, G_DOCS, G_MUTATE}


def test_the_work_order_test_list_is_covered():
    """施工单「测试至少覆盖」的每个点，在骨架里至少有一行（按关键词核）。"""
    text = " ".join(g + " " + n + " " + w for g, n, _t, w in S)
    for kw in ("模拟输入", "--yes", "q、Ctrl-C", "setup hermes", "Python 版本低于", "找不到 Hermes", "没找到密钥", "假密钥",
               "有 Ollama", "有 LM Studio", "都没有", "服务没响应", "不同意", "维度", "--allow-remote-embedding",
               "新原话", "退避", "已有向量的不再算", "被删", "身份变", "缺向量", "read_log", "status", "逐字节", "V1–V3", "放回缺陷"):
        assert kw in text, kw


def test_the_11_11_additions_are_covered():
    """11-11 加的三样：预设、密钥、供应商与费用。"""
    new = [r for r in S if "NEW" in r[2].split(",")]
    text = " ".join(g + " " + n + " " + w for g, n, _t, w in new)
    for kw in ("--preset", "recommended", "light", "local", "custom", "getpass", "0600", "--api-key-env", "key set", "试一下", "temperature", "max_completion_tokens",
               "没测过", "币种", "currency", "不暂停", "没有价格表", "不显示值", "USD", "CNY"):
        assert kw in text, kw


def test_preset_and_provider_are_tables_not_a_plugin_framework():
    """用户 11-11：只是几个输入选项，一张小表，不搭插件机制。骨架里不能出现 plugin／registry／loader 一类的行。"""
    for _g, n, _t, w in S:
        assert not any(bad in n for bad in ("plugin_system", "registry", "loader")), n


def test_row_count_is_not_tiny():
    assert len(S) >= 120


# ================================================================ 乙：每一行都要有同名的真测试（offline_ 开头的是离线脚本，结果写在交回报告里，不在套件里跑）
import glob
import os
import re

_HERE = os.path.dirname(__file__)


def _defs():
    out = set()
    for f in glob.glob(os.path.join(_HERE, "test_*.py")):
        if f.endswith("test_wizard_vectors_skeleton.py"):
            continue
        out |= set(re.findall(r"^def (test_\w+)\(", open(f, encoding="utf-8").read(), re.M))
    return out


def test_every_skeleton_row_has_a_real_test_of_the_same_name():
    d = _defs()
    missing = [n for g, n, _t, _w in S if g != G_MUTATE and not n.startswith("offline_") and n not in d]
    assert missing == []


def test_offline_rows_are_only_the_four_that_need_data_outside_the_repo_or_the_old_code():
    assert sorted(n for g, n, _t, _w in S if n.startswith("offline_")) == sorted(
        ["offline_replay_v1_v2_v3_v1b_byte_identical", "offline_150_hybrid_requests_reproduce_110_of_110", "offline_keyword_mode_and_hybrid_materials_unchanged_vs_head", "offline_full_existing_suite_still_passes"])
