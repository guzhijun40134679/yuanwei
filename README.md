# Yuanwei (原委)

English | [简体中文](https://github.com/guzhijun40134679/yuanwei/blob/main/README.zh-CN.md)

**Every memory has its origin; every quote has its source.** Long-term memory for AI assistants.

> Preview release · research prototype; some features are still being developed and tested. On 2026-11-11 it was installed on Hermes v0.22.0 (+10397) and every item on that checklist passed; what the checklist did not cover is listed under the limits below.

> **Language:** the setup wizard, the command-line messages and the detailed guides in `docs/` are currently in Chinese only.

## What it does

- **Keeps your words verbatim.** What you and your assistant said is stored exactly as it was said, with who said it and on which day. Nothing is rewritten or summarized.
- **Files them by event.** On write it does one small thing: it attaches each message to an event and to one episode of that event, plus a one-line summary. Messages about the same event stay together; similar events on different occasions stay apart.
- **Answers from the original words.** When the assistant needs something from the past, it searches the original messages, the same event first, and receives each one with its date, so it can work out what is newer and what is outdated.

Underneath, it is retrieval over the original messages (what is usually called RAG: keywords, plus optional local vectors), with a very light layer of event filing on top. The filing only helps with finding things; what the assistant receives is always the original words.

## Why keep the original words

Many memory products summarize conversations, or extract a handful of facts, and store those. We do the opposite: the original words are kept as they are, and the filing is only an index.

- **When a summary is wrong, everything built on it is wrong too**, and you can't tell which message it came from. Original words carry their dates, so you can check them yourself.
- **Public research points the same way.** The LongMemEval paper compared the two: giving the model summaries or extracted facts instead of the original messages lowers accuracy, except on questions that aggregate across sessions; using extracted facts only to help retrieval, while still handing over the original messages, improved both recall and accuracy.
- **Retrieval over original words is far from exhausted.** In our own tests, original-message retrieval plus light filing tied with Mem0. But a question usually needs only two or three original messages, while we hand the model several hundred. Finding them more completely and handing them over more precisely is what comes next.

## Supported assistants

| Assistant | Status |
|---|---|
| Hermes | Available in the preview (installed as Hermes's external memory plugin) |
| Claude Code, Codex, Pi | Coming in later updates |

**Platform: tested on Mac only** (macOS on Apple M-series chips). Windows and Linux are untested. Known possible problems on Windows: the file lock for concurrent writes does not work, command-line output may be garbled or fail, and a black console window may flash while organizing. These will be fixed once it has been tested on a real Windows machine. On systems other than macOS, the wizard says so first.

## Install

Requires Python 3.11 or later and an existing Hermes install.

We recommend a separate environment for Yuanwei at `~/.yuanwei/venv` (next to the built-in vector engine's `~/.yuanwei/engine/`, out of everything else's way). The commands below use full paths, so you don't need to add `yuanwei` to your PATH:

```
python3 -m venv ~/.yuanwei/venv
~/.yuanwei/venv/bin/pip install yuanwei
~/.yuanwei/venv/bin/yuanwei setup
```

You can also skip the environment and use `python3 -m pip install yuanwei` (don't type plain `pip`: some machines only have `pip3`); at the end, the wizard tells you the full path of `yuanwei`.

`yuanwei setup` is a question-and-answer wizard (in Chinese): check the environment → pick a preset → pick the organizing model → set the key (input is hidden; it is stored in a file on your computer that only you can read, shared by all your bots) → choose whether to turn on local vectors → show the plan → change nothing until you confirm.

| Preset | Organizing | Retrieval |
|---|---|---|
| Recommended (推荐) | DeepSeek (organizing tested) | Keywords + local vectors (needs a model download of about 600 MB, only after asking you; vectors: LM Studio is the tested setup; the built-in program is almost identical to it but did not pass our own strict standard) |
| Light (轻量) | DeepSeek | Keywords only |
| Custom (自选) | Your own provider and model (untested) | Your choice |
| All local (全本机, experimental) | A local LLM | Local vectors |

Local vectors come from one of two sources. **Built-in** (Apple M-series Macs): Yuanwei prepares the vector program and model for you, asks before downloading and tells you the size, then checks the download against the checksums pinned in this release; you don't need to install anything else. Or **LM Studio / Ollama**, if you already have them. **Only LM Studio is tested.** The built-in program uses the same model file, and its vectors are almost identical to LM Studio's: in a full check, 8 of 11,588 sentences fell slightly below our threshold, the lowest at 0.9977, none below 0.995. It did not pass our own strict standard, so it is not labeled "tested".

You can also let your assistant install it: give `docs/让助手来装.md` to Hermes. The assistant only shows you a plan first; you set the key yourself in a terminal.

The full guide (install, use, check status, uninstall; what each step changes and how to undo it) is `docs/装到Hermes上.md`, in Chinese.

## You stay in control

- **No memory in this window.** Say "don't use memory" (别用记忆), or put `#yuanwei-nomem` on the first line of a message, and this conversation window stops searching Yuanwei's memory until you turn it back on. Turning it back on needs your own confirmation: reply exactly 「确认开启」 ("confirm enable"), or put `#yuanwei-mem` on the first line. The assistant cannot confirm for you. Only the commands are guaranteed; plain language relies on the assistant understanding it, and how reliably it does has not been measured.
- **Delete a conversation.** Tell your assistant; it is deleted only after you reply exactly 「确认删除」 ("confirm delete"). You can also delete it from the command line. The conversation's original messages are removed from Yuanwei, and the filing text that depends on them is cleared too. Deleted content cannot be recovered.
- **Keys in your messages are masked first.** Recognizable API keys and passwords are masked before anything is stored, so they never enter memory and are never sent to the organizing model. Formats it doesn't recognize may slip through.
- **Stored on your own computer.** The memory store is a local SQLite file. Organizing sends conversations to the model service you configured; by default the vector service only connects to your own machine.
- **Only your own words.** Other people's messages in groups, and messages from bots, are not recorded.
- **One store per bot.** Each new bot you create in Hermes needs Yuanwei installed once more (run the wizard for that bot's directory). Until then, settings copied from elsewhere stay inactive and never read or write the main Hermes store.
- **One organizing key for all bots.** All your bots share it, so you change it once (`yuanwei key set`). If one bot needs a different key, set it for that bot alone (`yuanwei key set --this-bot-only --hermes-home <its directory>`). To keep the shared key somewhere else, set `YUANWEI_KEY_DIR`: absolute paths only; the directory is made readable only by you, so use one meant just for keys; your terminal and the Hermes service must use the same setting. `yuanwei key status` only reports where the key comes from; it never shows the key itself.
- **Hermes's own memory stays on.** Yuanwei takes only Hermes's single "external memory plugin" slot. The "no memory" switch above applies to Yuanwei only, not to Hermes's built-in notes.

## How it tested

We wrote a long story in Chinese, spread over 150 conversations, and two batches of questions, 55 each. Three memory systems each organized it, then the same answering model (DeepSeek) answered every question with the same material budget (16,000 tokens). Fully correct answers (2026-11-02):

| System | After conversation 48 | After conversation 150 |
|---|---|---|
| Yuanwei | 50 | 43 |
| Mem0 | 49 | 43 |
| Hindsight | 47 | 34 |
| No memory system: all earlier original messages given straight to the model (control, no budget limit) | 54 | not tested |

Limits:
- This release only covers one-to-one chats between you and a single bot. Group chats and messages between bots are neither recorded nor searched; to have something decided in a group remembered, say it once in your one-to-one chat.
- The story and questions are our own, not a public benchmark.
- Each system ran once.
- The grader knew which systems were being compared. When a different grader regraded the answers, Mem0 scored 4 more than Yuanwei on the conversation-150 batch.
- With the way the preview writes memory now, a re-answer of the same questions on November 10, graded separately, scored 53 and 46. Those numbers are not directly comparable with the table above.

Results on public benchmarks, run-to-run variation, cost and speed will follow.

## Documentation

The source code and these documents are on GitHub: https://github.com/guzhijun40134679/yuanwei (the documents are in `docs/`, in Chinese for now).

- `docs/装到Hermes上.md`: the full user guide.
- `docs/让助手来装.md`: give it to Hermes and let Hermes install Yuanwei.
- `docs/产品规则表.md`: every behavior rule and its limits.
- `docs/开发说明.md`: for people changing the code or writing host adapters.
- `docs/复制关系表.md`: which code was copied from the research directory.

## Credits

Designed and directed by Junzhi Gu. The code was written with Claude ([Anthropic](https://github.com/anthropics)); the algorithms, architecture and code were reviewed with GPT through Codex ([OpenAI](https://github.com/openai)).

## License

Source-available under PolyForm Noncommercial 1.0.0: free for personal, learning, research and other non-commercial use; commercial use needs the copyright holder's permission first. Full text in `LICENSE`. Copyright holder: Junzhi Gu (顾钧之).
