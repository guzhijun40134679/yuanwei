"""预设、整理用供应商、本机向量运行方式、价格：都只是几张小表（数据），向导和不问话的写法读同一份。
加一行就多一个选项，不用改别处的代码。凡是 verified=False 的地址、模型名，都是「未核」：真机试装时逐个核（不带密钥的连通检查，返回 401 算连得上）。
"""
TESTED, TRIED, UNTESTED, EXPERIMENT = "测过", "试过", "没测过", "实验"
LABELS = (TESTED, TRIED, UNTESTED, EXPERIMENT)
UNTESTED_NOTE = "没测过：整理的提示词只在 DeepSeek 上调过"

# ---- 预设（第一步选；每个预设给后面每一步的默认值）
PRESETS = [
    {"id": "recommended", "name": "推荐", "label": TESTED, "provider": "deepseek", "vectors": "auto",
     "label_text": "整理测过；向量：LM Studio 是测过的那一套，自带程序和它几乎一样、没过我们自己定的严格标准",        # 向量 auto：Mac 的 M 系列上会落到自带程序，所以不能整个标「测过」
     "summary": "整理用 DeepSeek，再加本机向量。聊得久了以后找得更准；要下载一个约 600 MB 的本机模型，向量计算在你自己的电脑上。"},
    {"id": "light", "name": "轻量", "label": TESTED, "label_text": "整理测过；检索只用关键词，答题效果没测过", "provider": "deepseek", "vectors": "none",
     "summary": "整理用 DeepSeek（测过）；检索只用关键词（答题效果没测过），不下载任何东西。以后随时可以再开向量（yuanwei vectors setup）。"},
    {"id": "custom", "name": "自选", "label": UNTESTED, "provider": "ask", "vectors": "ask",
     "summary": "自己选整理用的供应商、模型，以及向量服务。没测过。"},
    {"id": "local", "name": "全本机（实验）", "label": EXPERIMENT, "provider": "local", "vectors": "auto",
     "summary": "整理用你电脑上的大模型，向量也在本机，对话不出电脑。效果没测过，很吃内存。"},
]

# ---- 整理用供应商（兼容 OpenAI 的接口）。params 是白名单：表里写什么就发什么，没写的一律不发
PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "example_model": None,
     "params": {"temperature": 0, "reasoning_effort": "high", "max_tokens": 65536}, "label": TESTED, "key_env": "DEEPSEEK_API_KEY", "verified": True, "local_only": False,
     "note": "150 次复验和考核都用它整理"},
    {"id": "openai-luna", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna", "example_model": None,
     "params": {"reasoning_effort": "high", "max_completion_tokens": 65536}, "label": TRIED, "key_env": "OPENAI_API_KEY", "verified": False, "local_only": False,
     "note": "11-06 在旧故事上整理两串（96 步，放弃 0）；新故事没测。参数照 提_公共.py：不发 temperature、不发 max_tokens"},
    {"id": "moonshot", "name": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "model": None, "example_model": "moonshot-v1-32k",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_MOONSHOT_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "bailian", "name": "阿里云百炼（通义千问，兼容模式）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": None, "example_model": "qwen-plus",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_BAILIAN_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": None, "example_model": "glm-4-plus",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_ZHIPU_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "model": None, "example_model": "Qwen/Qwen2.5-72B-Instruct",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_SILICONFLOW_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": None, "example_model": "deepseek/deepseek-chat",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_OPENROUTER_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "manual", "name": "手填（兼容 OpenAI 接口）", "base_url": None, "model": None, "example_model": "平台给的模型名",
     "params": {"temperature": 0}, "label": UNTESTED, "key_env": "YUANWEI_CUSTOM_API_KEY", "verified": False, "local_only": False, "note": UNTESTED_NOTE},
    {"id": "local", "name": "本机大模型", "base_url": "http://127.0.0.1:11434/v1", "model": None, "example_model": "本机服务里的模型名",
     "params": {"temperature": 0}, "label": EXPERIMENT, "key_env": None, "verified": False, "local_only": True,
     "note": "对话不出电脑；效果没测过；吃内存"},
]

