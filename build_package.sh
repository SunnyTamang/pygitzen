#!/bin/bash
# Build script for pygitzen package
# Moves previous build to archive folder, cleans, and builds fresh package

set -e  # Exit on error

echo "🔨 Building pygitzen package..."
echo ""

# Create archive directory if it doesn't exist
ARCHIVE_DIR="previous_builds"
if [ ! -d "$ARCHIVE_DIR" ]; then
    mkdir -p "$ARCHIVE_DIR"
    echo "📁 Created archive directory: $ARCHIVE_DIR"
fi

# Move existing dist/ to archive if it exists
if [ -d "dist" ] && [ -n "$(ls -A dist 2>/dev/null)" ]; then
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    ARCHIVE_PATH="$ARCHIVE_DIR/dist_$TIMESTAMP"
    echo "📦 Moving existing dist/ to: $ARCHIVE_PATH"
    mv dist "$ARCHIVE_PATH"
    echo "✅ Archived previous build"
else
    echo "ℹ️  No existing dist/ to archive"
fi

# Clean build artifacts
echo ""
echo "🧹 Cleaning build artifacts..."
rm -rf build/
rm -rf *.egg-info/
rm -rf src/pygitzen.egg-info/
rm -rf src/*.so
rm -f git_service_cython.c
rm -f *.pyc
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo "✅ Cleaned build artifacts"

# Check if Cython is available
echo ""
echo "🔍 Checking build dependencies..."
if ! python3 -c "import Cython" 2>/dev/null; then
    echo "⚠️  Cython not found. Installing..."
    pip install Cython build
else
    echo "✅ Cython found"
fi

# Build the package
echo ""
echo "📦 Building package..."
python3 -m build

# Show results
echo ""
echo "✅ Build complete!"
echo ""
echo "📊 Build results:"
ls -lh dist/ 2>/dev/null || echo "⚠️  No dist/ folder created"
echo ""
echo "📁 Previous builds archived in: $ARCHIVE_DIR"
ls -1t "$ARCHIVE_DIR" 2>/dev/null | head -5 || echo "   (none yet)"

