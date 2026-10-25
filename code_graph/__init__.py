"""Code Graph — structural cross-file relationship memory beside the vector index.

Decoupled from the embedding pipeline: nothing here contacts Ollama or reads
index.db. The package provides the Repository Graph Layer (extractors.py +
graph_store.py) and the Code-Aware Structural Router (router.py), exposed to
MCP clients through tool.py.
"""
