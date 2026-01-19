# Smart Fork - Design Document

## Overview

Smart Fork is a cross-machine semantic search system for Claude Code sessions. It solves the problem of losing context when switching between machines or wanting to reference past work without manually searching through session files.

## The Problem

Claude Code stores sessions locally at `~/.claude/projects/{repo}/`. These sessions:
- Are NOT synced across machines
- Get compacted over time (context lost)
- Are hard to search through manually
- Contain valuable context about past work

## The Solution

Smart Fork continuously indexes all sessions to Qdrant Cloud, enabling:
- **Semantic search** - find sessions by what you worked on, not file names
- **Cross-machine access** - any machine can search/retrieve any session
- **Context injection** - load past session transcripts into current conversation
- **Automatic indexing** - Stop hook indexes after every Claude response

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  ANY MACHINE                                                        │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Claude Code                                                  │   │
│  │                                                              │   │
│  │ Stop hook (fires after every response):                     │   │
│  │   → smart-fork index --repo "$CLAUDE_PROJECT_DIR"           │   │
│  │   → Reads current transcript, upserts to Qdrant             │   │
│  │                                                              │   │
│  │ /smart-fork skill:                                          │   │
│  │   → Takes description of what you want                      │   │
│  │   → Searches Qdrant for similar sessions                    │   │
│  │   → You pick one, context gets injected                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                           │ HTTPS
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  QDRANT CLOUD ($30/month tier with Inference)                      │
│                                                                     │
│  • Server-side embedding via Qdrant Cloud Inference                │
│  • Model: sentence-transformers/all-minilm-l6-v2 (384 dims)        │
│  • Stores vectors + full transcript text in payloads               │
│  • Similarity search filtered by repo                              │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. Server-Side Embedding (Qdrant Cloud Inference)

**Decision:** Use Qdrant's server-side embedding instead of local embedding.

**Why:**
- No local dependencies (no sentence-transformers, no PyTorch)
- Faster installation (pip install is quick)
- Consistent embeddings across machines
- Qdrant handles model updates

**How:**
```python
# In QdrantClient constructor
client = QdrantClient(
    url=endpoint,
    api_key=api_key,
    cloud_inference=True,  # THIS IS THE KEY
)

# When upserting points
models.PointStruct(
    id=point_id,
    vector=models.Document(  # NOT a raw vector array
        text=chunk,
        model="sentence-transformers/all-minilm-l6-v2",
    ),
    payload={...},
)

# When searching
client.query_points(
    collection_name=name,
    query=models.Document(  # Same pattern
        text=query_text,
        model="sentence-transformers/all-minilm-l6-v2",
    ),
)
```

**Gotchas:**
- Must use `models.Document`, not raw vectors
- Model name must be exact: `sentence-transformers/all-minilm-l6-v2`
- Requires paid Qdrant Cloud tier ($30/month minimum)

### 2. Two Types of Points Per Session

**Intent Points** - For searching:
- Just USER messages concatenated
- Chunked to ~400 tokens (model limit is 512)
- Multiple intent points per session for long conversations
- Payload includes: repo info, machine, timestamp, content hash

**Chunk Points** - For retrieval:
- Full transcript (user + assistant) chunked to ~4KB
- Used when retrieving session for context injection
- Includes the actual text content

**Why separate:**
- Search should match on what YOU asked, not Claude's verbose responses
- Retrieval needs the full conversation for context

### 3. Deterministic Point IDs

**Decision:** Use UUID5 with deterministic seeds.

```python
point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}_intent_{i}"))
```

**Why:**
- Re-indexing same session produces same IDs
- Upsert naturally updates existing points
- No duplicate points accumulating

### 4. Content Hash for Change Detection

**Decision:** Store SHA256 hash of full transcript in payload.

```python
content_hash = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
```

**Why:**
- Skip re-indexing unchanged sessions
- Backfill is fast (only indexes new/changed sessions)
- Stop hook can quickly skip if nothing changed

### 5. Stop Hook (Not PreCompact)

**Decision:** Use Stop hook for continuous indexing.

