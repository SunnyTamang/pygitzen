#!/usr/bin/env python3
"""
Setup script for pygitzen package.
This is a fallback for older pip versions that don't support pyproject.toml.
"""

from setuptools import setup, find_packages
from setuptools.extension import Extension

# Try to import Cython, but don't fail if it's not available
# (It will be available during build due to build-system requirements)
try:
    from Cython.Build import cythonize
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False
    # Fallback: if Cython not available, we'll skip the extension
    # (This shouldn't happen during build, but helps with setup.py imports)
    def cythonize(*args, **kwargs):
        return []

extensions = [
    Extension(
        "git_service_cython",
        ["git_service_cython.pyx"],
        include_dirs=[],
        extra_compile_args=["-O3"],  # Optimize for speed
    )
]

# Read the README file
def read_readme():
    try:
        with open("README_PYPI.md", "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""

setup(
    name="pygitzen",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "initializedcheck": False,
        },
    ) if CYTHON_AVAILABLE else [],
    version="0.1.3-rc1",
    author="Sunny Tamang",
    author_email="sunnysinghtamang@gmail.com",
    description="A Python-native LazyGit-like TUI using Textual and dulwich",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/SunnyTamang/pygitzen",
    project_urls={
        "Homepage": "https://github.com/SunnyTamang/pygitzen",
        "Repository": "https://github.com/SunnyTamang/pygitzen",
        "Issues": "https://github.com/SunnyTamang/pygitzen/issues",
    },
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Version Control :: Git",
        "Topic :: Terminals",
    ],
    python_requires=">=3.9",
    install_requires=[
        "textual>=0.58",
        "rich>=13.7",
        "dulwich>=0.22",
        "typing-extensions>=4.7; python_version<'3.11'",
    ],
    entry_points={
        "console_scripts": [
            "pygitzen=pygitzen.__main__:main",
        ],
    },
    keywords=[
        "git", "tui", "terminal", "textual", "dulwich", "lazygit",
        "version-control", "git-client", "terminal-ui", "cli", "git-manager"
    ],
    include_package_data=True,
    zip_safe=False,
    setup_requires=[
        "Cython>=3.0.0",  # Required for building from source (fallback for older pip)
    ],
)
