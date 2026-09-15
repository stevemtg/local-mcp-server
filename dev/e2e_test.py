"""End-to-end test: spawn server.py over stdio and exercise all tools.

Requires a local Ollama with 'nomic-embed-text' running on localhost:11434.
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters, ClientSession
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


SAMPLE_PY = '''
"""Sample project."""


def compute_retry_delay(
    attempt: int,
) -> float:
    """Exponential backoff with jitter."""
    import random

    return min(60.0, (2**attempt) + random.random())


class UserRepository:
    def find_user_by_email(self, email: str):
        return db.query("SELECT * FROM users WHERE email = ?", (email,))
'''

SAMPLE_TS = """
export function parseConfig(raw: string) {
  return JSON.parse(raw);
}
"""


async def main() -> None:
    sample = Path(tempfile.mkdtemp(prefix="sample_project_"))
    (sample / "app.py").write_text(SAMPLE_PY, encoding="utf-8")
    (sample / "config.ts").write_text(SAMPLE_TS, encoding="utf-8")

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            assert set(t.name for t in tools.tools) == {
                "index_codebase",
                "semantic_search",
                "semantic_search_similar",
                "get_symbol_context",
                "get_index_status",
            }

            async def call(name, **kwargs):
                result = await session.call_tool(name, kwargs)
                out = []
                for block in result.content:
                    if getattr(block, "text", None):
                        try:
                            out.append(json.loads(block.text))
                        except json.JSONDecodeError:
                            out.append(block.text)
                return out

            print("\n--- get_index_status (before) ---")
            print(json.dumps(await call("get_index_status", directory_path=str(sample)), indent=2)[:600])

            print("\n--- index_codebase ---")
            print(json.dumps(await call("index_codebase", directory_path=str(sample)), indent=2))

            print("\n--- index_codebase (again: should skip all) ---")
            print(json.dumps(await call("index_codebase", directory_path=str(sample)), indent=2))

            print("\n--- semantic_search: 'how is exponential backoff computed' ---")
            res = await call("semantic_search", query="how is exponential backoff computed", directory_path=str(sample), limit=3)
            top = res[0]["results"][0] if res and res[0].get("results") else {}
            print(json.dumps({"top_symbol": top.get("symbol"), "top_path": top.get("path"),
                              "lines": [top.get("startLine"), top.get("endLine")],
                              "score": top.get("score"), "reason": top.get("reason")}, indent=2))
            assert top.get("symbol") == "compute_retry_delay", top

            print("\n--- semantic_search with path_filter='*.ts' ---")
            res = await call("semantic_search", query="parse config json", directory_path=str(sample), path_filter="*.ts", limit=3)
            top = res[0]["results"][0] if res and res[0].get("results") else {}
            print("filtered top:", top.get("path"), top.get("symbol"), top.get("score"))
            assert top.get("path") == "config.ts", res

            print("\n--- semantic_search_similar: similar to app.py:5-13 ---")
            res = await call("semantic_search_similar", directory_path=str(sample), path="app.py", start_line=5, end_line=13, limit=3)
            top = res[0]["results"][0] if res and res[0].get("results") else {}
            print("similar top:", top.get("path"), top.get("symbol"), top.get("score"))
            assert top.get("path"), res  # returns something; same-range chunks were excluded

            print("\n--- get_symbol_context: app.py:11 (compute_retry_delay) ---")
            res = await call("get_symbol_context", directory_path=str(sample), path="app.py", line=11)
            ctx = res[0]
            print(json.dumps(ctx, indent=2)[:1500])
            assert ctx["path"] == "app.py", ctx
            assert ctx["absolutePath"] == str(sample / "app.py"), ctx
            assert ctx["requestedLine"] == 11 and ctx["language"] == "python"
            assert ctx["symbol"]["name"] == "compute_retry_delay", ctx["symbol"]
            assert ctx["symbol"]["kind"] == "function", ctx["symbol"]
            assert [ctx["symbol"]["startLine"], ctx["symbol"]["endLine"]] == [5, 13], ctx["symbol"]
            assert ctx["imports"] == [{"line": 9, "text": "import random"}], ctx["imports"]
            assert ctx["references"] == []  # only occurrence is the excluded definition
            assert ctx["relatedTests"] == []
            assert ctx["indexState"] == "current", ctx["indexState"]
            assert ctx["note"] == (
                "Live source is authoritative. References are lexical matches, "
                "not proven callers or callees."
            )
            print("(get_symbol_context answered without any Ollama request)")

            print("\n--- get_index_status (after) ---")
            print(json.dumps(await call("get_index_status", directory_path=str(sample)), indent=2))

    shutil.rmtree(sample, ignore_errors=True)
    print("\nE2E TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())

