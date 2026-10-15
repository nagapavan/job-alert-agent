#!/usr/bin/env python3
"""
Setup ChromaDB as persistent vector memory for the job-alert-agent project.
Indexes all source files into a 'workspace_memory' collection.
"""

import os
import sys
import subprocess
from pathlib import Path

def install_chromadb():
    """Install chromadb and chroma-mcp packages."""
    print("Installing chromadb and dependencies...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", 
            "--quiet",
            "chromadb>=0.4.0",
            "chroma-mcp>=0.2.0"
        ])
        print("✓ Successfully installed chromadb and chroma-mcp")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to install chromadb: {e}")
        return False

def setup_chroma_directory():
    """Create .chroma directory for persistent storage."""
    chroma_dir = Path(".chroma")
    chroma_dir.mkdir(exist_ok=True)
    print(f"✓ Created .chroma directory at {chroma_dir.absolute()}")
    return chroma_dir

def index_project_files(chroma_dir):
    """Index all source files into ChromaDB."""
    try:
        import chromadb
    except ImportError:
        print("✗ chromadb not installed. Run install_chromadb() first.")
        return 0

    # Initialize persistent client using new API
    client = chromadb.PersistentClient(path=str(chroma_dir))
    
    # Get or create collection
    try:
        collection = client.delete_collection(name="workspace_memory")
        print("✓ Cleaned up existing workspace_memory collection")
    except:
        pass
    
    collection = client.create_collection(
        name="workspace_memory",
        metadata={"hnsw:space": "cosine"}
    )
    print("✓ Created 'workspace_memory' collection")
    
    # Patterns to include
    include_patterns = {
        '.py', '.js', '.html', '.css', '.md', '.json', '.yaml', '.yml', '.txt'
    }
    
    # Directories to skip
    skip_dirs = {
        '.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules',
        '.DS_Store', 'logs', '.aider.tags.cache.v4', '.opencode', '.chroma'
    }
    
    files_indexed = 0
    file_list = []
    
    # Walk through project
    for root, dirs, files in os.walk('.'):
        # Remove skip directories from dirs list (modifies in-place)
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        
        for file in files:
            file_path = Path(root) / file
            
            # Skip binary files and build artifacts
            if file_path.suffix not in include_patterns:
                continue
            
            # Skip certain files
            if file in {'.api_key', '.key', '.DS_Store', 'job_alert_agent.db'}:
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    
                # Skip empty files
                if not content.strip():
                    continue
                
                # Create document metadata
                relative_path = str(file_path)
                doc_id = relative_path.replace('/', '_').replace('.', '_')
                
                # Store document with metadata
                file_list.append({
                    'id': doc_id,
                    'path': relative_path,
                    'content': content,
                    'file_size': len(content),
                    'suffix': file_path.suffix
                })
                
                files_indexed += 1
                if files_indexed % 10 == 0:
                    print(f"  Processed {files_indexed} files...")
                    
            except Exception as e:
                print(f"  ⚠ Skipped {file_path}: {e}")
    
    # Batch insert documents
    print(f"\nIndexing {len(file_list)} files into ChromaDB...")
    batch_size = 50
    
    for i in range(0, len(file_list), batch_size):
        batch = file_list[i:i+batch_size]
        
        ids = [doc['id'] for doc in batch]
        documents = [doc['content'] for doc in batch]
        metadatas = [
            {
                'path': doc['path'],
                'file_size': doc['file_size'],
                'file_type': doc['suffix']
            }
            for doc in batch
        ]
        
        try:
            collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
            print(f"  ✓ Indexed batch {i//batch_size + 1} ({len(batch)} files)")
        except Exception as e:
            print(f"  ✗ Failed to index batch: {e}")
    
    # Persist to disk
    try:
        client.persist()
        print("✓ Persisted ChromaDB to disk")
    except:
        # Some versions don't require explicit persist
        pass
    
    return files_indexed

def test_semantic_search(chroma_dir):
    """Test semantic search on indexed files."""
    try:
        import chromadb
    except ImportError:
        print("✗ chromadb not installed")
        return
    
    # Initialize persistent client using new API
    client = chromadb.PersistentClient(path=str(chroma_dir))
    
    try:
        collection = client.get_collection(name="workspace_memory")
    except:
        print("✗ workspace_memory collection not found")
        return
    
    # Test queries
    test_queries = [
        "FastAPI backend API routes",
        "database schema models",
        "resume parsing and PDF extraction",
        "LinkedIn automation and scraping",
        "LLM provider configuration"
    ]
    
    print("\n" + "="*60)
    print("SEMANTIC SEARCH TEST")
    print("="*60)
    
    for query in test_queries:
        try:
            results = collection.query(
                query_texts=[query],
                n_results=2
            )
            
            print(f"\n🔍 Query: '{query}'")
            if results and results['documents'] and results['documents'][0]:
                for i, doc in enumerate(results['documents'][0]):
                    metadata = results['metadatas'][0][i]
                    distance = results['distances'][0][i]
                    print(f"  Result {i+1}: {metadata['path']}")
                    print(f"    Distance: {distance:.4f}")
                    # Show first 100 chars of content
                    preview = doc[:100].replace('\n', ' ')
                    print(f"    Preview: {preview}...")
            else:
                print("  No results found")
        except Exception as e:
            print(f"  ✗ Query failed: {e}")

def main():
    """Main setup function."""
    print("="*60)
    print("ChromaDB Vector Memory Setup for job-alert-agent")
    print("="*60)
    
    # Step 1: Install dependencies
    if not install_chromadb():
        print("\n✗ Setup failed: Could not install chromadb")
        sys.exit(1)
    
    # Step 2: Create .chroma directory
    chroma_dir = setup_chroma_directory()
    
    # Step 3: Index project files
    files_count = index_project_files(chroma_dir)
    print(f"\n✓ Successfully indexed {files_count} files")
    
    # Step 4: Test semantic search
    test_semantic_search(chroma_dir)
    
    print("\n" + "="*60)
    print("SETUP COMPLETE")
    print("="*60)
    print(f"ChromaDB folder: {chroma_dir.absolute()}")
    print(f"Files indexed: {files_count}")
    print(f"Collection: workspace_memory")
    print("\nNext steps:")
    print("1. Configure MCP server for OpenCode integration")
    print("2. Add ChromaDB configuration to .opencode/mcp.json")
    print("="*60)

if __name__ == "__main__":
    main()
