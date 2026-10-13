# mcp_server.py —— S5 的 MCP server：工具住在独立进程里
from datetime import datetime

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("s5-tools")


@mcp.tool()
def reverse_text(text: str) -> str:
    """把传入的字符串倒过来返回"""
    return text[::-1]


@mcp.tool()
def char_count(text: str) -> int:
    """返回字符串的字符个数"""
    return len(text)


@mcp.tool()
def now() -> str:
    """返回当前日期和时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    mcp.run()