# ---- 本机向量运行方式（默认值全部「真机试装时核」）
EMBED_MODEL_DIM = 1024
RUNTIMES = [
    {"id": "lmstudio", "name": "LM Studio", "tested": True, "cmd": "lms", "base_url": "http://127.0.0.1:1234/v1", "probe_path": "/v1/models", "models_path": "/v1/models",
     "model": "text-embedding-qwen3-embedding-0.6b", "pull_cmd": ["{cmd}", "get", "Qwen/Qwen3-Embedding-0.6B-GGUF"], "official_url": "https://lmstudio.ai",
     "blurb": "测过的就是它；要自己开一次本机服务。"},
    {"id": "ollama", "name": "Ollama", "tested": False, "cmd": "ollama", "base_url": "http://127.0.0.1:11434/v1", "probe_path": "/api/version", "models_path": "/api/tags",
     "model": "qwen3-embedding:0.6b", "pull_cmd": ["{cmd}", "pull", "{model}"], "official_url": "https://ollama.com/download",
     "blurb": "更省事，装好就在后台跑；同一个模型，但没在它上面单独测过。"},
    {"id": "llamacpp", "name": "自带（llama.cpp 服务程序）", "tested": False, "managed": True, "cmd": None, "base_url": None, "probe_path": None, "models_path": None,
     "model": "qwen3-embedding-0.6b-q8_0", "pull_cmd": None, "official_url": "https://github.com/ggml-org/llama.cpp",
     "blurb": "原委替你准备程序和模型，不用装别的软件，也不用你开服务。同一个模型文件；向量和 LM Studio 算的几乎一样，没过我们自己定的严格标准。"},
]

# ---- 价格（每百万词元）。没有的模型 = 没有价格表：只记词元数，不估价
PRICES = {
    "deepseek-flash": {"currency": "CNY", "in_miss": 2.0, "in_hit": 0.04, "out": 8.0, "source": "沿用 Config.price_*"},
    "gpt-6-luna": {"currency": "USD", "in_miss": 0.10, "in_hit": 0.01, "in_write": 0.125, "out": 0.50, "source": "提_公共.py PRICE_USD_PER_M（11-06 试探，真机试装时核）"},
}


def get(table, id_):
    for r in table:
        if r["id"] == id_:
            return r
    return None


def preset(id_):
    return get(PRESETS, id_)


def provider(id_):
    return get(PROVIDERS, id_)


def runtime(id_):
    return get(RUNTIMES, id_)


def price(model):
    return PRICES.get(model)


# ================================================================ 自带向量引擎（llama.cpp 服务程序）：钉住的值、启动配方
# 下面是钉住的值。engine 这一块是 2026-11-11 主会话在真机上下载、逐项核对的（事实见 产品打包/真机事实-自带引擎-2026-11-11.md 和 …-b11701-2026-11-11.json）。
# pins 里 verified 为假、或有必填的格子是 None，下载和启动一律被拒（没有环境变量能放过）。测试靠注入假的钉住表（pins=）和假下载器，不靠开关。
ENGINE_CTX = 2048                                                    # 和测过的「加载上下文长度」对齐（主会话裁定 Q3）
ENGINE_SUPPORTED = (("Darwin", "arm64"),)
PIN_REQUIRED_ENGINE = ("tag", "url", "kind", "sha256", "size", "max_total_bytes", "max_files", "binary", "manifest")
PIN_REQUIRED_MODEL = ("sha256", "size", "file_name")

