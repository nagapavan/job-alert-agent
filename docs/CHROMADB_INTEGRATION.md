# ChromaDB Vector Memory Integration Guide

## Overview

ChromaDB provides persistent semantic vector memory for the job-alert-agent project. It enables AI agents (Claude, OpenCode) to:
- Search across all source code and documentation semantically
- Understand code relationships and patterns
- Provide context-aware development assistance
- Make informed decisions based on existing implementations

**Status:** ✅ Fully operational  
**Location:** `.chroma/` (15MB persistent SQLite database)  
**Files Indexed:** 123 (all project source files)  
**Search Quality:** <100ms typical latency, cosine similarity

---

## Quick Start

### 1. Use the Python API

```python
from backend.chroma_memory import get_chroma_memory

# Initialize
memory = get_chroma_memory()

# Search
results = memory.search("FastAPI routes endpoints", n_results=5)
for i, (path, doc, metadata, distance) in enumerate(zip(
    results['ids'], results['documents'], 
    results['metadatas'], results['distances']
)):
    print(f"{i+1}. {metadata['path']} (distance: {distance:.4f})")
```

### 2. Use the CLI

```bash
# Search project
python scripts/chroma_cli.py search "database models"

# Statistics
python scripts/chroma_cli.py stats

# List Python files only
python scripts/chroma_cli.py list --file-type .py

# Retrieve specific file
python scripts/chroma_cli.py get backend/main.py
```

### 3. Use the FastAPI Endpoints

```bash
# Semantic search
curl "http://localhost:8000/api/memory/search?q=resume%20parsing&n_results=5"

# Get statistics
curl http://localhost:8000/api/memory/stats

# Rebuild index
curl -X POST http://localhost:8000/api/memory/rebuild
```

### 4. Use with Claude/OpenCode via MCP

The ChromaDB MCP server is automatically configured in:
- `.opencode/mcp.json` (OpenCode)
- `.opencode/claude_mcp.json` (Claude desktop)

Claude can invoke tools:
```python
# Claude can call this implicitly
results = tools.chroma.search({
    "query": "match scoring algorithm",
    "n_results": 3
})
```

---

## What's Indexed?

### Included File Types
- `.py` — Python source (backend, scripts)
- `.js` — JavaScript (extension, frontend)
- `.html` — HTML templates
- `.css` — Stylesheets
- `.md` — Documentation (AGENTS.md, README.md, etc.)
- `.json` — Configuration and data files
- `.yaml`, `.yml` — Configuration

### Excluded
- `.venv/` — Virtual environment
- `.git/` — Git history
- `__pycache__/` — Compiled Python
- `.key`, `.api_key` — Sensitive files
- `job_alert_agent.db` — Database binary
- `logs/` — Runtime logs

### File Count
```
Total: 123 files indexed
Backend: 35+ Python files
Frontend: 15+ JavaScript/HTML files
Docs: 10+ Markdown files
Tests: 8+ Test files
Config: Various .json, .yaml files
```

---

## Semantic Search Examples

### Good Queries (Conceptual)
✅ "FastAPI backend routes"  
✅ "database schema models SQLAlchemy"  
✅ "resume PDF parsing extraction"  
✅ "LinkedIn automation scraping"  
✅ "LLM provider OpenAI Gemini Claude"  
✅ "job matching scoring algorithm"  
✅ "browser automation Playwright"  

### Less Effective (Too Specific)
❌ "line 42 of main.py"  
❌ "the function called do_thing"  
❌ Single word: "routes" (prefer "API routes")  

---

## Architecture Details

### Storage
- **Format:** SQLite with Parquet partitions
- **Location:** `.chroma/` folder
- **Size:** ~15MB (123 files, 384-dimensional vectors)
- **Durability:** Write-ahead logging via `chroma.sqlite3`

### Embedding Model
- **Name:** all-MiniLM-L6-v2 (Hugging Face)
- **Dimensions:** 384
- **Training:** Sentence transformers (semantic understanding)
- **Local:** Downloaded to `~/.cache/chroma/` on first use
- **Speed:** ~5-50ms per document

### Client API
- **New API:** `chromadb.PersistentClient(path=".chroma")`
- **Collection:** `workspace_memory` (single default)
- **Similarity:** Cosine distance (lower = more similar)
- **Metadata:** Stores `path`, `file_size`, `file_type`

---

## Integration Points

### 1. FastAPI Backend (`backend/chroma_api.py`)

**Endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/memory/stats` | Collection statistics |
| `POST` | `/api/memory/search` | Semantic search (JSON body) |
| `GET` | `/api/memory/search?q=...&n_results=5` | Semantic search (URL params) |
| `GET` | `/api/memory/file?path=backend/main.py` | Retrieve indexed file |
| `POST` | `/api/memory/rebuild` | Rebuild entire index |

**Example Response:**
```json
{
  "query": "database models",
  "results": [
    {
      "file_path": "backend/database.py",
      "distance": 0.7493,
      "content_preview": "import sqlalchemy...",
      "file_size": 12543
    }
  ],
  "total_results": 1
}
```

### 2. Python Wrapper (`backend/chroma_memory.py`)

**Methods:**
- `search(query, n_results=5, where=None)` — Semantic search
- `search_by_file_type(query, file_type=".py", n_results=5)` — Filtered search
- `get_file(file_path)` — Retrieve indexed file content
- `add_document(doc_id, content, metadata)` — Add/index document
- `update_document(doc_id, content, metadata)` — Update indexed document
- `delete_document(doc_id)` — Remove from index
- `count_documents()` — Get total indexed count

### 3. CLI Tool (`scripts/chroma_cli.py`)

**Commands:**
```bash
search      # Semantic search
stats       # Show statistics
get         # Retrieve file
add         # Index a file
update      # Update indexed file
delete      # Remove from index
rebuild     # Full reindex
list        # List all indexed files
```

### 4. MCP Servers

**OpenCode (`opencode/mcp.json`):**
- Enables ChromaDB access via MCP protocol
- Two implementations: npx and Python

**Claude (`opencode/claude_mcp.json`):**
- Standalone configuration for Claude desktop client
- Points to `.chroma` data directory

---

## Maintenance

### Rebuild Index After Major Changes

```bash
python scripts/setup_chromadb.py
```

This:
1. Scans project for new/modified files
2. Clears old collection
3. Re-indexes all eligible files
4. Tests semantic search
5. Reports final count

**Time:** ~2-5 minutes (depends on file count and disk speed)

### Add New Documents

```python
from backend.chroma_memory import get_chroma_memory

