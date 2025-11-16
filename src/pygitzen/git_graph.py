"""
Git graph visualization utilities with color support.

This module provides functions to extract and render git log graph visualization
with colors, matching git log --graph --color=always output.
"""

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rich.text import Text


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def ansi_to_rich_style(ansi_code: str) -> Optional[str]:
    """
    Convert ANSI color code to Rich Text style name.
    
    Args:
        ansi_code: ANSI code like '32', '33', '1;32', etc.
    
    Returns:
        Rich style name like 'green', 'yellow', 'bright_green', or None for reset/default
    """
    # ANSI color code to Rich style mapping
    color_map = {
        '30': 'black', '31': 'red', '32': 'green', '33': 'yellow',
        '34': 'blue', '35': 'magenta', '36': 'cyan', '37': 'white',
        '90': 'bright_black', '91': 'bright_red', '92': 'bright_green',
        '93': 'bright_yellow', '94': 'bright_blue', '95': 'bright_magenta',
        '96': 'bright_cyan', '97': 'bright_white'
    }
    
    # Handle bold/bright colors (e.g., '1;32' for bold green)
    codes = ansi_code.split(';')
    style = None
    
    for code in codes:
        if code in color_map:
            style = color_map[code]
        elif code == '1' and style:
            # Bold modifier - convert to bright version if not already
            if not style.startswith('bright_'):
                style = f"bright_{style}"
        elif code == '0' or code == '':
            # Reset
            style = None
    
    return style


# ANSI escape sequence regex for faster parsing
_ansi_escape_re = re.compile(r'\x1B\[([0-9;]*)m')

def parse_ansi_to_rich_text(text_with_ansi: str) -> Text:
    """
    Parse text with ANSI color codes and convert to Rich Text with proper styling.
    Optimized version using regex for faster parsing.
    
    Args:
        text_with_ansi: Text containing ANSI escape codes
    
    Returns:
        Rich Text object with colors applied
    """
    result = Text()
    current_style = None
    last_pos = 0
    
    # Find all ANSI escape sequences and their positions
    for match in _ansi_escape_re.finditer(text_with_ansi):
        # Add text before this ANSI code
        if match.start() > last_pos:
            text_segment = text_with_ansi[last_pos:match.start()]
            if text_segment:
                if current_style:
                    result.append(text_segment, style=current_style)
                else:
                    result.append(text_segment)
        
        # Process ANSI code
        ansi_code = match.group(1)
        if ansi_code == '' or ansi_code == '0':
            # Reset
            current_style = None
        else:
            current_style = ansi_to_rich_style(ansi_code)
        
        last_pos = match.end()
    
    # Add remaining text after last ANSI code
    if last_pos < len(text_with_ansi):
        text_segment = text_with_ansi[last_pos:]
        if text_segment:
            if current_style:
                result.append(text_segment, style=current_style)
            else:
                result.append(text_segment)
    
    return result


def get_git_colored_graph(repo_path: Path, max_count: int = 50, branch: Optional[str] = None) -> Tuple[Dict[str, str], Dict]:
    """
    Get colored graph prefixes from git log --graph --color=always.
    
    Args:
        repo_path: Path to git repository
        max_count: Maximum number of commits to retrieve
        branch: Branch name (None for all branches)
    
    Returns:
        Tuple of (graph_prefixes, graph_prefixes_colored) where:
        - graph_prefixes: Maps commit SHA to plain graph prefix string (or list if has continuation lines)
        - graph_prefixes_colored: Maps commit SHA to colored graph prefix string (or list [main, cont1, cont2, ...] if has continuation lines)
    """
    graph_prefixes = {}  # sha -> plain graph prefix string or list
    graph_prefixes_colored = {}  # sha -> colored graph prefix string or list (with ANSI codes)
    
    try:
        # Build git command
        cmd = ['git', 'log', '--graph', '--color=always', 
               '--format=%x00%H|%s|%an|%at|%P', 
               f'--max-count={max_count}']
        
        if branch:
            cmd.append(branch)
        else:
            cmd.append('--all')
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10
        )
        
        if result.returncode != 0:
            return graph_prefixes, graph_prefixes_colored
        
        # Parse output - git log --graph prefixes each line with graph characters
        # Format: graph_chars \x00 commit_info OR just graph_chars (continuation lines)
        lines = result.stdout.strip().split('\n')
        last_commit_sha = None
        
        for line in lines:
            if not line:
                continue
            
            # Check if this is a continuation line (just graph chars, no null byte)
            if '\x00' not in line:
                # This is a continuation line like "|\ " or "|/ "
                # Keep both colored and plain versions
                graph_chars = strip_ansi_codes(line.rstrip())
                graph_chars_colored = line.rstrip()  # Keep ANSI codes
                if graph_chars and last_commit_sha:
                    # Store continuation line for the last commit
                    if last_commit_sha not in graph_prefixes:
                        graph_prefixes[last_commit_sha] = []
                        graph_prefixes_colored[last_commit_sha] = []
                    if not isinstance(graph_prefixes[last_commit_sha], list):
                        graph_prefixes[last_commit_sha] = [graph_prefixes[last_commit_sha]]
                        graph_prefixes_colored[last_commit_sha] = [graph_prefixes_colored[last_commit_sha]]
                    graph_prefixes[last_commit_sha].append(graph_chars)
                    graph_prefixes_colored[last_commit_sha].append(graph_chars_colored)
                continue
            
            # Split on null byte - first part is graph, second part is commit info
            parts = line.split('\x00', 1)
            if len(parts) < 2:
                continue
            
            # Don't strip - preserve git's spacing (merge commits have extra spaces)
            # Keep both colored and plain versions
            graph_part_raw = parts[0]  # Graph characters with ANSI codes
            graph_part = strip_ansi_codes(graph_part_raw)  # Plain text version
            commit_part = parts[1]
            
            # Parse commit info
            commit_parts = commit_part.split('|')
            if len(commit_parts) >= 4:
                sha = commit_parts[0]
                
                # Store the graph prefix for this commit
                # Preserve git's exact spacing - git uses "*   " (3 spaces) for merge commits
                # We'll normalize to have at least one space, but preserve extra spaces for merges
                # Remove only excessive trailing spaces (more than 3), keep 1-3 spaces
                trailing_spaces = len(graph_part) - len(graph_part.rstrip())
                if trailing_spaces > 3:
                    # Too many spaces, normalize to 1
                    graph_prefix = graph_part.rstrip() + " "
                    graph_prefix_colored = graph_part_raw.rstrip() + " "
                else:
                    # Preserve git's spacing (1-3 spaces)
                    graph_prefix = graph_part.rstrip() + (" " * max(1, trailing_spaces))
                    graph_prefix_colored = graph_part_raw.rstrip() + (" " * max(1, trailing_spaces))
                
                if graph_prefix.strip():  # Make sure there's actual graph content
                    graph_prefixes[sha] = graph_prefix
                    graph_prefixes_colored[sha] = graph_prefix_colored
                    last_commit_sha = sha
    
    except Exception:
        # Return empty dicts on error
        pass
    
    return graph_prefixes, graph_prefixes_colored


def convert_graph_prefix_to_rich(graph_prefix_colored: str) -> Text:
    """
    Convert a colored graph prefix (with ANSI codes) to Rich Text.
    
    Args:
        graph_prefix_colored: Graph prefix string with ANSI color codes
    
    Returns:
        Rich Text object with colors applied
    """
    return parse_ansi_to_rich_text(graph_prefix_colored)

