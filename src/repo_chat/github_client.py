import asyncio
import base64
import fnmatch
import os
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"


class GitHubError(Exception):
    pass


class RateLimitError(GitHubError):
    pass


class GitHubClient:
    """Async GitHub API client with in-memory cache. Nothing is ever written to disk."""

    def __init__(self) -> None:
        self._token = os.environ.get("GITHUB_TOKEN")
        # Caches keyed by (owner, repo, branch, path) or subsets
        self._file_cache: dict[tuple[str, str, str, str], str] = {}
        self._tree_cache: dict[tuple[str, str, str], list[str]] = {}
        self._repo_info_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._http = httpx.AsyncClient(
            headers=self._build_headers(),
            timeout=30.0,
            follow_redirects=True,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _check(self, resp: httpx.Response) -> None:
        if resp.status_code in (403, 429):
            hint = ""
            if not self._token:
                hint = " Set the GITHUB_TOKEN environment variable to raise the limit from 60 to 5 000 req/hr."
            raise RateLimitError(f"GitHub API rate limit exceeded (HTTP {resp.status_code}).{hint}")
        if resp.status_code == 404:
            raise GitHubError(f"Not found: {resp.url}")
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_repo_info(self, owner: str, repo: str) -> dict[str, Any]:
        key = (owner, repo)
        if key in self._repo_info_cache:
            return self._repo_info_cache[key]
        resp = await self._http.get(f"{GITHUB_API}/repos/{owner}/{repo}")
        self._check(resp)
        data = resp.json()
        info: dict[str, Any] = {
            "name": data["name"],
            "full_name": data["full_name"],
            "description": data.get("description") or "",
            "default_branch": data["default_branch"],
            "language": data.get("language") or "",
            "stars": data.get("stargazers_count", 0),
            "license": (data.get("license") or {}).get("name") or "",
        }
        self._repo_info_cache[key] = info
        return info

    async def get_file_tree(
        self, owner: str, repo: str, branch: str, max_files: int = 1000
    ) -> list[str]:
        key = (owner, repo, branch)
        if key in self._tree_cache:
            return self._tree_cache[key]
        resp = await self._http.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        self._check(resp)
        data = resp.json()
        files = [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ][:max_files]
        self._tree_cache[key] = files
        return files

    async def get_file_content(
        self, owner: str, repo: str, branch: str, path: str
    ) -> str:
        key = (owner, repo, branch, path)
        if key in self._file_cache:
            return self._file_cache[key]
        resp = await self._http.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            params={"ref": branch},
        )
        self._check(resp)
        data = resp.json()
        raw = data.get("content", "")
        # GitHub returns base64 with newlines
        content = base64.b64decode(raw.replace("\n", "")).decode("utf-8", errors="replace")
        self._file_cache[key] = content
        return content

    async def get_multiple_files(
        self, owner: str, repo: str, branch: str, paths: list[str]
    ) -> dict[str, str]:
        paths = paths[:10]
        results = await asyncio.gather(
            *[self.get_file_content(owner, repo, branch, p) for p in paths],
            return_exceptions=True,
        )
        return {
            path: (r if not isinstance(r, Exception) else f"Error: {r}")
            for path, r in zip(paths, results)
        }

    async def search_files(
        self, owner: str, repo: str, branch: str, pattern: str
    ) -> list[str]:
        files = await self.get_file_tree(owner, repo, branch)
        matches = fnmatch.filter(files, pattern)
        if not matches:
            matches = [f for f in files if pattern in f]
        return matches

    async def compare_files(
        self, file_specs: list[dict[str, str]]
    ) -> dict[str, str]:
        async def _fetch(spec: dict[str, str]) -> tuple[str, str]:
            content = await self.get_file_content(
                spec["owner"], spec["repo"], spec["branch"], spec["path"]
            )
            return f"{spec['owner']}/{spec['repo']}:{spec['path']}", content

        results = await asyncio.gather(
            *[_fetch(s) for s in file_specs], return_exceptions=True
        )
        out: dict[str, str] = {}
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                spec = file_specs[i]
                k = f"{spec['owner']}/{spec['repo']}:{spec['path']}"
                out[k] = f"Error: {r}"
            else:
                k, v = r
                out[k] = v
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