PINS = {
    "engine": {
        "id": "llama.cpp-server", "tag": "b11701", "license": "MIT", "verified": True, "owner": "ggml-org", "repo": "ggml-org/llama.cpp",
        "release_page": "https://github.com/ggml-org/llama.cpp/releases/tag/b11701",
        "published_at": "2026-10-16T23:20:45Z",
        # llama.cpp 每一版都标「预发布」，一天十几版，没有正式版。挑 b11701：10-20 起 Mac 包从约 12 MB 跳到约 16 MB（构建方式变了），这是变之前的最后一版；资产名式样稳定。
        "source_note": "摘要首次来源：GitHub 发布记录（ggml-org/llama.cpp 的 b11701，发布于 2026-10-16T23:20:45Z）登记的 digest；2026-11-11 主会话实际下载压缩包、自己算 SHA-256，"
                       "和登记的 digest 一致；解开后逐个文件算 SHA-256 生成下面的清单；本机运行核过自报版本（0.7.0-dev，build 11701，commit 01fc3aae8）",
        "assets": {"darwin-arm64": {
            "url": "https://github.com/ggml-org/llama.cpp/releases/download/b11701/llama-b11701-bin-macos-arm64.tar.gz",     # 下载会跳到 release-assets.githubusercontent.com（https）
            "kind": "tar.gz",
            "sha256": "e4219b1442dcf0b68e860ea6f06d417163326391e8514b04019080f2444ff47c", "size": 12249449,                  # 压缩包的 SHA-256、字节数（实际下载核过）
            # 解压后实际：61 个条目（1 个目录、42 个文件、18 个包内链接），文件合计 32,421,654 字节，最大的一个 9,250,520 字节。
            # 上限只是第二道防线（清单已经逐个文件钉死了大小和摘要）：条目数留 80（实际 61，多约三成，给 tar 里偶尔多出来的目录项一点余地），总字节留 40 MiB＝41,943,040（实际 32.4 MB，多约三成）。
            "max_total_bytes": 41943040, "max_files": 80,
            "top_dir": "llama-b11701",
            "binary": "llama-b11701/llama-server",                                                                         # 自报：0.7.0-dev (build 11701, commit 01fc3aae8)
            # 整个运行包的清单（只列文件和链接，目录不列）：文件钉死大小、SHA-256、可不可执行（exec）；链接钉死目标（都在同一目录里，没有绝对路径和 ..）
            "manifest": [
                {"path": "llama-b11701/LICENSE", "type": "file", "size": 1078, "sha256": "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d", "exec": False},
                {"path": "llama-b11701/ggml-metal-tuning", "type": "file", "size": 87696, "sha256": "72162323fcee7d7889190899ca69ddbc1423c29e5f1f502e9bfd6dad003d8317", "exec": True},
                {"path": "llama-b11701/ggml-rpc-server", "type": "file", "size": 133136, "sha256": "e5e80be949739e616bd11ec7bee841e80d22c11af0e5945bf987cff6e8e6daff", "exec": True},
                {"path": "llama-b11701/libggml-base.0.27.0.dylib", "type": "file", "size": 751240, "sha256": "c1c2475546d7feb872d5f855fd30f3c146ecf60aaaca30d52cd0eeff38e846d9", "exec": True},
                {"path": "llama-b11701/libggml-base.0.dylib", "type": "symlink", "target": "libggml-base.0.27.0.dylib"},
                {"path": "llama-b11701/libggml-base.dylib", "type": "symlink", "target": "libggml-base.0.dylib"},
                {"path": "llama-b11701/libggml-blas.0.27.0.dylib", "type": "file", "size": 58776, "sha256": "12aefdc8dd4873d69554dc85a0dd09fffef9828e1fc9ecf6b48913ef2106b33a", "exec": True},
                {"path": "llama-b11701/libggml-blas.0.dylib", "type": "symlink", "target": "libggml-blas.0.27.0.dylib"},
                {"path": "llama-b11701/libggml-blas.dylib", "type": "symlink", "target": "libggml-blas.0.dylib"},
                {"path": "llama-b11701/libggml-cpu.0.27.0.dylib", "type": "file", "size": 957408, "sha256": "f29b0322166bc9ea2a1f2482f9cd034959013228831f5bfd06aa770b75dd4d12", "exec": True},
                {"path": "llama-b11701/libggml-cpu.0.dylib", "type": "symlink", "target": "libggml-cpu.0.27.0.dylib"},
                {"path": "llama-b11701/libggml-cpu.dylib", "type": "symlink", "target": "libggml-cpu.0.dylib"},
                {"path": "llama-b11701/libggml-metal.0.27.0.dylib", "type": "file", "size": 5587704, "sha256": "448d7eafb7906c4bbd445cac28fad15ae9311b4315461283742a2fb1af2a5653", "exec": True},
                {"path": "llama-b11701/libggml-metal.0.dylib", "type": "symlink", "target": "libggml-metal.0.27.0.dylib"},
                {"path": "llama-b11701/libggml-metal.dylib", "type": "symlink", "target": "libggml-metal.0.dylib"},
                {"path": "llama-b11701/libggml-rpc.0.27.0.dylib", "type": "file", "size": 242800, "sha256": "547ac08b705a96e82ddb9fcfc2529472f97f5da1957984b3a7652cd5875d452d", "exec": True},
                {"path": "llama-b11701/libggml-rpc.0.dylib", "type": "symlink", "target": "libggml-rpc.0.27.0.dylib"},
                {"path": "llama-b11701/libggml-rpc.dylib", "type": "symlink", "target": "libggml-rpc.0.dylib"},
                {"path": "llama-b11701/libggml.0.27.0.dylib", "type": "file", "size": 60016, "sha256": "a97bc40525aea78834fdf28197b5b21f149b6d5ac2d3b615b95b86f775a69477", "exec": True},
                {"path": "llama-b11701/libggml.0.dylib", "type": "symlink", "target": "libggml.0.27.0.dylib"},
                {"path": "llama-b11701/libggml.dylib", "type": "symlink", "target": "libggml.0.dylib"},
                {"path": "llama-b11701/libllama-batched-bench-impl.dylib", "type": "file", "size": 56576, "sha256": "55883543101b508dd945509c55860f7badd4286d669283e13afdfc2f54dbf623", "exec": True},
                {"path": "llama-b11701/libllama-bench-impl.dylib", "type": "file", "size": 323016, "sha256": "698a7614525fae1a2d854bf4dba05f23d127b9916d156694775467904fac647d", "exec": True},
                {"path": "llama-b11701/libllama-cli-impl.dylib", "type": "file", "size": 255144, "sha256": "d20470e85282da77477405c8d3e66336d7bdf50b76b125aa42a76e6b8270aba5", "exec": True},
                {"path": "llama-b11701/libllama-common.0.7.0.dylib", "type": "file", "size": 7865960, "sha256": "4d79801e3660f42ce9760b03067d85226d8a168877e8a24b09a155918c38aaf8", "exec": True},
                {"path": "llama-b11701/libllama-common.0.dylib", "type": "symlink", "target": "libllama-common.0.7.0.dylib"},
                {"path": "llama-b11701/libllama-common.dylib", "type": "symlink", "target": "libllama-common.0.dylib"},
                {"path": "llama-b11701/libllama-completion-impl.dylib", "type": "file", "size": 121744, "sha256": "49f60dda87db4094b98141a5c7adc4321400ee957a12736765176cdca275eabb", "exec": True},
                {"path": "llama-b11701/libllama-fit-params-impl.dylib", "type": "file", "size": 55264, "sha256": "14b8f4a2858391b0f576a8fc5439fb6b9aa69eb36c82e9bfcdb28fd0df23a3ac", "exec": True},
                {"path": "llama-b11701/libllama-perplexity-impl.dylib", "type": "file", "size": 156384, "sha256": "743af2c79af7d04bfc4676ff0587a339a3792e2452041db84f44d050e8e6de92", "exec": True},
                {"path": "llama-b11701/libllama-quantize-impl.dylib", "type": "file", "size": 96496, "sha256": "582dadbf515289d7a5dd5aa27be5fdcbe58cad0a2eb4eaa72a15087ad9d26864", "exec": True},
                {"path": "llama-b11701/libllama-server-impl.dylib", "type": "file", "size": 9250520, "sha256": "637257849f3002d67b124811997031ed2b0c48ae48ca3083626c74ad6ab82a8b", "exec": True},
                {"path": "llama-b11701/libllama.0.7.0.dylib", "type": "file", "size": 3529184, "sha256": "a227d9c9c4fbd2486c7cafd4f3b0fd09c7662527473b11cbed1acceca9713bdf", "exec": True},
                {"path": "llama-b11701/libllama.0.dylib", "type": "symlink", "target": "libllama.0.7.0.dylib"},
                {"path": "llama-b11701/libllama.dylib", "type": "symlink", "target": "libllama.0.dylib"},
                {"path": "llama-b11701/libmtmd.0.7.0.dylib", "type": "file", "size": 1459072, "sha256": "d5771c8af10f63a194ce906f14c813e6aa290d548b1c297678e643e50e6bbb39", "exec": True},
                {"path": "llama-b11701/libmtmd.0.dylib", "type": "symlink", "target": "libmtmd.0.7.0.dylib"},
                {"path": "llama-b11701/libmtmd.dylib", "type": "symlink", "target": "libmtmd.0.dylib"},
                {"path": "llama-b11701/llama", "type": "file", "size": 107320, "sha256": "3c4fad1e432b6942e279bc102c9c4639e010e983e29e2580947929ae1f249c7b", "exec": True},
                {"path": "llama-b11701/llama-batched-bench", "type": "file", "size": 33488, "sha256": "64f9f09bd2bf1f9f2e9748e36a4dc802704ce8c3fa20eddfc314c6bebd3006a0", "exec": True},
                {"path": "llama-b11701/llama-bench", "type": "file", "size": 33464, "sha256": "8e381bc0e4463985ccf1425e3628566664992230d545d4db0482f58482a4fb45", "exec": True},
                {"path": "llama-b11701/llama-cli", "type": "file", "size": 49960, "sha256": "67b08224c590a337389e8b46a8d6e49902b959be9922e572ffd81645bac1b084", "exec": True},
                {"path": "llama-b11701/llama-completion", "type": "file", "size": 33472, "sha256": "2b14c5f6bf13aec0118c0917797178d1300d85f8b8321a301fc8fc206c38a934", "exec": True},
                {"path": "llama-b11701/llama-cvector-generator", "type": "file", "size": 102296, "sha256": "5b46a82cfc32167e4851760cf51e4f891d42d1f891b4ad5ffcc17b09ce321848", "exec": True},
                {"path": "llama-b11701/llama-export-lora", "type": "file", "size": 99856, "sha256": "10c070346e874490fe66905acb69c6cd84e5745df503d336186072888fe0e919", "exec": True},
                {"path": "llama-b11701/llama-fit-params", "type": "file", "size": 33472, "sha256": "ceb4c943e71f731a60cbdb351928999822cd973b261b160c89e245b16fe8ba61", "exec": True},
                {"path": "llama-b11701/llama-gemma3-cli", "type": "file", "size": 35344, "sha256": "f7f9126b0881654bdb4c9ceab6de40f42e31998fa0c775505b5979ef7e8e567e", "exec": True},
                {"path": "llama-b11701/llama-gguf-split", "type": "file", "size": 59952, "sha256": "b0e0b7e10eaa5e51d3ec05dcf1192e594248e9e14684a8deac3a7ae71bb2e87b", "exec": True},
                {"path": "llama-b11701/llama-imatrix", "type": "file", "size": 206320, "sha256": "ce2afc6c4d5aa9a4fe5da7c6f720ea12af8e335127fde56bf67c8a5384ed3f1e", "exec": True},
                {"path": "llama-b11701/llama-llava-cli", "type": "file", "size": 35344, "sha256": "9a77293c34faa94ae6955f9b1da3dca9fa55cf09fcd0fff87c96f5ec44b9d547", "exec": True},
                {"path": "llama-b11701/llama-minicpmv-cli", "type": "file", "size": 35344, "sha256": "4baffe08bb410686ee032b1b2ff9a948bbde3fdc014dddf750f36978e3b40589", "exec": True},
                {"path": "llama-b11701/llama-mtmd-cli", "type": "file", "size": 103680, "sha256": "56c910a0461732bb7e9f895130b74c0279222d170816d4126504b3ce0634b7b1", "exec": True},
                {"path": "llama-b11701/llama-mtmd-debug", "type": "file", "size": 75824, "sha256": "9415b27059be9cbe5bd3bab202d5141941b32086cfd8c25c1536bddf8264aada", "exec": True},
                {"path": "llama-b11701/llama-perplexity", "type": "file", "size": 33472, "sha256": "2efff38c7c52e14a9d9ba0d34319a572b514c79d1fb5f7804fbf87ae061980c4", "exec": True},
                {"path": "llama-b11701/llama-quantize", "type": "file", "size": 33472, "sha256": "88a670a6136af2b1877305ece7e92fe470078a2425986b68ea25f886ddc9685b", "exec": True},
                {"path": "llama-b11701/llama-qwen2vl-cli", "type": "file", "size": 35344, "sha256": "9be4a36b2587c0216009b1ed59ac79b1e4dd2e885f326e8aeffac8aa37761d31", "exec": True},
                {"path": "llama-b11701/llama-results", "type": "file", "size": 58912, "sha256": "6d49341ed672c8ffd9db1e7c404f66839d9cbf94decb89034dd50f19e33019fa", "exec": True},
                {"path": "llama-b11701/llama-server", "type": "file", "size": 49984, "sha256": "981b460a278e9a0c880cef32d6fab42785a119084456b16845304cf38c32cf69", "exec": True},
                {"path": "llama-b11701/llama-tokenize", "type": "file", "size": 58736, "sha256": "0f3c161faed20f9cfd4759b26283b266367327181ff4182a4c0803866812c7ad", "exec": True},
                {"path": "llama-b11701/llama-tts", "type": "file", "size": 57384, "sha256": "79dd3217746cda6998689ccb0b8ce2d33fa6944422d9522db256b6f0d58e42e2", "exec": True},
            ],
        }},
    },
    # 模型文件：「可以复用的」和「可以下载的」分开登记；各自的一致性状态在 CONSISTENCY 里分开记，一个过了不连带另一个。
    # 2026-11-11 核过：官方发布的文件和测过的是同一个文件（SHA-256 一样，Hugging Face 的 x-linked-etag 和 lfs oid、魔搭的 Sha256 都是它），所以两条登记的摘要相同。
    "models": [
        {"id": "qwen3-embedding-0.6b-q8_0@tested", "role": "reusable", "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
         "size": 639150592, "file_name": "Qwen3-Embedding-0.6B-Q8_0.gguf", "sources": [], "license": "Apache-2.0", "verified": True,
         "note": "150 次复验用 LM Studio 跑的就是这个文件（主会话提供）；本机 LM Studio 目录里的同一个文件 SHA-256 核过一致，可以直接复用，原委只读不动"},
        {"id": "qwen3-embedding-0.6b-q8_0@official", "role": "downloadable", "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439", "size": 639150592,
         "file_name": "Qwen3-Embedding-0.6B-Q8_0.gguf", "license": "Apache-2.0", "verified": True,
         "sources": [{"name": "Hugging Face（通义千问官方仓库）", "url": "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf"},     # 302 到 us.aws.cdn.hf.co（https）
                     {"name": "魔搭 ModelScope（通义千问官方账号）", "url": "https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/master/Qwen3-Embedding-0.6B-Q8_0.gguf"}],   # 直接 200
         "note": "官方文件就是测过的那个文件（SHA-256 相同）；下载来的文件落盘前照样核这个摘要，对不上就删掉"},
    ],
    "embedding_dim": EMBED_MODEL_DIM,
}

