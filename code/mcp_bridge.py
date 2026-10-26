# mcp_bridge.py —— 桥：MCP server 的工具 → LangChain 工具（会话活到服务结束）
import os
import sys
from contextlib import AsyncExitStack

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import create_model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVER = StdioServerParameters(
    command=sys.executable,                              # 同一个解释器（容器里就是容器那个）
    args=[os.path.join(BASE_DIR, "mcp_server.py")],
)

# JSON Schema 类型 → Python 类型
TYPE_MAP = {"integer": int, "number": float, "string": str,
            "boolean": bool, "array": list, "object": dict}


def json_schema_to_model(tool_name: str, schema: dict):
    """① MCP 的 JSON Schema 参数描述 → pydantic 参数模型（LangChain 要的形状）"""
    props = schema.get("properties") or {}            # 取得所有参数定义。
    required = set(schema.get("required") or [])      # 读取必填参数
    fields = {}
    for key, spec in props.items():
        py_type = TYPE_MAP.get(spec.get("type"), str)  # 如果：spec.get("type") == "string"那么：py_type == str,其余默认str
        fields[key] = (py_type, ... if key in required else None)   # ...的意思是这个参数是必填的
    return create_model(f"{tool_name}Args", **fields)   # create_model 可以动态创建 Pydantic 模型


def make_lc_tool(session, mcp_tool) -> StructuredTool:
    """② 一个 MCP 工具 → 一个 LangChain 工具"""
    async def _run(**kwargs) -> str:    # **kwargs 表示接收任意多个关键字参数，全部收集到字典 kwargs 里，可以处理多个不同工具
        result = await session.call_tool(mcp_tool.name, kwargs)  # 使用 MCP 会话，调用服务器上的这个工具，并把参数传过去。
        return "\n".join(getattr(c, "text", str(c)) for c in result.content)
        # 如果 c 有 text 属性，就取它；否则把 c 转成字符串。然后把多个内容拼成一个字符串

    return StructuredTool.from_function(
        coroutine=_run,
        name=mcp_tool.name,
        description=mcp_tool.description or "",
        args_schema=json_schema_to_model(mcp_tool.name, mcp_tool.input_schema),
    )       # 这里把 MCP 工具的资料填进 LangChain 工具，但工具真正执行时，仍然会通过 MCP 调用远程 Server



_stack = None


async def startup():
    """服务启动时调用：拉起 MCP server 子进程 → 建会话 → 返回 LangChain 工具"""
    global _stack
    _stack = AsyncExitStack()
    read, write = await _stack.enter_async_context(stdio_client(SERVER))  # 通过 SERVER 配置启动 MCP Server 子进程，并取得读写管道
    session = await _stack.enter_async_context(ClientSession(read, write))  # 使用刚才的读写管道创建 MCP 会话，并把这个会话也交给 stack 管理
    await session.initialize()
    listed = await session.list_tools()
    tools = [make_lc_tool(session, t) for t in listed.tools]  # # 最终得到一组 LangChain 工具
    print(f"🔌 [MCP] 连上 server，拿到 {len(tools)} 个工具：{[t.name for t in tools]}")
    return tools


async def shutdown():
    """服务关闭时调用：关掉子进程和会话"""
    global _stack
    if _stack is not None:
        await _stack.aclose()
        _stack = None