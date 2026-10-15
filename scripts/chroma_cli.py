#!/usr/bin/env python3
"""
ChromaDB Vector Memory CLI Tool

Manage and query the ChromaDB vector memory from the command line.
"""

import sys
import argparse
from pathlib import Path
from typing import Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.chroma_memory import ChromaMemory


def create_parser():
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        description="ChromaDB Vector Memory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Search for related code
  python scripts/chroma_cli.py search "FastAPI routes"
  
  # Search Python files only
  python scripts/chroma_cli.py search "database models" --file-type .py
  
  # Get statistics
  python scripts/chroma_cli.py stats
  
  # Retrieve a specific file
  python scripts/chroma_cli.py get backend/main.py
  
  # Add a new document
  python scripts/chroma_cli.py add reasoning/2026-10-15-setup.md
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Search command
    search_parser = subparsers.add_parser('search', help='Semantic search')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('-n', '--n-results', type=int, default=5, help='Number of results (default: 5)')
    search_parser.add_argument('-f', '--file-type', help='Filter by file type (e.g., .py, .js)')
    
    # Stats command
    subparsers.add_parser('stats', help='Show memory statistics')
    
    # Get command
    get_parser = subparsers.add_parser('get', help='Retrieve a specific file')
    get_parser.add_argument('path', help='File path to retrieve')
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add/index a file')
    add_parser.add_argument('path', help='File path to add')
    
    # Update command
    update_parser = subparsers.add_parser('update', help='Update a file in memory')
    update_parser.add_argument('path', help='File path to update')
    
    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete a file from memory')
    delete_parser.add_argument('path', help='File path to delete')
    
    # Rebuild command
    subparsers.add_parser('rebuild', help='Rebuild entire index from source files')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List indexed files')
    list_parser.add_argument('-f', '--file-type', help='Filter by file type')
    
    return parser


def cmd_search(memory: ChromaMemory, args):
    """Execute search command."""
    if args.file_type:
        results = memory.search_by_file_type(
            query=args.query,
            file_type=args.file_type,
            n_results=args.n_results
        )
    else:
        results = memory.search(
            query=args.query,
            n_results=args.n_results
        )
    
    if not results['ids']:
        print(f"No results found for: '{args.query}'")
        return
    
    print(f"\n🔍 Search Results for: '{args.query}'")
    print(f"   Found {len(results['ids'])} results\n")
    
    for i, (doc_id, doc, metadata, distance) in enumerate(zip(
        results['ids'],
        results['documents'],
        results['metadatas'],
        results['distances']
    )):
        print(f"{i+1}. {metadata.get('path', 'unknown')}")
        print(f"   Distance: {distance:.4f} (lower is better)")
        print(f"   Size: {metadata.get('file_size', 0)} bytes")
        
        # Show preview
        preview = doc.replace('\n', ' ')[:120]
        if len(doc) > 120:
            preview += "..."
        print(f"   Preview: {preview}")
        print()


def cmd_stats(memory: ChromaMemory, args):
    """Execute stats command."""
    count = memory.count_documents()
    print(f"\n📊 ChromaDB Vector Memory Statistics")
    print(f"   Collection: {memory.collection_name}")
    print(f"   Data Directory: {memory.data_dir.absolute()}")
    print(f"   Total Documents: {count}")
    print()


def cmd_get(memory: ChromaMemory, args):
    """Execute get command."""
    content = memory.get_file(args.path)
    
    if content is None:
        print(f"✗ File not found: {args.path}")
        return
    
    print(f"\n📄 File: {args.path}")
    print(f"   Size: {len(content)} bytes\n")
    print("--- Content ---")
    print(content)
    print("--- End ---\n")


def cmd_add(memory: ChromaMemory, args):
    """Execute add command."""
    path = Path(args.path)
    
    if not path.exists():
        print(f"✗ File not found: {path}")
        return
    
    try:
        content = path.read_text(encoding='utf-8', errors='ignore')
        doc_id = args.path.replace('/', '_').replace('.', '_')
        
        success = memory.add_document(
            doc_id=doc_id,
            content=content,
            metadata={
                'path': args.path,
                'file_size': len(content),
                'file_type': path.suffix
            }
        )
        
        if success:
            print(f"✓ Added file to memory: {args.path}")
        else:
            print(f"✗ Failed to add file: {args.path}")
    except Exception as e:
        print(f"✗ Error: {e}")


def cmd_update(memory: ChromaMemory, args):
    """Execute update command."""
    path = Path(args.path)
    
    if not path.exists():
        print(f"✗ File not found: {path}")
        return
    
    try:
        content = path.read_text(encoding='utf-8', errors='ignore')
        doc_id = args.path.replace('/', '_').replace('.', '_')
        
        success = memory.update_document(
            doc_id=doc_id,
            content=content,
            metadata={
                'path': args.path,
                'file_size': len(content),
                'file_type': path.suffix
            }
        )
        
        if success:
            print(f"✓ Updated file in memory: {args.path}")
        else:
            print(f"✗ Failed to update file: {args.path}")
    except Exception as e:
        print(f"✗ Error: {e}")


def cmd_delete(memory: ChromaMemory, args):
    """Execute delete command."""
    doc_id = args.path.replace('/', '_').replace('.', '_')
    
    success = memory.delete_document(doc_id)
    if success:
        print(f"✓ Deleted file from memory: {args.path}")
    else:
        print(f"✗ Failed to delete file: {args.path}")


def cmd_rebuild(memory: ChromaMemory, args):
    """Execute rebuild command."""
    import subprocess
    
    print("Rebuilding ChromaDB index from source files...")
    script_path = Path(__file__).parent / "setup_chromadb.py"
    
    try:
        subprocess.run([sys.executable, str(script_path)], check=True)
        print("✓ Index rebuild completed successfully")
    except subprocess.CalledProcessError as e:
        print(f"✗ Rebuild failed: {e}")


def cmd_list(memory: ChromaMemory, args):
    """Execute list command."""
    try:
        all_docs = memory.collection.get()
        
        print(f"\n📋 Indexed Files ({len(all_docs['ids'])} total)")
        print()
        
        for doc_id, metadata in zip(all_docs['ids'], all_docs['metadatas']):
            path = metadata.get('path', 'unknown')
            size = metadata.get('file_size', 0)
            ftype = metadata.get('file_type', 'unknown')
            
            if args.file_type and ftype != args.file_type:
                continue
            
            print(f"  {path:50s} {size:>8d}b")
        
        print()
    except Exception as e:
        print(f"✗ Error: {e}")


def main():
    """Main CLI function."""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        memory = ChromaMemory()
        
        # Route commands
        if args.command == 'search':
            cmd_search(memory, args)
        elif args.command == 'stats':
            cmd_stats(memory, args)
        elif args.command == 'get':
            cmd_get(memory, args)
        elif args.command == 'add':
            cmd_add(memory, args)
        elif args.command == 'update':
            cmd_update(memory, args)
        elif args.command == 'delete':
            cmd_delete(memory, args)
        elif args.command == 'rebuild':
            cmd_rebuild(memory, args)
        elif args.command == 'list':
            cmd_list(memory, args)
    
    except Exception as e:
        print(f"✗ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