# 测过的向量身份（主会话 11-11 定）：LM Studio 本机兼容接口、这个模型文件、加载上下文 2048、运行包 @2.53.0。只有核验得到的身份和它逐项一样，才能写「测过」。
# 别的运行方式（Ollama、自带引擎、手填）没有测过的身份，一律「没测过」。字段名同 vectors.IDENTITY_FIELDS。
TESTED_EMBED_IDENTITY = {
    "provider": "lmstudio", "model": "text-embedding-qwen3-embedding-0.6b",
    "model_file_sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
    "runtime_version": "llama.cpp-mac-arm64-apple-metal-advsimd@2.53.0", "context_length": 2048,
    "dimension": 1024, "normalization": "l2", "prefix": "none", "template": "none", "text_processing": "verbatim",
}
IDENTITY_FIELD_ZH = {"provider": "运行方式", "model": "模型名", "model_file_sha256": "模型文件", "runtime_version": "引擎版本", "context_length": "加载的上下文长度",
                     "dimension": "维度", "normalization": "归一化", "prefix": "输入前缀", "template": "任务模板", "text_processing": "文字处理"}
TESTED_YES, TESTED_ENGINE, TESTED_NO = "tested", "engine_differs", "untested"


def _engine_ver(v):
    return str(v).rsplit("@", 1)[-1]


