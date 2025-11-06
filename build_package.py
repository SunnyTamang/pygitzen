#!/usr/bin/env python3
"""
Build script for pygitzen package.
Moves previous build to archive folder, cleans, and builds fresh package.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    print("🔨 Building pygitzen package...")
    print()
    
    # Create archive directory if it doesn't exist
    archive_dir = Path("previous_builds")
    archive_dir.mkdir(exist_ok=True)
    print(f"📁 Archive directory: {archive_dir}")
    
    # Move existing dist/ to archive if it exists
    dist_dir = Path("dist")
    if dist_dir.exists() and any(dist_dir.iterdir()):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"dist_{timestamp}"
        print(f"📦 Moving existing dist/ to: {archive_path}")
        shutil.move(str(dist_dir), str(archive_path))
        print("✅ Archived previous build")
    else:
        print("ℹ️  No existing dist/ to archive")
    
    # Clean build artifacts
    print()
    print("🧹 Cleaning build artifacts...")
    
    clean_dirs = ["build", "*.egg-info", "src/pygitzen.egg-info"]
    clean_files = ["git_service_cython.c", "*.pyc"]
    clean_patterns = ["__pycache__"]
    
    # Remove directories
    for pattern in clean_dirs:
        for path in Path(".").glob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
    
    # Remove .so files from src
    for so_file in Path("src").glob("*.so"):
        so_file.unlink(missing_ok=True)
    
    # Remove .c file from root
    c_file = Path("git_service_cython.c")
    if c_file.exists():
        c_file.unlink()
    
    # Remove __pycache__ directories
    for pycache in Path(".").rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)
    
    print("✅ Cleaned build artifacts")
    
    # Check if Cython is available
    print()
    print("🔍 Checking build dependencies...")
    try:
        import Cython
        print("✅ Cython found")
    except ImportError:
        print("⚠️  Cython not found. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "Cython", "build"], check=True)
    
    # Build the package
    print()
    print("📦 Building package...")
    try:
        subprocess.run([sys.executable, "-m", "build"], check=True)
        print("✅ Build complete!")
    except subprocess.CalledProcessError as e:
        print(f"❌ Build failed: {e}")
        sys.exit(1)
    
    # Show results
    print()
    print("📊 Build results:")
    if dist_dir.exists():
        for item in sorted(dist_dir.iterdir()):
            size = item.stat().st_size / 1024  # KB
            print(f"   {item.name} ({size:.1f} KB)")
    else:
        print("⚠️  No dist/ folder created")
    
    print()
    print("📁 Previous builds archived in: previous_builds")
    archived = sorted(archive_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for item in archived[:5]:
        print(f"   {item.name}")


if __name__ == "__main__":
    main()

