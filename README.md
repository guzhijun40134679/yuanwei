# Yuanwei (原委)

English | [简体中文](https://github.com/guzhijun40134679/yuanwei/blob/main/README.zh-CN.md)

**Every memory has its origin; every quote has its source.** Long-term memory for AI assistants.

Website: https://yuanwei.dev

> Preview release · research prototype; some features are still being developed and tested. On 2026-11-11 it was installed on Hermes v0.22.0 (+10397) and every item on that checklist passed; what the checklist did not cover is listed under the limits below. 0.1.0a10 was also regression-tested on an isolated Hermes v0.23.2 with a fake model and no network (about 180 scenarios through the gateway the desktop app uses and through Hermes's WeChat adapter, with no regressions against 0.1.0a9), and used for a day on the author's Mac in the desktop app (and for two messages on WeChat the next day) without losing a turn. Telegram, Discord, Slack and other chat platforms have not been tried on a real install.

> **Language:** the setup wizard, the command-line messages and the detailed guides in `docs/` are currently in Chinese only.

## What it does

- **Keeps your words verbatim.** What you and your assistant said is stored exactly as it was said, with who said it and on which day. Nothing is rewritten or summarized.
- **Files them by event.** On write it does one small thing: it attaches each message to an event and to one episode of that event, plus a one-line summary. Messages about the same event stay together; similar events on different occasions stay apart.
- **Answers from the original words.** When the assistant needs something from the past, it searches the original messages, the same event first, and receives each one with its date, so it can work out what is newer and what is outdated.
- **New messages need not wait for filing.** Committed messages are searchable by keyword immediately, including while vectors are pending under the installed default. Recall and automatic notes exclude only the current turn; earlier messages of the same session, filed or not, stay searchable. Result notices list unfiled message IDs without changing the quoted words.
- **Image turns are saved too.** User text and the assistant's reply are saved with an image-count marker, including image-only turns. Images and host-added cache-path hints are not stored. A default question Hermes inserts for an image-only turn may be retained because its interface cannot distinguish it from the same words typed by the user.
- **Your answers to the assistant's question cards are saved too.** When the assistant asks you questions on a card (Hermes's `clarify` tool) on your own computer or in a one-to-one chat Yuanwei has checked against Hermes v0.23.2's code (a private chat on WeChat, Telegram or Discord, a Hermes API session under session authority, or a Slack direct message), your picks and typed answers are folded into your message for that turn, one tagged line each: the question, the options it offered, and whether you picked an option or wrote your own answer. Unanswered and skipped questions are not saved. Card answers follow the same rules as messages you type: only keys and passwords written in a recognisable form (`sk-…`, `XXX_KEY=…`, 「密码是 …」, `password=…`) are masked; a bare password typed as a card answer is stored as typed. Card answers are not saved in shared chats (Discord server threads, Slack group DMs), because anyone there may have answered. They are not saved yet in chat apps Yuanwei has not checked (WhatsApp, Signal, Matrix, Feishu, email and others), even one-to-one. On Telegram and similar apps, if a question has options and you type your own words without first tapping "✏️ Other (type answer)", the card is cancelled and Hermes treats what you typed as a mid-answer line: from 0.1.0a9 Yuanwei saves those words as a mid-answer line (see below), not as the card's answer, together with the answers you had already picked (tested only on a fake host, not yet on a real Telegram); typing an option's number or its exact text counts as picking it. To write your own answer on the card, tap Other first.
- **What you say while the assistant is still answering is saved too.** If you send another line before the assistant has finished (Hermes calls it a redirect when it takes your line as a correction and carries on from it, and a steer when it slips your line in after a tool result), that line is folded into your message for that turn, one tagged line each, in the order things happened: `（用户中途又说）` ("you also said, mid-answer") for a correction Hermes took as yours, and, on chat apps where Yuanwei can read the origin note Hermes adds, for a steer too; `（中途插入·来源未核实）` ("inserted mid-answer, origin not verified") for a steer whose origin cannot be checked. The desktop app's embedded preview page can send a line to a busy assistant by script, without you clicking anything, and Yuanwei cannot tell that apart from you typing it, so every steer on your own computer is marked unverified; buttons and plug-ins that send a line for you are stored like lines you typed. A "don't use memory" line sent this way closes the window (even an unverified one); "confirm delete", "confirm open" and `#yuanwei-mem` sent this way do not count, and the next turn says so. Not saved: a turn you stopped (Hermes does not hand those over), a steer sent before the turn had any tool result (Hermes inserts it ahead of the turn's opening line), a steer that missed its turn and became the next turn's opening line (on chat apps, with Hermes's origin note attached; a known existing problem), and corrections under the Codex app-server runtime. If Yuanwei cannot tell which turn a line belongs to, it saves the opening line and the reply and none of the mid-answer lines. These lines follow the same chat rules as card answers: in shared chats, and in chat apps Yuanwei has not checked (even one-to-one), a turn that has them is not saved (in one rare case only the opening line and the reply are). The assistant's `status` tool now also shows how many turns started, were saved and were not, in this conversation. Known limits are listed in [`docs/装到Hermes上.md`](https://github.com/guzhijun40134679/yuanwei/blob/main/docs/装到Hermes上.md) (Chinese). This was first written and tested on a fake host. On 2026-11-25 a set of scenarios was run on an isolated real Hermes v0.23.2 (fake model, no network) to see which turns get lost or recorded wrongly; 0.1.0a10 fixes part of what that found (see the 0.1.0a10 section of that page) and was then rerun on the same setup: about 180 scenarios, no regressions against 0.1.0a9. Telegram, Discord, Slack and other chat platforms have not been tried on a real install.
- **Higher-ranked material comes first.** Search results are paged in retrieval order, then shown chronologically within each page. A hit and its accompanying context stay on the same page. Across workspaces, local ranks are interleaved and each section names its source. A group too large for a page returns an explicit error; it is not silently split or dropped.
- **A recall hands over about 8,000 tokens by default, and up to 24,000 on request.** Without a budget, one search selects about 8,000 estimated tokens in total (counted by the same measure for Chinese and English); if the assistant asks for a larger budget, the cap is 24,000. A message too long for the default is retried once at 24,000, and if it still does not fit, the answer says the relevant message was found but is too long, instead of "no related records found".
- **Returns nothing when nothing qualifies.** A search admits a message only if it contains a keyword hit or, with local vectors on and a calibrated vector model, its meaning is close enough (a cosine score at or above a floor measured for that model). If no message qualifies, the result is "no related records found", with no material and no continuation hint. Each quote says how it was found: keyword match, similar meaning only (with its cosine score), or neighboring or same-episode context. Quotes are never rewritten.

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

`yuanwei setup` is a question-and-answer wizard (in Chinese): check the environment → pick a preset → pick the organizing model (or choose "no organizing": no key, no cost; original messages are still stored and searched, and you can turn organizing on later with `yuanwei organize on --yes`) → set the key (input is hidden; it is stored in a file on your computer that only you can read, shared by all your bots) → choose whether to turn on local vectors → choose the reading scope and register your own account on each detected chat platform (you can skip one; unregistered platforms stay unrecorded) → show the plan → change nothing until you confirm. Detection only checks configuration: it cannot prove a platform is online and may miss one. You can register a known platform manually.

| Preset | Organizing | Retrieval |
|---|---|---|
| Recommended (推荐) | DeepSeek (organizing tested) | Keywords + local vectors (needs a model download of about 600 MB, only after asking you; vectors: LM Studio is the tested setup; the built-in program is almost identical to it but did not pass our own strict standard) |
| Light (轻量) | DeepSeek | Keywords only |
| Custom (自选) | Your own provider and model (untested) | Your choice |
| All local (全本机, experimental) | A local LLM | Local vectors |

Local vectors come from one of two sources. **Built-in** (Apple M-series Macs): Yuanwei prepares the vector program and model for you, asks before downloading and tells you the size, then checks the download against the checksums pinned in this release; you don't need to install anything else. Or **LM Studio / Ollama**, if you already have them. **Only LM Studio is tested.** The built-in program uses the same model file, and its vectors are almost identical to LM Studio's: in a full check, 8 of 11,588 sentences fell slightly below our threshold, the lowest at 0.9977, none below 0.995. It did not pass our own strict standard, so it is not labeled "tested".

You can also let your assistant install it: give `docs/让助手来装.md` to Hermes. The assistant only shows you a plan first; you set the key yourself in a terminal.

The full guide (install, use, check status, uninstall; what each step changes and how to undo it) is `docs/装到Hermes上.md`, in Chinese.

A recall shares each query vector across workspaces with the same full vector identity. New configurations use a 10-second query timeout. Existing explicit values are preserved; status warns if the timeout is below 10 seconds. To change an existing 5-second setting, edit `timeout_query = 10` under `[embedding]` in that bot's `yuanwei.toml`, then restart Hermes. A timeout still falls back to keywords under the installed `partial` mode and records the reason.

## You stay in control

- **No memory in this window.** Say "don't use memory" (别用记忆), or put `#yuanwei-nomem` on the first line of a message, and this conversation window stops searching Yuanwei's memory until you turn it back on. Turning it back on needs your own confirmation: reply exactly 「确认开启」 ("confirm enable"), or put `#yuanwei-mem` on the first line. The assistant cannot confirm for you. On your own computer and in the one-to-one chats listed above, words you type on a question card count like a message: a command or plain words that turn memory off do so from the next turn. On Telegram, Discord and Slack this holds only after you tap Other, or on a card without options. Options you pick never do. In any other chat a command typed on a card does nothing, so send `#yuanwei-nomem` as a normal message after the card closes. A card answer can never turn memory back on or confirm a deletion; send 「确认开启」 or 「确认删除」 as a normal message after the card closes. Only the commands are guaranteed; plain language relies on the assistant understanding it, and how reliably it does has not been measured.
- **Delete a conversation.** Tell your assistant; it is deleted only after you reply exactly 「确认删除」 ("confirm delete"). You can also delete it from the command line. The conversation's original messages are removed from Yuanwei, and the filing text that depends on them is cleared too. Deleted content cannot be recovered. Deleting a conversation in Hermes does not delete it from Yuanwei: its messages can still be found, so delete it in Yuanwei too, best from inside that conversation before you delete it in Hermes (afterwards, find its ID with `yuanwei conversations list hermes`, then run `yuanwei conversations delete hermes <id> --yes`). Lines taken back in Hermes (regenerating, editing and resending, `/undo`) stay in Yuanwei, and nothing marks which version was replaced.
- **Keys in your messages are masked first.** Keys and passwords written in a recognisable form (`sk-…`, `XXX_KEY=…`, 「密码是 …」, `password=…`) are masked before anything is stored, so they never enter memory and are never sent to the organizing model. Formats it doesn't recognize may slip through (for example 「密码是」 followed by a password written in Chinese characters without quotes), and a bare password with nothing around it (for example one typed as a card answer) is stored as typed.
- **Stored on your own computer.** The memory store is a local SQLite file. Organizing sends conversations to the model service you configured; by default the vector service only connects to your own machine.
- **Chat platforms record only private chats from your registered accounts.** Unregistered accounts, other people and bots cannot record or search memories. In this release, group chats on every chat platform are neither recorded nor searched, including your own messages in a group, with known gaps (fix pending). Yuanwei goes by the chat type Hermes reports, and in these multi-person chats it stays on for your registered account: Discord servers (by default Hermes moves each @mention of the bot in a server channel into a new thread, so most server conversations count; only replies and channels where that is turned off stay blocked), Slack group DMs, LINE multi-person rooms, Matrix rooms Hermes takes for a direct chat (for example an unnamed direct room that has three or more people), and, if you registered yourself for `api_server`, group-chat turns that reach your bot from a Hermes on another machine through the API server (every such turn is saved as yours, whoever in the group wrote it). Your own messages there are recorded, and the assistant can search your memory and reply where everyone in that chat can see it. Keep private matters to a true one-to-one chat. The wizard asks for your account on each platform, and you can skip it. Existing installations can register with `yuanwei owners set <platform> --id <your-ID> --yes`; restart Hermes afterward. For a particular bot, add `--hermes-home <its-directory>`. `set` replaces the platform's entire list; repeat `--id` for multiple accounts of your own. Send `/whoami` in a private chat with Hermes to find your ID; do not use a display name, the bot's account, or a shared or automatic sender ID (such as the API server's `api` or a webhook route). A Hermes allowlist does not establish who owns the memory.
- **One store per bot.** Each new bot you create in Hermes needs Yuanwei installed once more (run the wizard for that bot's directory). Until then, settings copied from elsewhere stay inactive and never read or write the main Hermes store.
- **Projects, including the Hermes desktop app.** Yuanwei files a conversation under the Hermes project whose folder it is in; conversations outside any project go to "life". The Hermes desktop app does not hand the project folder to memory plugins (Hermes issue #144869), so Yuanwei asks Hermes for "this turn's folder" through an internal Hermes function. This is not part of Hermes's plugin interface: if Hermes renames both sources, desktop conversations fall back to "life". Once Hermes fixes #144869, Yuanwei uses the folder Hermes passes directly. This covers only the local desktop app and terminal UI; chat platforms such as Telegram are not asked and still go to "life". This was verified for ordinary desktop main conversations; side and auxiliary sessions (such as `/btw`, and the sessions used for organizing) are not covered, and Yuanwei does not promise they all land in "life" (Hermes marks `/btw` branches `skip_memory=True`, so Yuanwei does not record them, which is Hermes's own design).
- **Organizing can be turned off.** Organizing needs a key and costs money; `yuanwei organize off --yes` turns it off (`on` turns it back on, `status` shows it; without `--yes` it only prints the plan; restart Hermes afterward). While off, messages are still stored and searched, nothing is sent to any model, no key is needed, and results carry no "unfiled" notice; conversations stored while off are organized in order when you turn it on again.
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
- This release only covers one-to-one chats between you and a single bot. Group chats and messages between bots are neither recorded nor searched (except the gaps above); to have something decided in a group remembered, say it once in your one-to-one chat.
- The owner-registration flow and filtering of all group chats added on November 17 were checked offline with fake environments. On WeChat, the author's own install has recorded the registered owner's one-to-one messages correctly; group-chat filtering and the rejection of unregistered senders were run only in the isolated Hermes v0.23.2 through its WeChat adapter (network faked), not on a real chat platform. `status hermes` / `doctor hermes` warn about missing registrations and show cumulative events rejected because the sender was unregistered or was not an owner, by platform. Earlier messages are not counted retroactively; redelivery after a restart can count an event again.
- The relevance floor is not "no answer" detection. It only decides which messages may count as candidates; it cannot tell whether your memory holds the answer, and unrelated questions can still bring back material (in our small constructed test, 6 of 24 unrelated held-out queries still admitted at least one message). The floor was calibrated for one vector model file (Qwen3-Embedding-0.6B-Q8_0) running under LM Studio. A vector model that is not calibrated falls back to keyword hits only, so a question worded differently from the stored words may find nothing; `yuanwei status hermes` and `yuanwei doctor hermes` say so (and `doctor hermes` exits with code 1). The same model file reuses the same threshold under any runtime program, including the built-in vector program; status marks a runtime that is not on the measured list as not measured, and only the LM Studio path was measured (the built-in program was not tested on a real machine). Retrieval cursors created before this upgrade stop working; ask again.
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