def compare_to_tested(identity, weak=False):
    """把核验得到的向量身份和测过的比。返回 {"applicable","level","differs","text"}；level 三档：
    tested（逐项一样）｜engine_differs（只有运行包版本不同，而且这一项取到了、身份不弱）｜untested（别的：模型文件对不上、取不到（弱）、上下文或维度不同……，differs 列出哪几项）。
    不是 LM Studio（没有测过的身份）：applicable=False，一律 untested。"""
    if isinstance(identity, dict) and str(identity.get("provider") or "").startswith(ENGINE_PROVIDER_PREFIX):
        return engine_consistency_for_identity(identity)
    if not isinstance(identity, dict) or identity.get("provider") != TESTED_EMBED_IDENTITY["provider"]:
        return {"applicable": False, "level": TESTED_NO, "differs": [], "text": "没测过（这种运行方式没有测过的向量身份）"}
    differs = [k for k in TESTED_EMBED_IDENTITY if identity.get(k) != TESTED_EMBED_IDENTITY[k]]
    names = "、".join(IDENTITY_FIELD_ZH[k] for k in differs)
    if not differs and not weak:
        return {"applicable": True, "level": TESTED_YES, "differs": [], "text": "测过（和测过的设置完全一样）"}
    rv = identity.get("runtime_version")
    if differs == ["runtime_version"] and not weak and isinstance(rv, str) and "@" in rv:
        return {"applicable": True, "level": TESTED_ENGINE, "differs": differs,
                "text": "同一个模型文件和设置；LM Studio 的引擎版本不同（测过的是 %s，你的是 %s），算出的向量可能有极小差别，这个版本没核过" % (
                    _engine_ver(TESTED_EMBED_IDENTITY["runtime_version"]), _engine_ver(rv))}
    if not differs:
        names = "有取不到、只取到上限的项（身份偏弱）"
    return {"applicable": True, "level": TESTED_NO, "differs": differs, "text": "没测过：和测过的设置不一样的有：%s" % names}


