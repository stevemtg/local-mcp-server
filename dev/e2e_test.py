"""End-to-end test: spawn server.py over stdio and exercise all tools.

Requires a local Ollama with 'nomic-embed-text' running on localhost:11434.
"""

import asyncio
import json
import re
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
    csharp_source = (ROOT / "dev" / "fixtures" / "scopes.cs").read_text(encoding="utf-8")
    (sample / "Renderer.cs").write_text(csharp_source, encoding="utf-8")

    params = StdioServerParameters(command=sys.executable, args=["-B", str(ROOT / "server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert "semantic_search_similar" in initialized.instructions
            assert "get_symbol_context" in initialized.instructions
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            assert set(t.name for t in tools.tools) == {
                "index_codebase",
                "semantic_search",
                "semantic_search_similar",
                "get_symbol_context",
                "get_index_status",
            }
            examples = {}
            for tool in tools.tools:
                if tool.name in ("semantic_search", "semantic_search_similar", "get_symbol_context"):
                    definition = tool.model_dump(by_alias=True)
                    assert definition["annotations"]["readOnlyHint"] is True
                    assert definition["annotations"]["openWorldHint"] is False
                    properties = definition["inputSchema"]["properties"]
                    assert all(p.get("description") for p in properties.values())
                    if tool.name == "get_symbol_context":
                        assert properties["line"]["minimum"] == 1
                        assert properties["max_references"]["default"] == 12
                        assert "maximum" not in properties["max_references"]  # clamped, not rejected
                    else:
                        assert properties["limit"]["minimum"] == 1
                        assert properties["limit"]["maximum"] == 50
                        assert "get_symbol_context" in tool.description
                    example = re.search(r"Example arguments:\s*(\{[^\n]+\})", tool.description)
                    assert example, tool.name
                    arguments = json.loads(example.group(1))
                    assert set(arguments) <= properties.keys(), (tool.name, arguments)
                    assert set(definition["inputSchema"]["required"]) <= arguments.keys()
                    examples[tool.name] = {**arguments, "directory_path": str(sample)}

            # The Cline rule examples must use the same valid input names as
            # the tool descriptions (output startLine is not input start_line).
            rules = (ROOT / ".clinerules/10-semantic-code-search.md").read_text(encoding="utf-8")
            for raw in re.findall(r"```json\s*(.*?)\s*```", rules, re.DOTALL):
                arguments = json.loads(raw)
                name = "semantic_search" if "query" in arguments else (
                    "get_symbol_context" if "line" in arguments else "semantic_search_similar"
                )
                assert {**arguments, "directory_path": str(sample)} == examples[name]

            async def call(name, **kwargs):
                result = await session.call_tool(name, kwargs)
                assert not result.is_error, result
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

            print("\n--- searches on an unindexed project remain read-only ---")
            for name, arguments in (
                ("semantic_search", {"query": "retry requests"}),
                ("semantic_search_similar", {"path": "app.py", "end_line": 13}),
            ):
                ctx = (await call(name, directory_path=str(sample), **arguments))[0]
                assert ctx["results"] == [] and "index_codebase" in ctx["note"], ctx
            assert not (sample / ".semantic_index").exists()

            print("\n--- direct symbol-context example needs no index ---")
            ctx = (await call("get_symbol_context", **examples["get_symbol_context"]))[0]
            assert ctx["symbol"]["name"] == "compute_retry_delay", ctx
            assert ctx["indexState"] == "not_indexed", ctx
            assert not (sample / ".semantic_index").exists()
            # Preserve the documented clamping behavior through MCP as well.
            await call("get_symbol_context", **examples["get_symbol_context"], max_references=0)

            print("\n--- invalid search inputs are actionable MCP errors ---")
            for name, arguments in (
                ("semantic_search", {"query": "   "}),
                ("semantic_search", {"query": "retry", "limit": 0}),
                ("semantic_search", {"query": "retry", "min_score": 1.1}),
                ("semantic_search_similar", {"path": "app.py", "start_line": 13, "end_line": 5}),
                ("semantic_search_similar", {"path": "app.py", "end_line": 9999}),
                ("semantic_search_similar", {"path": str(sample / "app.py")}),
            ):
                error = await session.call_tool(name, {"directory_path": str(sample), **arguments})
                assert error.is_error, error
            assert not (sample / ".semantic_index").exists()

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

            # Add a known analogous implementation, so similarity must find
            # relevant code rather than merely return any non-empty result.
            retry_source = SAMPLE_PY.split("class UserRepository:")[0].replace(
                "compute_retry_delay", "calculate_wait"
            )
            (sample / "retry_worker.py").write_text(retry_source, encoding="utf-8")
            indexed = (await call("index_codebase", directory_path=str(sample)))[0]
            assert indexed["files_embedded"] == 1 and indexed["files_failed"] == 0, indexed

            print("\n--- advertised search examples execute through MCP ---")
            for name in ("semantic_search", "semantic_search_similar"):
                response = (await call(name, **examples[name]))[0]
                assert response["results"], (name, response)

            print("\n--- semantic_search_similar: similar to app.py:5-13 ---")
            res = await call("semantic_search_similar", directory_path=str(sample), path="app.py", start_line=5, end_line=13, limit=3)
            top = res[0]["results"][0] if res and res[0].get("results") else {}
            print("similar top:", top.get("path"), top.get("symbol"), top.get("score"))
            assert top.get("path") == "retry_worker.py" and top.get("symbol") == "calculate_wait", res
            assert res[0]["range"] == [5, 13], res
            assert all(h["path"] != "app.py" or h["startLine"] > 13 or h["endLine"] < 5
                       for h in res[0]["results"]), res

            res = await call("semantic_search_similar", directory_path=str(sample),
                             path="app.py", end_line=13, limit=3)
            assert res[0]["range"] == [1, 13], res
            assert res[0]["results"][0]["path"] == "retry_worker.py", res
            res = await call("semantic_search_similar", directory_path=str(sample),
                             path="app.py", limit=3)
            assert res[0]["range"] is None
            assert all(h["path"] != "app.py" for h in res[0]["results"]), res
            assert len({(h["path"], h["startLine"], h["endLine"]) for h in res[0]["results"]}) == len(res[0]["results"])

            (sample / "new_retry.py").write_text(retry_source, encoding="utf-8")
            res = await call("semantic_search_similar", directory_path=str(sample),
                             path="new_retry.py", limit=1)
            assert res[0]["results"][0]["path"] in ("retry_worker.py", "app.py"), res

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

            print("\n--- get_symbol_context: Renderer.cs:23 (Render) ---")
            ctx = (await call("get_symbol_context", directory_path=str(sample),
                              path="Renderer.cs", line=23))[0]
            assert ctx["symbol"] == {
                "name": "Render", "kind": "method", "startLine": 23,
                "endLine": 33, "declaration": "private void Render(bool enabled)",
            }, ctx
            assert ctx["references"] == [
                {"path": "Renderer.cs", "line": 20, "preview": "Render(true);"},
                {"path": "Renderer.cs", "line": 37, "preview": "Render(false);"},
            ], ctx
            assert ctx["imports"] == [
                {"line": 1, "text": "using System;"},
                {"line": 2, "text": "using System.Collections.Generic;"},
            ], ctx
            assert ctx["relatedTests"] == [] and ctx["indexState"] == "current", ctx
            print(json.dumps(ctx, indent=2))

            print("\n--- get_index_status (after) ---")
            print(json.dumps(await call("get_index_status", directory_path=str(sample)), indent=2))

    shutil.rmtree(sample, ignore_errors=True)
    print("\nE2E TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())