memory = get_chroma_memory()
memory.add_document(
    doc_id="reasoning_2026_10_15",
    content=open("reasoning/2026-10-15-example.md").read(),
    metadata={
        "path": "reasoning/2026-10-15-example.md",
        "file_type": ".md",
        "file_size": 5432
    }
)
```

Or via CLI:
```bash
python scripts/chroma_cli.py add reasoning/2026-10-15-example.md
```

### Delete Indexed Files

```bash
python scripts/chroma_cli.py delete backend/old_module.py
```

### Monitor Size

```bash
python scripts/chroma_cli.py stats
# Shows total indexed documents and collection name
```

---

## Performance Characteristics

### Search Performance
- **Single query:** <100ms (cold cache)
- **Repeated queries:** <50ms (warm cache)
- **Batch operations:** ~10-20ms per document

### Memory Usage
- **Python process:** +80-150MB (after loading model)
- **Embedding generation:** ~5-10MB working set

### Indexing Speed
- **Files scanned:** 123/123 (~100ms total walk)
- **Indexing:** 3 batches of 50 documents (~5-10 seconds)
- **Embedding model download:** ~1 minute (first run only)

### Database Size
- **SQLite file:** 15MB
- **Index overhead:** ~120KB per document

---

## Troubleshooting

### ChromaDB Not Found

```bash
pip install chromadb chroma-mcp
```

### Collection Empty

```bash
python scripts/setup_chromadb.py
```

### MCP Server Won't Start

```bash
# Verify installation
python -m chroma_mcp --help

# Check data directory exists
ls -la .chroma/
```

### Slow Searches

- First query after startup may be slow (model loading)
- Verify `.chroma/` is on local disk (not network mount)
- Check system disk space: `df -h`

### Stale Results

Rebuild index after major refactoring:
```bash
python scripts/setup_chromadb.py
```

---

## Integration Workflows

### Before Editing Code

```python
from backend.chroma_memory import get_chroma_memory

memory = get_chroma_memory()

# Search for related implementations
results = memory.search("my feature area", n_results=3)

# Review patterns and conventions
for result in results['documents']:
    print(result[:500])  # Preview
```

### Documenting Decisions

Create file: `reasoning/YYYY-MM-DD-short-description.md`

After editing, optionally re-index:
```bash
python scripts/chroma_cli.py add reasoning/2026-10-15-feature.md
```

### Agent Context Awareness

Claude can search before responding:
```
User: "How do I add a new API endpoint?"

Claude calls: memory.search("FastAPI endpoint route handler")
Claude reviews: Results show patterns from backend/main.py
Claude responds: "Based on the project pattern of..."
```

---

## Configuration Reference

### Environment Variables
```bash
# Override data directory
export CHROMA_DATA_DIR=/custom/path

# Disable telemetry (already off by default)
export CHROMA_TELEMETRY_DISABLED=true
```

### File Patterns

Edit `scripts/setup_chromadb.py` to change indexed file types:
```python
include_patterns = {
    '.py', '.js', '.html', '.css', '.md', '.json', '.yaml', '.yml', '.txt'
}
```

### Skip Directories

Edit to exclude more folders:
```python
skip_dirs = {
    '.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules',
    '.DS_Store', 'logs', '.aider.tags.cache.v4', '.opencode', '.chroma'
}
```

---

## Related Documentation

- `.chroma/README.md` — Database-specific notes
- `reasoning/2026-10-15-setup-chromadb-memory.md` — Setup decision log
- `backend/chroma_memory.py` — Python API implementation
- `backend/chroma_api.py` — FastAPI endpoints
- `scripts/chroma_cli.py` — CLI tool
- `scripts/setup_chromadb.py` — Setup and indexing script

---

## FAQ

**Q: Can I search for specific line numbers?**  
A: No, semantic search works on concepts, not syntax positions. Use git grep for line-level searches.

**Q: How do I know search quality is good?**  
A: Check the `distance` score. Typically 0.4-0.7 = highly relevant, 0.8+ = tangentially related.

**Q: Can I use this without MCP?**  
A: Yes! Use the Python API or FastAPI endpoints directly.

**Q: How often should I rebuild?**  
A: After significant refactoring or architectural changes (~monthly). Small changes don't require rebuilds.

**Q: Is the index stored in git?**  
A: No, `.chroma/` is not committed. Rebuild locally or in CI/CD.

**Q: Can I add custom collections?**  
A: Yes, modify `backend/chroma_memory.py` to create additional collections for different purposes.

---

## Version History

- **2026-10-15:** Initial setup and integration
  - 123 files indexed
  - FastAPI endpoints added
  - CLI tool created
  - MCP configuration for OpenCode/Claude