# 自带引擎和 LM Studio 测过的向量一致吗：按「模型文件摘要｜引擎版本｜运行包摘要（前 12 位）｜启动配方摘要（前 12 位）」登记，换了任何一项都不沿用这条结果、回到「一致性还没在真机上核」。
# strong 为真才能写「测过」。核过但没过强档的，写专门的一档（见 engine_consistency_text）；记录里的数字就是核对记录里的原数。
ENGINE_PROVIDER_PREFIX = "llama.cpp-server"
LABEL_UNCHECKED = "一致性还没在真机上核"
LABEL_STRONG = "测过（和 LM Studio 的向量一致）"
CHECKED, CHECKED_NOT_STRONG, UNCHECKED = "strong", "checked_not_strong", "unchecked"


def consistency_key(model_sha, tag, pkg, recipe):
    return "%s|%s|%s|%s" % (model_sha, tag, str(pkg)[:12], str(recipe)[:12])


CONSISTENCY = {
    # 2026-11-11 晚，主会话在真机上正式核（产品打包/一致性核对记录-自带引擎-2026-11-11.md）：引擎 b11701、运行包 6822d22ee55c、模型 06507c7b…、启动配方 5ffb9ffa5eac。
    # 门槛一（抽样 1,000 条不同文字，种子 20261111）没过：最小 0.998596，2 条低于 0.999。全量 11,588 条：最小 0.997689，8 条低于 0.999，没有低于 0.995 的。用户定：不改规矩，自带程序照常给用，说明老实写；「测过」只给 LM Studio。
    consistency_key("06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439", "b11701", "6822d22ee55c", "5ffb9ffa5eac"): {
        "strong": False, "at": "2026-11-11", "report": "一致性核对记录-自带引擎-2026-11-11.md", "engine": "b11701", "pkg": "6822d22ee55c",
        "sample_n": 1000, "sample_min": 0.998596, "sample_below_0999": 2, "full_n": 11588, "full_min": 0.997689, "full_below_0999": 8,
    },
}


