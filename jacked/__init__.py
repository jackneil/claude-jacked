"""
Claude Jacked - Smart reviewers, commands, and session search for Claude Code.

Base install provides agents, commands, behavioral rules, and a web dashboard.
Install extras for additional features:
  pip install "claude-jacked[search]"    — session search via Qdrant
  pip install "claude-jacked[security]"  — security gatekeeper hook
  pip install "claude-jacked[all]"       — everything
"""

__version__ = "0.6.1"


def _qdrant_available() -> bool:
    """Check if qdrant-client is installed."""
    try:
        import qdrant_client  # noqa: F401
        return True
    except ImportError:
        return False


def __getattr__(name: str):
    """Lazy imports for backwards compat — only works if [search] extra installed."""
    _search_classes = {
        "SmartForkConfig": "jacked.config",
        "QdrantSessionClient": "jacked.client",
        "SessionIndexer": "jacked.indexer",
        "SessionSearcher": "jacked.searcher",
        "SessionRetriever": "jacked.retriever",
    }
    if name in _search_classes:
        import importlib
        module = importlib.import_module(_search_classes[name])
        return getattr(module, name)
    raise AttributeError(f"module 'jacked' has no attribute {name!r}")


__all__ = [
    "__version__",
    "_qdrant_available",
]
