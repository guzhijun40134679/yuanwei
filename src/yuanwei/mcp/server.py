"""MCP 服务（stdio，官方 SDK 低层 Server）。只是把 tools.ToolSurface 接到协议上；工作区、宿主、上限都来自启动配置，工具参数换不了。

* 返回只放 content（一段文字），不放 structuredContent：重复的结构化正文算进宿主的返回大小，首版明确不放；final_return_text 把两者都算进去，给测试和大小核对用。
* 输入校验自己做（validate_input=False）：SDK 的校验错误会把整个参数回显进错误文字，那样错误就成了不控长的大返回。
"""
import argparse
import asyncio
import os
import secrets
import sys

from .. import hosts as H
from ..access import Access
from ..config import Config
from ..idsource import CallBoundIdentity, ConnectionIdentity, NoIdentity
from ..memory import Memory
from ..rules import RULES, TOOL_DESCRIPTIONS
from ..tools import ALL_TOOLS, ToolSurface

ENV_CONNECTION = "YUANWEI_CONNECTION"
SCHEMAS = {
    "recall": {"type": "object", "properties": {"query": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}, "budget": {"type": "integer"}, "cursor": {"type": "string"}}},
    "get_messages": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}, "before": {"type": "integer"}, "after": {"type": "integer"}, "cursor": {"type": "string"}}},
    "attachments": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    "status": {"type": "object", "properties": {}},
}


def final_return_text(call_result):
    """宿主会数到的最终返回：content 里的全部文字，加上 structuredContent（有的话，序列化后）。"""
    import json
    parts = [c["text"] for c in call_result.get("content") or [] if c.get("type") == "text"]
    sc = call_result.get("structuredContent")
    if sc is not None:
        parts.append(json.dumps(sc, ensure_ascii=False))
    return "".join(parts)


def instance_claimant():
    """这一次进程启动时生成的随机实例标记，用来认领连接句柄。不用 pid：pid 会被复用，旧句柄可能被碰巧同 pid 的另一次启动认领。"""
    return "inst-" + secrets.token_hex(12)


def build_server(surface, mode="tools_only"):
    import mcp.types as types
    from mcp.server.lowlevel import Server
    surface.mode = mode
    server = Server("yuanwei", instructions=RULES[mode] if surface.profile.tools_opened else "这一家宿主暂不支持读取长期记忆（拿不到可信的会话身份）；只有 status 可用，用来看原因。")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=n, description=TOOL_DESCRIPTIONS[n], inputSchema=SCHEMAS[n], _meta={"anthropic/alwaysLoad": True}) for n in ALL_TOOLS if n in surface.tool_names()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            m = server.request_context.meta
            meta = m.model_dump(by_alias=True) if m is not None else {}                          # 宿主放的 _meta（工具参数不是 _meta）
        except Exception:                                                                       # noqa: BLE001
            meta = {}
        ident = await asyncio.to_thread(surface.bind, meta)                                      # 调用到达时先取冻结的身份，再进线程、排队
        r = await asyncio.to_thread(surface.call, name, arguments, ident)
        return types.CallToolResult(content=[types.TextContent(type="text", text=r.text)], isError=r.is_error)
    return server


async def serve(server):
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (rs, ws):
        await server.run(rs, ws, server.create_initialization_options())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m yuanwei.mcp", allow_abbrev=False)
    ap.add_argument("--db", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--host", required=True, choices=sorted(H.PROFILES))
    ap.add_argument("--config")
    ap.add_argument("--mode", default="tools_only", choices=sorted(RULES))
    ap.add_argument("--call-binding", action="store_true", help="只给 claude-code、codex：按调用前钩子登记的调用编号认身份（装接头时显式打开；默认读取工具不开放）")
    a = ap.parse_args(argv)
    cfg = Config.load(a.config) if a.config else Config()
    mem = Memory.open(a.db, a.workspace, config=cfg, spawn="subprocess", config_path=a.config)
    acc = Access(mem)
    prof = H.profile(a.host)
    if a.call_binding:
        prof = H.with_call_binding(prof)
    if prof.identity == "call_bound":
        ids = CallBoundIdentity(acc, a.host)
    elif prof.identity == "launch_bound":
        ids = ConnectionIdentity(acc, os.environ.get(ENV_CONNECTION, ""), instance_claimant())      # 句柄只给这一个进程；认领失败就一直是「没有身份」，全部读取被拒
    else:
        ids = NoIdentity()                                                                              # in_process 的宿主不走独立的 MCP 进程；unproven 的宿主工具不开放
    server = build_server(ToolSurface(mem, acc, prof, ids), a.mode)
    asyncio.run(serve(server))
    return 0


if __name__ == "__main__":
    sys.exit(main())