def engine_consistency_state(model_sha, tag, pkg, recipe):
    """返回 {"level": strong｜checked_not_strong｜unchecked, "record", "text"}。找不到这一组（换了模型、引擎版本、运行包或启动配方）就是 unchecked。"""
    rec = CONSISTENCY.get(consistency_key(model_sha, tag, pkg, recipe))
    if not rec:
        return {"level": UNCHECKED, "record": None, "text": LABEL_UNCHECKED}
    if rec.get("strong"):
        return {"level": CHECKED, "record": rec, "text": LABEL_STRONG}
    return {"level": CHECKED_NOT_STRONG, "record": rec, "text": engine_consistency_text(rec)}


def engine_consistency_text(rec):
    """核过、强档没过：同一个模型文件，向量几乎一样，但没过我们自己定的线，所以不能当成和 LM Studio 那套完全一样。不写「测过」，不写「一致」。"""
    return ("同一个模型文件；向量和 LM Studio 算的几乎一样（全量 {n:,} 句里 {k} 句略低于我们定的线，最低 {m:.4f}），没过我们自己定的严格标准，所以不能当成和 LM Studio 那套完全一样").format(
        n=rec["full_n"], k=rec["full_below_0999"], m=rec["full_min"])


def engine_consistency_for_identity(identity):
    """从核验得到的编码身份里取（模型摘要、引擎版本、运行包摘要、启动配方摘要），查上面的表。身份读不出来按没核过。"""
    import re
    m = re.match(r"^llama\.cpp-server@([^;]+);pkg=([0-9a-f]+);recipe=([0-9a-f]+)$", str(identity.get("runtime_version") or ""))
    st = engine_consistency_state(identity.get("model_file_sha256"), *m.groups()) if m else {"level": UNCHECKED, "record": None, "text": LABEL_UNCHECKED}
    return {"applicable": False, "level": st["level"], "differs": [], "text": st["text"]}


