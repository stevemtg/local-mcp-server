"""Opt-in Ollama/ComfyUI GPU-handoff orchestration for the MCP server.

Runtime home of the ``generate_image`` feature designed in
``docs/comfyui_orchestration/PLAN.md`` and prototyped in ``reference.py``.
Deployment contract: every GPU user (the agent host, MCP image tools, and
embedding/search requests) participates in the same cooperative file lease and
durable recovery journal. The feature stays disabled while ``IMAGE_MODEL`` is
unset; ``server.py`` then neither registers ``generate_image`` nor gates the
embedding tools.
"""

from .errors import Fault
from .journal import Journal
from .lease import InferenceGate, file_lease, gpu_session
from .orchestrator import Orchestrator
from .settings import Settings, load_settings
from .tool import register

__all__ = [
    "Fault",
    "InferenceGate",
    "Journal",
    "Orchestrator",
    "Settings",
    "file_lease",
    "gpu_session",
    "load_settings",
    "register",
]
