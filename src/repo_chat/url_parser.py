import re
from dataclasses import dataclass


@dataclass
class RepoSpec:
    owner: str
    repo: str
    branch: str | None = None
    path: str | None = None

    def display_name(self) -> str:
        if self.branch:
            return f"{self.owner}/{self.repo}@{self.branch}"
        return f"{self.owner}/{self.repo}"

    def short_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def cache_key(self) -> tuple[str, str, str]:
        return (self.owner, self.repo, self.branch or "")


_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/([^/\s]+)(?:/(.+))?)?/?$"
)


def parse_github_url(url: str) -> RepoSpec:
    m = _GITHUB_URL_RE.match(url.strip().rstrip("/"))
    if not m:
        raise ValueError(f"Invalid GitHub URL: {url!r}")
    owner, repo, branch, path = m.groups()
    return RepoSpec(owner=owner, repo=repo, branch=branch or None, path=path or None)
