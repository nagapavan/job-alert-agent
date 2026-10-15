"""
FastAPI endpoints for ChromaDB vector memory search and management.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import logging

from backend.chroma_memory import get_chroma_memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["vector-memory"])


class SearchQuery(BaseModel):
    """Semantic search query."""
    query: str = Field(..., min_length=1, max_length=500, description="Search query")
    n_results: int = Field(default=5, ge=1, le=20, description="Number of results to return")
    file_type: Optional[str] = Field(None, description="Filter by file type (e.g., '.py', '.js')")


class SearchResult(BaseModel):
    """Individual search result."""
    file_path: str
    distance: float
    content_preview: str
    file_size: int


class SearchResponse(BaseModel):
    """Response from semantic search."""
    query: str
    results: List[SearchResult]
    total_results: int


class MemoryStats(BaseModel):
    """Statistics about the vector memory store."""
    total_documents: int
    collection_name: str
    data_directory: str


@router.get("/stats", response_model=MemoryStats, summary="Get vector memory statistics")
async def get_memory_stats():
    """Get statistics about the ChromaDB vector memory store."""
    try:
        memory = get_chroma_memory()
        return MemoryStats(
            total_documents=memory.count_documents(),
            collection_name=memory.collection_name,
            data_directory=str(memory.data_dir)
        )
    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve memory statistics")


@router.post("/search", response_model=SearchResponse, summary="Semantic search in vector memory")
async def semantic_search(search_query: SearchQuery):
    """
    Perform semantic search across indexed project files.
    
    **Query Examples:**
    - "FastAPI backend API routes"
    - "database schema models SQLAlchemy"
    - "resume parsing PDF extraction"
    - "LinkedIn automation scraping"
    - "LLM provider configuration"
    """
    try:
        memory = get_chroma_memory()
        
        if search_query.file_type:
            results = memory.search_by_file_type(
                query=search_query.query,
                file_type=search_query.file_type,
                n_results=search_query.n_results
            )
        else:
            results = memory.search(
                query=search_query.query,
                n_results=search_query.n_results
            )
        
        search_results = []
        for doc_id, doc, metadata, distance in zip(
            results['ids'],
            results['documents'],
            results['metadatas'],
            results['distances']
        ):
            # Create preview (first 200 chars, single line)
            preview = doc.replace('\n', ' ')[:200] + "..." if len(doc) > 200 else doc
            
            search_results.append(SearchResult(
                file_path=metadata.get('path', 'unknown'),
                distance=float(distance),
                content_preview=preview,
                file_size=metadata.get('file_size', 0)
            ))
        
        return SearchResponse(
            query=search_query.query,
            results=search_results,
            total_results=len(search_results)
        )
    
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.get("/search", response_model=SearchResponse, summary="Semantic search (GET)")
async def semantic_search_get(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    n_results: int = Query(default=5, ge=1, le=20, description="Number of results"),
    file_type: Optional[str] = Query(None, description="Filter by file type")
):
    """
    Perform semantic search using GET request.
    
    **Query Parameters:**
    - `q`: Search query string
    - `n_results`: Number of results (default: 5, max: 20)
    - `file_type`: Optional filter by file type (e.g., '.py', '.js', '.md')
    """
    return await semantic_search(
        SearchQuery(query=q, n_results=n_results, file_type=file_type)
    )


@router.get("/file", summary="Get indexed file content")
async def get_file(
    path: str = Query(..., description="File path (e.g., 'backend/main.py')")
):
    """
    Retrieve the content of an indexed file from the vector memory.
    
    **Example:** `/api/memory/file?path=backend/main.py`
    """
    try:
        memory = get_chroma_memory()
        content = memory.get_file(path)
        
        if content is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        
        return {
            "file_path": path,
            "content": content,
            "size": len(content)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get file '{path}': {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve file")


@router.post("/rebuild", summary="Rebuild vector memory index")
async def rebuild_memory():
    """
    Rebuild the entire ChromaDB index from project files.
    
    **Warning:** This operation may take several minutes for large projects.
    
    **Returns:** Status message with file count.
    """
    try:
        # Import here to avoid circular dependencies
        import subprocess
        import sys
        from pathlib import Path
        
        script_path = Path(__file__).parent.parent / "scripts" / "setup_chromadb.py"
        
        # Run the setup script
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            logger.error(f"Rebuild failed: {result.stderr}")
            raise HTTPException(status_code=500, detail=f"Rebuild failed: {result.stderr}")
        
        # Reconnect to get fresh stats
        memory = get_chroma_memory()
        doc_count = memory.count_documents()
        
        return {
            "status": "success",
            "message": "Vector memory index rebuilt successfully",
            "total_documents": doc_count,
            "output": result.stdout
        }
    
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Rebuild operation timed out")
    except Exception as e:
        logger.error(f"Rebuild failed: {e}")
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {str(e)}")


# Include this router in main.py:
# from backend.chroma_api import router as chroma_router
# app.include_router(chroma_router)
