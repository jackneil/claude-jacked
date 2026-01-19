# Jacked

Cross-machine semantic search for Claude Code sessions. Find and load context from past work without digging through files.

## Why This Exists

Claude Code stores sessions locally at `~/.claude/projects/`. Problem is:
- Sessions don't sync across machines
- They get compacted over time (context lost)
- Finding "that thing I did 2 weeks ago" means grep-ing through JSONL files

Jacked fixes this by continuously indexing all your sessions to Qdrant Cloud. Search semantically, load context instantly, works from any machine.

## How It Works

```
You: /jacked implement overnight OB time handling

Claude: Found 3 matches:
        1. [92%] 2025-01-10 - "anesthesia time handling, overnight cases..."
        2. [78%] 2025-01-05 - "OB epidural time tracking..."
        3. [65%] 2024-12-20 - "DOS resolver for midnight cases..."

        Load context from which session? (1-3, or 'skip')

You: 1

Claude: Loaded context from session 2025-01-10. That session covered:
        - AnesthesiaTimeEntry model changes
        - Discontinuous time handling
        - note_ids tracking fixes

        What would you like to work on?
```

## Quick Start

### 1. Get Qdrant Cloud Account

Sign up at [cloud.qdrant.io](https://cloud.qdrant.io) and create a cluster.

**Important:** You need a **paid tier** ($30/month minimum) for server-side embedding via Qdrant Cloud Inference. The free tier won't work.

Get your:
- Cluster URL (e.g., `https://abc123.us-east-1-1.aws.cloud.qdrant.io`)
- API Key

### 2. Install Jacked

**Recommended: Use pipx** (installs globally, available in all terminals):

```bash
pipx install jacked
```

Don't have pipx? Install it first:
```bash
# Linux/Mac
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Windows
pip install pipx
pipx ensurepath
```

**Alternative: pip install** (if you don't want pipx):
```bash
pip install jacked
```

**For development** (editable install, code changes take effect immediately):
```bash
git clone https://github.com/hank-ai/jacked
cd jacked
pipx install --editable .
```

### 3. Configure Credentials

Set environment variables (add to your shell profile):

```bash
# Linux/Mac (.bashrc or .zshrc)
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"
```

```powershell
# Windows PowerShell (profile.ps1) - or set via System Properties
$env:QDRANT_CLAUDE_SESSIONS_ENDPOINT = "https://your-cluster.qdrant.io"
$env:QDRANT_CLAUDE_SESSIONS_API_KEY = "your-api-key"
```

Or create a `.env` file in your working directory:
```
QDRANT_CLAUDE_SESSIONS_ENDPOINT=https://your-cluster.qdrant.io
QDRANT_CLAUDE_SESSIONS_API_KEY=your-api-key
```

### 4. Install Hook & Skill

```bash
jacked install
```

This adds:
- **Stop hook** - Auto-indexes sessions after every Claude response
- **Skill file** - Enables `/jacked` command in Claude

### 5. Index Existing Sessions

```bash
jacked backfill
```

This indexes all your existing Claude sessions. Takes a few minutes depending on how many you have.

### 6. Verify It Works

```bash
jacked status   # Check Qdrant connectivity
jacked search "something you worked on before"
```

## Usage

### CLI Commands

```bash
# Search for sessions
jacked search "implement user authentication"
jacked search "fix database connection" --repo /path/to/repo

# List indexed sessions
jacked list
jacked list --repo myproject --limit 20

# Get full transcript
jacked retrieve <session_id>
jacked retrieve <session_id> --summary
jacked retrieve <session_id> --output transcript.txt

# Index a specific session
jacked index /path/to/session.jsonl --repo /path/to/repo

# Backfill all sessions
jacked backfill
jacked backfill --repo myproject --force  # Re-index even unchanged

# Check status
jacked status

# Delete a session from index
jacked delete <session_id>

# Show configuration help
jacked configure
```

### In Claude Code

Use the `/jacked` skill:

```
/jacked implement caching for the API
```

Claude will:
1. Search for similar past sessions
2. Show matches with relevance scores
3. Let you pick which one to load
4. If local: suggest native resume command
5. If remote: inject the transcript as context

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  YOUR MACHINE                                                   │
│                                                                 │
│  Claude Code                                                    │
│  ├── Stop hook → jacked index (after every response)       │
│  └── /jacked skill → search + retrieve + inject            │
│                                                                 │
│  jacked CLI                                                 │
│  ├── index    - Parse JSONL, upsert to Qdrant                  │
│  ├── search   - Semantic search via Qdrant                     │
│  ├── retrieve - Get full transcript from Qdrant                │
│  └── ...                                                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  QDRANT CLOUD                                                   │
│                                                                 │
│  • Server-side embedding (no local ML models needed)           │
│  • Model: sentence-transformers/all-minilm-l6-v2               │
│  • Stores vectors + full transcripts in payloads               │
│  • ~11K points for 120 sessions                                │
└─────────────────────────────────────────────────────────────────┘
```

## Why Qdrant Cloud Inference?

Jacked uses Qdrant's **server-side embedding** feature. This means:

**You don't need:**
- sentence-transformers
- PyTorch
- Any ML models locally
- GPU

**Benefits:**
- Fast `pip install` (no heavy dependencies)
- Consistent embeddings across all your machines
- Qdrant handles model updates

**Trade-off:**
- Requires paid Qdrant tier ($30/month)
- Your text goes to Qdrant for embedding

## ⚠️ Security Warning

**Jacked sends your session data to Qdrant Cloud.** This includes:

- Full conversation transcripts (your messages + Claude's responses)
- Repo paths and machine names
- **Anything you paste into sessions** (API keys, passwords, secrets)

**If you paste sensitive data in a Claude session, it will be indexed.**

Recommendations:
- Don't paste secrets in Claude sessions (use env vars instead)
- Keep your Qdrant API key secure
- Consider self-hosting Qdrant if security is critical

Future versions may add regex-based redaction for common secret patterns.

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `QDRANT_CLAUDE_SESSIONS_ENDPOINT` | Yes | Your Qdrant Cloud cluster URL |
| `QDRANT_CLAUDE_SESSIONS_API_KEY` | Yes | Your Qdrant API key |
| `QDRANT_CLAUDE_SESSIONS_COLLECTION` | No | Collection name (default: `claude_sessions`) |
| `CLAUDE_PROJECTS_DIR` | No | Override Claude projects dir (default: `~/.claude/projects`) |
| `SMART_FORK_MACHINE_NAME` | No | Override machine name for indexing |

### Hook Configuration

The Stop hook is added to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "jacked index --repo \"$CLAUDE_PROJECT_DIR\""
      }]
    }]
  }
}
```

## Troubleshooting

### "Configuration error: QDRANT_CLAUDE_SESSIONS_ENDPOINT not set"

Your environment variables aren't loaded. Either:
- Add them to your shell profile and restart terminal
- Create a `.env` file in your working directory
- On Windows, you may need to set them at Machine level via System Properties

### "No matching sessions found"

- Run `jacked backfill` to index existing sessions
- Check `jacked status` to verify Qdrant connectivity
- Try a broader search query

### "Indexed Vectors: 0" in status

This is normal right after indexing. Qdrant indexes vectors asynchronously. Wait a few seconds and check again.

### Hook not running

- Verify hook is in `~/.claude/settings.json`
- Make sure `jacked` is on your PATH
- Check Claude Code logs for hook errors

### "jacked: command not found"

The script is installed but not on PATH. Best fix:
```bash
pipx install jacked
```

This installs it globally and adds it to PATH automatically.

If you prefer pip, add the scripts directory to PATH:
- Linux/Mac: `~/.local/bin`
- Windows: `C:\Users\you\AppData\Roaming\Python\PythonXX\Scripts`

## How It's Different From...

### Claude's native `--resume`
- Only works on the same machine
- Requires the session file to exist locally
- Jacked works cross-machine via Qdrant

### Grep-ing through session files
- Grep is keyword-based, Jacked is semantic
- "implement auth" finds sessions about "user authentication" and "login flow"
- No need to remember exact words you used

### Manual copy-paste
- Jacked automates the search → retrieve → inject flow
- Context appears in your current conversation without switching windows

## Development

```bash
# Clone and install in dev mode
git clone https://github.com/hank-ai/jacked
cd jacked
pip install -e ".[dev]"

# Run tests
pytest

# Create .env for local testing
echo "QDRANT_CLAUDE_SESSIONS_ENDPOINT=your-url" > .env
echo "QDRANT_CLAUDE_SESSIONS_API_KEY=your-key" >> .env
```

## Design Documentation

For implementation details, architecture decisions, and lessons learned, see [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT

## Credits

Built for use with [Claude Code](https://claude.ai/code) by Anthropic.

Uses [Qdrant](https://qdrant.tech/) for vector search and [Qdrant Cloud Inference](https://qdrant.tech/documentation/cloud/cloud-inference/) for server-side embedding.