# 启动参数（数据；参数名在 llama.cpp b11701 上已核，2026-11-11）：每行 (名字, 命令行片段, 说明)。占位符 {model_path} {port} {alias} {keyfile} 在启动时才填。
ENGINE_ARGS = [
    ("model", ["-m", "{model_path}"], "模型文件"),
    ("embedding", ["--embedding"], "打开向量接口（b11701 已核；--embeddings 是同一个）"),
    ("pooling", ["--pooling", "last"], "取最后一个词（Qwen3-Embedding 要这样）"),
    ("host", ["--host", "127.0.0.1"], "只监听本机回环地址"),
    ("port", ["--port", "{port}"], "随机空闲端口"),
    ("ctx", ["-c", str(ENGINE_CTX)], "上下文长度，和测过的一致"),
    ("batch", ["-b", str(ENGINE_CTX)], "批大小"),
    ("ubatch", ["-ub", str(ENGINE_CTX)], "物理批大小"),
    ("threads", ["-t", "4"], "线程数（固定值，不随电脑变，免得配方变）"),
    ("parallel", ["-np", "1"], "同时处理 1 条"),
    ("alias", ["--alias", "{alias}"], "带本轮启动代号的名字，客户端用它核对「这就是我拉起的那一个」"),
    ("gpu", ["-ngl", "99"], "全放 GPU（Metal）"),
    ("no_ui", ["--no-ui"], "不要网页界面（b11701 已核：--no-webui 在这一版是旧名字，已标成过时）"),
    ("no_slots", ["--no-slots"], "不要 slots 接口"),
    ("api_key_file", ["--api-key-file", "{keyfile}"], "本轮的随机凭据文件（不放在命令行上）"),
]
RECIPE_VERSION = 1
RECIPE_TEXT = {"prefix": "none", "template": "none", "processing": "verbatim", "normalization": "client-float64-L2", "pooling": "last"}
ENGINE_DEFAULTS = {"idle_exit_seconds": 900, "cold_wait_seconds": 0, "ready_timeout_seconds": 60, "poll_seconds": 1.0, "stable_seconds": 60, "max_consecutive_failures": 5,
                   "max_total_restarts": 20, "restart_delays": (1, 3, 10, 30, 120), "cooldown_seconds": (600, 1800), "request_max_seconds": 300, "stop_wait_seconds": 10,
                   "breaker_seconds": 30, "download_max_seconds": 3600, "kill_grace_seconds": 5}
