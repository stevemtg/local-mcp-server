import inspect
import sys

print("python:", sys.version)
print("prefix:", sys.prefix)

import mcp.server.mcpserver as m

print("module file:", getattr(m, "__file__", None))
print("package path:", getattr(m, "__path__", None))
from mcp.server.mcpserver import MCPServer

print("MCPServer:", MCPServer.__module__)
print("init params:", list(inspect.signature(MCPServer.__init__).parameters))
print("exports:", sorted(getattr(m, "__all__", dir(m)))[:40])
import image_orchestration.tool

print("tool import ok:", image_orchestration.tool.__file__)
