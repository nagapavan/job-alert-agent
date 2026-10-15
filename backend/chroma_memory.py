"""
ChromaDB Vector Memory Interface

Provides convenient access to the ChromaDB vector database for semantic search
and document retrieval across the project codebase.
"""

from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


class ChromaMemory:
    """Interface to ChromaDB vector memory store."""
    
    def __init__(self, data_dir: str = ".chroma", collection_name: str = "workspace_memory"):
        """
        Initialize ChromaDB memory interface.
        
        Args:
            data_dir: Path to ChromaDB data directory
            collection_name: Name of the collection to use
        """
        self.data_dir = Path(data_dir)
        self.collection_name = collection_name
        self._client = None
        self._collection = None
    
    @property
    def client(self):
        """Lazy-load ChromaDB client."""
        if self._client is None:
            try:
                import chromadb
                
                # Use new PersistentClient API
                self._client = chromadb.PersistentClient(path=str(self.data_dir))
                logger.info(f"Initialized ChromaDB client at {self.data_dir}")
            except ImportError:
                logger.error("chromadb not installed. Run: pip install chromadb")
                raise
        return self._client
    
    @property
    def collection(self):
        """Lazy-load ChromaDB collection."""
        if self._collection is None:
            try:
                self._collection = self.client.get_collection(name=self.collection_name)
            except Exception as e:
                logger.error(f"Failed to get collection '{self.collection_name}': {e}")
                raise
        return self._collection
    
    def search(
        self,
        query: str,
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Semantic search in the vector database.
        
        Args:
            query: Search query string
            n_results: Number of results to return
            where: Optional filter criteria (Chroma where syntax)
        
        Returns:
            Dictionary with 'ids', 'documents', 'metadatas', 'distances'
        """
        try:
            if where:
                results = self.collection.query(
                    query_texts=[query],
                    n_results=n_results,
                    where=where
                )
            else:
                results = self.collection.query(
                    query_texts=[query],
                    n_results=n_results
                )
            
            # Flatten single-query results
            if results['documents']:
                return {
                    'ids': results['ids'][0] if results['ids'] else [],
                    'documents': results['documents'][0] if results['documents'] else [],
                    'metadatas': results['metadatas'][0] if results['metadatas'] else [],
                    'distances': results['distances'][0] if results['distances'] else []
                }
            return {'ids': [], 'documents': [], 'metadatas': [], 'distances': []}
        
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return {'ids': [], 'documents': [], 'metadatas': [], 'distances': []}
    
    def search_by_file_type(
        self,
        query: str,
        file_type: str = ".py",
        n_results: int = 5
    ) -> Dict[str, Any]:
        """
        Search filtering by file type.
        
        Args:
            query: Search query string
            file_type: File extension to filter by (e.g., '.py', '.js', '.md')
            n_results: Number of results to return
        
        Returns:
            Filtered search results
        """
        return self.search(
            query=query,
            n_results=n_results,
            where={"file_type": file_type}
        )
    
    def get_file(self, file_path: str) -> Optional[str]:
        """
        Retrieve a specific file's content from the database.
        
        Args:
            file_path: Path to the file (e.g., 'backend/main.py')
        
        Returns:
            File content or None if not found
        """
        try:
            doc_id = file_path.replace('/', '_').replace('.', '_')
            result = self.collection.get(ids=[doc_id])
            
            if result['documents']:
                return result['documents'][0]
            return None
        except Exception as e:
            logger.error(f"Failed to get file '{file_path}': {e}")
            return None
    
    def count_documents(self) -> int:
        """Get total number of indexed documents."""
        try:
            return self.collection.count()
        except Exception as e:
            logger.error(f"Failed to count documents: {e}")
            return 0
    
    def add_document(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add or update a document in the vector database.
        
        Args:
            doc_id: Unique document identifier
            content: Document content to index
            metadata: Optional metadata dictionary
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.collection.add(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata or {}]
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add document '{doc_id}': {e}")
            return False
    
    def update_document(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Update an existing document in the vector database.
        
        Args:
            doc_id: Document identifier
            content: New document content
            metadata: Optional updated metadata
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.collection.update(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata or {}]
            )
            return True
        except Exception as e:
            logger.error(f"Failed to update document '{doc_id}': {e}")
            return False
    
    def delete_document(self, doc_id: str) -> bool:
        """
        Delete a document from the vector database.
        
        Args:
            doc_id: Document identifier to delete
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.collection.delete(ids=[doc_id])
            return True
        except Exception as e:
            logger.error(f"Failed to delete document '{doc_id}': {e}")
            return False


# Global instance for easy access
_chroma_memory: Optional[ChromaMemory] = None


def get_chroma_memory() -> ChromaMemory:
    """Get or create the global ChromaMemory instance."""
    global _chroma_memory
    if _chroma_memory is None:
        _chroma_memory = ChromaMemory()
    return _chroma_memory


# Example usage patterns (uncomment to test)
if __name__ == "__main__":
    # Initialize
    memory = ChromaMemory()
    
    # Search examples
    print("Searching for 'FastAPI backend routes'...")
    results = memory.search("FastAPI backend routes", n_results=3)
    for i, (doc_id, doc, meta) in enumerate(zip(results['ids'], results['documents'], results['metadatas'])):
        print(f"\n{i+1}. {meta.get('path', 'unknown')}")
        print(f"   Distance: {results['distances'][i]:.4f}")
        print(f"   Preview: {doc[:100]}...")
    
    print(f"\nTotal indexed documents: {memory.count_documents()}")
