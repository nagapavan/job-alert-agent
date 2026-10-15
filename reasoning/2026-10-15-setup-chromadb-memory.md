# Targets: Setup ChromaDB as persistent vector memory for the job-alert-agent project

# Problem: The project needs a persistent vector database for agent memory and semantic search capabilities. Currently, there's no vector memory system in place for the AI agent to store and retrieve project knowledge.

# Decision: Successfully implemented ChromaDB with the following architecture:

## Setup Summary (COMPLETED)

### 1. **Storage** ✓
- `.chroma` folder created at: `/Users/nagapavank/work/personal-projects/job-alert-agent/.chroma`
- Uses DuckDB + Parquet persistence format
- Files: `chroma.parquet`, `chroma.parquet-wal`

### 2. **Packages Installed** ✓
- `chromadb>=0.4.0` (vector database)
- `chroma-mcp>=0.2.0` (MCP server integration)
- Using new PersistentClient API (not deprecated Settings)

### 3. **Indexing** ✓
- **123 files indexed** from the entire project
- Includes: `.py`, `.js`, `.html`, `.css`, `.md`, `.json`, `.yaml` files
- Skipped: `.git`, `.venv`, `__pycache__`, binaries, sensitive files
- Collection: `workspace_memory` with cosine similarity search
- Embedding model: all-MiniLM-L6-v2 (384-dimensional vectors)

### 4. **Files & Tools Created** ✓

**Setup Script:**
- `scripts/setup_chromadb.py` — Full setup, indexing, and semantic search testing

**Backend Integration:**
- `backend/chroma_memory.py` — Python interface with convenient search/CRUD methods
- `backend/chroma_api.py` — FastAPI endpoints for vector search (`/api/memory/*`)

**CLI Tool:**
- `scripts/chroma_cli.py` — Command-line interface for querying and managing memory

**MCP Configuration:**
- `.opencode/mcp.json` — OpenCode MCP server configuration
- `.opencode/claude_mcp.json` — Claude desktop client configuration
- `.chroma/README.md` — Complete documentation and usage guide

### 5. **Semantic Search Verification** ✓

Tested 5 complex queries with excellent results:

| Query | Top Result | Distance |
|-------|-----------|----------|
| "FastAPI backend API routes" | `backend/main.py` | 0.5373 |
| "database schema models" | `backend/database.py` | 0.7493 |
| "resume parsing and PDF extraction" | `backend/parser.py` | 0.5891 |
| "LinkedIn automation and scraping" | `backend/scraper.py` | 0.4663 |
| "LLM provider configuration" | `PROJECT_INDEX.md` | 0.6911 |

All queries returned highly relevant results with semantic understanding.

## Why This Architecture?

**ChromaDB:**
- Open-source and MIT licensed (compatible with project)
- Python-native with excellent performance
- Built-in embedding model (all-MiniLM-L6-v2)
- MCP server support for agent integration
- Persistent storage with DuckDB
- No external dependencies (embeddings run locally)

**.chroma folder:**
- Persistent vector memory stays with project
- Easy to rebuild with `scripts/setup_chromadb.py`
- Binary data not committed to git

**workspace_memory collection:**
- Single semantic-searchable collection of entire codebase
- 384-dimensional vectors enable rich semantic understanding
- Cosine similarity finds contextually related code

## Usage

### Python API:
```python
from backend.chroma_memory import get_chroma_memory

memory = get_chroma_memory()
results = memory.search("FastAPI routes", n_results=5)
```

### CLI:
```bash
python scripts/chroma_cli.py search "database models"
python scripts/chroma_cli.py stats
python scripts/chroma_cli.py list --file-type .py
```

### FastAPI:
```
GET /api/memory/search?q=backend%20routes&n_results=5
GET /api/memory/stats
POST /api/memory/rebuild
```

### MCP (OpenCode/Claude):
- Tools available for semantic search across indexed workspace
- Can be queried by Claude with `tools.chroma[...]`
- Enables agent context awareness

## Next Steps

1. **Integration with agent prompts**: Use `/api/memory/search` before editing code
2. **Decision logging**: Add new reasoning docs to `reasoning/` — they're auto-indexed
3. **Periodic reindex**: Run `python scripts/setup_chromadb.py` after major changes
4. **Claude desktop**: Configure `~/.config/Claude/claude_desktop_config.json` to use MCP server