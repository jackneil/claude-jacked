"""
Claude Jacked - Cross-machine semantic search for Claude Code sessions.

This package provides tools to index, search, and retrieve context from past
Claude Code sessions, enabling seamless context sharing across machines.
"""

__version__ = "0.2.1"

from jacked.config import SmartForkConfig
from jacked.client import QdrantSessionClient
from jacked.indexer import SessionIndexer
from jacked.searcher import SessionSearcher
from jacked.retriever import SessionRetriever

__all__ = [
    "SmartForkConfig",
    "QdrantSessionClient",
    "SessionIndexer",
    "SessionSearcher",
    "SessionRetriever",
]
