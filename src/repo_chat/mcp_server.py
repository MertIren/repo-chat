"""MCP server that exposes GitHub repo browsing as stateless tools."""

import json
import os

from mcp.server.fastmcp import FastMCP

from .github_client import GitHubClient, GitHubError, RateLimitError

mcp = FastMCP(
    "repo-chat",
    instructions=(
        "Tools for browsing GitHub repositories without cloning them. "
        "All results are cached in memory on the server. "
        "Start with get_repo_info to resolve the default branch, "
        "then get_file_tree to see the structure, "
        "then get_file_content or get_multiple_files to read code."
    ),
)

# Shared in-memory client — cache persists for the lifetime of the server process
_client = GitHubClient()


def _fmt_error(e: Exception) -> str:
    if isinstance(e, RateLimitError):
        return str(e)
    if isinstance(e, GitHubError):
        return f"GitHub error: {e}"
    return f"Error: {e}"


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool()
async def get_repo_info(owner: str, repo: str) -> str:
    """Get basic metadata for a GitHub repository.

    Returns JSON with: name, description, default_branch, language, stars, license.
    Call this first to resolve the default branch before other calls.

    Args:
        owner: Repository owner (user or org name)
        repo: Repository name
    """
    try:
        info = await _client.get_repo_info(owner, repo)
        return json.dumps(info, indent=2)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def get_file_tree(
    owner: str, repo: str, branch: str, max_files: int = 1000
) -> str:
    """Get the full recursive file tree of a repository branch.

    Returns a newline-separated list of file paths. Result is cached by (owner, repo, branch).

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name (e.g. "main", "master")
        max_files: Maximum number of files to return (default 1000)
    """
    try:
        files = await _client.get_file_tree(owner, repo, branch, max_files=max_files)
        return "\n".join(files)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def get_file_content(owner: str, repo: str, branch: str, path: str) -> str:
    """Fetch a single file's content from GitHub. Never written to disk.

    Decoded from base64 in memory and returned as a string.
    Result is cached by (owner, repo, branch, path).

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name
        path: File path within the repo (e.g. "src/main.py")
    """
    try:
        return await _client.get_file_content(owner, repo, branch, path)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def get_multiple_files(
    owner: str, repo: str, branch: str, paths: list[str]
) -> str:
    """Batch-fetch up to 10 files from a repository in parallel.

    Returns JSON mapping path -> content. Uses the same cache as get_file_content.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name
        paths: List of file paths to fetch (max 10)
    """
    try:
        result = await _client.get_multiple_files(owner, repo, branch, paths)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def search_files(owner: str, repo: str, branch: str, pattern: str) -> str:
    """Search the file tree by glob pattern or substring.

    Examples: "*.test.ts", "config.*", "src/api/", "Dockerfile"
    Fetches the file tree first if not cached.

    Returns a newline-separated list of matching paths.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name
        pattern: Glob pattern (e.g. "**/*.py") or substring to match against file paths
    """
    try:
        matches = await _client.search_files(owner, repo, branch, pattern)
        if not matches:
            return f"No files matching {pattern!r}"
        return "\n".join(matches)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def compare_files(files: list[dict[str, str]]) -> str:
    """Fetch the same logical path across multiple repos in parallel for comparison.

    Returns JSON mapping "owner/repo:path" -> content.
    Useful for diffing how different repos implement the same thing.

    Args:
        files: List of dicts, each with keys: owner, repo, branch, path
               Example: [{"owner": "a", "repo": "x", "branch": "main", "path": "README.md"},
                         {"owner": "b", "repo": "y", "branch": "main", "path": "README.md"}]
    """
    try:
        result = await _client.compare_files(files)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _fmt_error(e)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main() -> None:
    mcp.run(transport="stdio")