**Why:**
- PreCompact hook is broken (GitHub issue #13572)
- Stop fires after every response - we always have latest
- By the time compaction happens, we already have everything in Qdrant
- Continuous indexing > one-time-before-compaction

**Hook config:**
```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "smart-fork index --repo \"$CLAUDE_PROJECT_DIR\""
      }]
    }]
  }
}
```

### 6. Hybrid Local/Remote Retrieval

**Decision:** Check if session exists locally before falling back to Qdrant.

**Why:**
- Native Claude resume (local) > context injection (remote)
- Native resume preserves Claude's internal state
- Context injection works but Claude "reads" it fresh

**Flow:**
```
Session found in search
    │
    ├── Local file exists? → Suggest: claude --resume <session_id>
    │
    └── No local file → Retrieve from Qdrant, inject as context
```

### 7. Repo ID Hashing

**Decision:** Hash full repo path for repo_id field.

```python
def get_repo_id(repo_path: str) -> str:
    normalized = repo_path.replace("\\", "/").lower().rstrip("/")
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]
```

**Why:**
- Two repos with same name in different locations get different IDs
- Avoids collisions like `/c/Github/my-project` vs `/d/Work/my-project`
- Still human-readable in Qdrant dashboard

## Data Schema

### Collection: `claude_sessions`

**Vector Config:**
- Size: 384 (MiniLM dimension)
- Distance: Cosine
- On-disk: true

**Payload Indexes:**
- `repo_id` (keyword) - for filtering by repo
- `repo_name` (keyword) - for display
- `session_id` (keyword) - for retrieval
- `type` (keyword) - "intent" or "chunk"
- `machine` (keyword) - which machine indexed it

### Intent Point Payload

```python
{
    "type": "intent",
    "repo_id": "a1b2c3d4",
    "repo_name": "hank-autocoder-keyphrases-llm",
    "repo_path": "/c/Github/hank-autocoder-keyphrases-llm",
    "session_id": "533e6824-6fb0-4f12-a406-517d2677734e",
    "machine": "JACKS-ML-DESKTOP",
    "timestamp": "2025-01-18T14:30:00Z",
    "content_hash": "sha256:...",
    "intent_text": "help me fix the anesthesia time handling...",
    "chunk_index": 0,
    "total_chunks": 3,
    "transcript_chunk_count": 17,
}
```

### Chunk Point Payload

```python
{
    "type": "chunk",
    "repo_id": "a1b2c3d4",
    "repo_name": "hank-autocoder-keyphrases-llm",
    "session_id": "533e6824-6fb0-4f12-a406-517d2677734e",
    "chunk_index": 0,
    "total_chunks": 17,
    "content": "USER: help me fix...\nASSISTANT: I'll help...",
}
```

## Lessons Learned

### 1. Windows PowerShell Escaping Hell

Running PowerShell from bash (Claude Code's shell) is a nightmare. Variable expansion breaks in unpredictable ways.

**Solution:** Write Python scripts for complex operations, or use `powershell -NoProfile -Command "..."` with careful escaping.

### 2. Environment Variable Loading

Windows env vars set at Machine level don't propagate to running shells.

**Solution:**
- Created `.env` file with credentials
- Updated config to auto-load from package root
- Use `[Environment]::GetEnvironmentVariable('NAME', 'Machine')` in PowerShell

### 3. Unicode Characters on Windows

Rich library spinners use Unicode that Windows console can't handle.

**Solution:** Replace ✓, ✗, ○ with ASCII [OK], [FAIL], [-]

### 4. Qdrant API Changes

`CollectionInfo.vectors_count` doesn't exist in newer versions.

**Solution:** Use `points_count`, `segments_count`, `indexed_vectors_count` instead.

### 5. Point ID Format

Qdrant requires UUIDs or integers, not arbitrary strings like "session_intent_0".

**Solution:** Use `uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}_intent_{i}")`

## File Structure

```
claude-smart-fork/
├── .env                    # Qdrant credentials (gitignored)
├── .gitignore
├── pyproject.toml
├── README.md
├── docs/
│   └── DESIGN.md          # This file
├── smart_fork/
│   ├── __init__.py
│   ├── cli.py             # Click CLI commands
│   ├── client.py          # Qdrant client wrapper
│   ├── config.py          # Configuration + env loading
│   ├── indexer.py         # Session indexing logic
│   ├── retriever.py       # Session retrieval
│   ├── searcher.py        # Semantic search
│   └── transcript.py      # JSONL parsing + chunking
├── skills/
│   └── smart-fork.md      # Claude Code skill file
└── tests/
    ├── test_config.py
    └── test_transcript.py
```

## Security Considerations

**What goes to Qdrant Cloud:**
- Full session transcripts (user + Claude messages)
- Repo paths and machine names
- Any secrets you paste into sessions (API keys, passwords, etc.)

**Mitigations:**
- `.env` file is gitignored
- README includes security warning
- Future: regex patterns to redact sensitive data before indexing

## Performance

- Backfill of 157 sessions: ~2 minutes
- Single session index: <1 second
- Search: <500ms
- Retrieve: <1 second

## Future Enhancements

1. **Sensitive data redaction** - regex patterns to strip API keys, passwords
2. **Smart summarization** - summarize injected context instead of full dump
3. **Session tagging** - manual categorization for better organization
4. **Web UI** - browse sessions in browser
5. **Proactive suggestions** - "You worked on similar stuff before, want context?"

## Dependencies

```
qdrant-client>=1.7.0   # Qdrant API client
click>=8.0.0           # CLI framework
python-dotenv>=1.0.0   # .env file loading
rich>=13.0.0           # Pretty terminal output
```

No sentence-transformers, no PyTorch, no heavy ML dependencies.
