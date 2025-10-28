#!/usr/bin/env python3
"""Test script to verify pygitzen installation and basic functionality."""

import sys
import subprocess
from pathlib import Path


def test_import():
    """Test that pygitzen can be imported."""
    try:
        import pygitzen
        print("✓ pygitzen imports successfully")
        print(f"  Version: {pygitzen.__version__}")
        return True
    except ImportError as e:
        print(f"✗ Failed to import pygitzen: {e}")
        return False


def test_cli():
    """Test that the CLI command is available."""
    try:
        result = subprocess.run([sys.executable, "-m", "pygitzen", "--help"], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print("✓ CLI command works")
            return True
        else:
            print(f"✗ CLI command failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ CLI test failed: {e}")
        return False


def test_git_service():
    """Test GitService in current directory."""
    try:
        from pygitzen.git_service import GitService
        git = GitService(".")
        branches = git.list_branches()
        print(f"✓ GitService works - found {len(branches)} branches")
        return True
    except Exception as e:
        print(f"✗ GitService test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("Testing pygitzen installation...")
    print()
    
    tests = [
        ("Import test", test_import),
        ("CLI test", test_cli),
        ("GitService test", test_git_service),
    ]
    
    passed = 0
    for name, test_func in tests:
        print(f"{name}:")
        if test_func():
            passed += 1
        print()
    
    print(f"Tests passed: {passed}/{len(tests)}")
    
    if passed == len(tests):
        print("🎉 All tests passed! pygitzen is ready to use.")
        print("\nTo run pygitzen:")
        print("  pygitzen")
    else:
        print("❌ Some tests failed. Check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
