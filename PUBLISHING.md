# PyPI Publishing Guide for pygitzen

## Prerequisites

1. Install build tools:
```bash
pip install build twine
```

2. Create PyPI account at https://pypi.org/account/register/
3. Create TestPyPI account at https://test.pypi.org/account/register/

## Local Development & Testing

### 1. Install in Development Mode
```bash
# From project root
pip install -e .
```

### 2. Test the Package
```bash
# Test in a git repository
cd /path/to/any/git/repo
pygitzen
```

### 3. Run Tests (if you add them later)
```bash
python -m pytest tests/
```

## Building the Package

### 1. Clean Previous Builds
```bash
rm -rf dist/ build/ *.egg-info/
```

### 2. Build Source and Wheel Distributions
```bash
python -m build
```

This creates:
- `dist/pygitzen-0.1.0.tar.gz` (source distribution)
- `dist/pygitzen-0.1.0-py3-none-any.whl` (wheel distribution)

## Publishing to PyPI

### Option 1: Upload to TestPyPI First (Recommended)

1. Upload to TestPyPI:
```bash
python -m twine upload --repository testpypi dist/*
```

2. Test installation from TestPyPI:
```bash
pip install --index-url https://test.pypi.org/simple/ pygitzen
```

3. If successful, upload to real PyPI:
```bash
python -m twine upload dist/*
```

### Option 2: Direct Upload to PyPI
```bash
python -m twine upload dist/*
```

## Version Management

Before each release, update the version in `pyproject.toml`:
```toml
version = "0.1.1"  # or "0.2.0", "1.0.0", etc.
```

## Installation from PyPI

Once published, users can install with:
```bash
pip install pygitzen
```

## Troubleshooting

### Common Issues:

1. **Package name conflicts**: If "pygitzen" is taken, change the name in `pyproject.toml`
2. **Missing files**: Ensure `MANIFEST.in` includes all necessary files
3. **Build errors**: Check that all dependencies are properly specified
4. **Upload errors**: Verify PyPI credentials and package name availability

### Useful Commands:

```bash
# Check package contents
python -m build --wheel
unzip -l dist/pygitzen-*.whl

# Validate package
twine check dist/*

# View package info
pip show pygitzen
```
