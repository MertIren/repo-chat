import asyncio
import json
import re
import sys
from dataclasses import dataclass, field

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .github_client import GitHubClient, RateLimitError
from .url_parser import RepoSpec, parse_github_url

console = Console()

CLAUDE_CMD = ["claude", "--no-session-persistence", "--tools", ""]


async def _call_claude(system: str, prompt: str) -> str:
    """One-shot Claude call, returns full text response."""
    proc = await asyncio.create_subprocess_exec(
        *CLAUDE_CMD, "-p", prompt, "--system-prompt", system,
        "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "claude CLI failed")
    return stdout.decode().strip()


async def _stream_claude(system: str, prompt: str):
    """Async generator that yields text chunks as they stream from Claude."""
    proc = await asyncio.create_subprocess_exec(
        *CLAUDE_CMD, "-p", prompt, "--system-prompt", system,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    prev_len = 0
    async for raw in proc.stdout:
        line = raw.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    full = block["text"]
                    if len(full) > prev_len:
                        yield full[prev_len:]
                        prev_len = len(full)
    await proc.wait()


@dataclass
class RepoChatSession:
    repos: list[RepoSpec] = field(default_factory=list)
    file_trees: dict[str, list[str]] = field(default_factory=dict)  # "owner/repo@branch" -> paths
    fetched_files: dict[tuple[str, str, str, str], str] = field(default_factory=dict)
    conversation: list[dict] = field(default_factory=list)


class RepoChatCLI:
    def __init__(self, initial_urls: list[str]) -> None:
        self.initial_urls = initial_urls
        self.gh = GitHubClient()
        self.session = RepoChatSession()

    # ------------------------------------------------------------------
    # Repo management
    # ------------------------------------------------------------------

    async def _load_repo(self, url: str) -> RepoSpec:
        spec = parse_github_url(url)
        if not spec.branch:
            info = await self.gh.get_repo_info(spec.owner, spec.repo)
            spec.branch = info["default_branch"]
        files = await self.gh.get_file_tree(spec.owner, spec.repo, spec.branch)
        tree_key = f"{spec.owner}/{spec.repo}@{spec.branch}"
        self.session.file_trees[tree_key] = files
        self.session.repos.append(spec)
        return spec

    # ------------------------------------------------------------------
    # Context builders
    # ------------------------------------------------------------------

    def _trees_context(self) -> str:
        parts = []
        for spec in self.session.repos:
            key = f"{spec.owner}/{spec.repo}@{spec.branch}"
            files = self.session.file_trees.get(key, [])
            parts.append(
                f"Repository: {spec.owner}/{spec.repo} (branch: {spec.branch})\n"
                + "\n".join(files)
            )
        return "\n\n".join(parts)

    def _files_context(self) -> str:
        if not self.session.fetched_files:
            return ""
        by_repo: dict[str, list[tuple[str, str]]] = {}
        for (owner, repo, branch, path), content in self.session.fetched_files.items():
            k = f"{owner}/{repo}@{branch}"
            by_repo.setdefault(k, []).append((path, content))
        parts = []
        for repo_key, entries in by_repo.items():
            parts.append(f"=== Files from {repo_key} ===")
            for path, content in entries:
                parts.append(f"--- {path} ---\n{content}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Claude calls
    # ------------------------------------------------------------------

    async def _select_files(self, question: str) -> list[dict]:
        trees = self._trees_context()
        fetched = self._files_context()

        system = (
            "You are a file selector. Given file trees from one or more GitHub repositories "
            "and a user question, choose 0–8 files that are most relevant to answer the question.\n\n"
            "Return ONLY valid JSON in this exact format with no explanation:\n"
            '{"files": [{"owner": "...", "repo": "...", "branch": "...", "path": "..."}]}\n\n'
            "If no files are needed (e.g. the question is about tree structure), return: "
            '{"files": []}'
        )
        user_msg = f"## File Trees\n{trees}\n\n"
        if fetched:
            user_msg += f"## Already Fetched Files (reuse these if sufficient)\n{fetched}\n\n"
        user_msg += f"## Question\n{question}"

        text = await _call_claude(system, user_msg)
        # Strip markdown code fences if present
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        try:
            return json.loads(text).get("files", [])
        except Exception:
            return []

    async def _fetch_selected(self, file_specs: list[dict]) -> int:
        to_fetch = [
            s for s in file_specs
            if (s["owner"], s["repo"], s["branch"], s["path"]) not in self.session.fetched_files
        ]
        if not to_fetch:
            return 0
        tasks = [
            self.gh.get_file_content(s["owner"], s["repo"], s["branch"], s["path"])
            for s in to_fetch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fetched_count = 0
        for spec, result in zip(to_fetch, results):
            if not isinstance(result, Exception):
                key = (spec["owner"], spec["repo"], spec["branch"], spec["path"])
                self.session.fetched_files[key] = result
                fetched_count += 1
        return fetched_count

    async def _answer(self, question: str) -> str:
        trees = self._trees_context()
        fetched = self._files_context()

        system = f"You are a helpful assistant that answers questions about GitHub repositories.\n\n## File Trees\n{trees}"
        if fetched:
            system += f"\n\n## File Contents\n{fetched}"

        # Embed prior conversation turns so the stateless CLI has context
        if self.session.conversation:
            history = "\n\n".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
                for m in self.session.conversation
            )
            system += f"\n\n## Conversation so far\n{history}\n\nContinue the conversation by responding to the latest User message."

        self.session.conversation.append({"role": "user", "content": question})

        full_response = ""
        console.print("[dim]...[/dim]", end="\r")
        async for chunk in _stream_claude(system, question):
            full_response += chunk
        console.print(" " * 10, end="\r")  # clear the "..." line
        console.print(Markdown(full_response, justify="left"))
        console.print()

        self.session.conversation.append({"role": "assistant", "content": full_response})
        return full_response

    # ------------------------------------------------------------------
    # Context summary
    # ------------------------------------------------------------------

    def _show_context_summary(self) -> None:
        by_repo: dict[str, int] = {}
        for (owner, repo, _, _) in self.session.fetched_files:
            k = f"{owner}/{repo}"
            by_repo[k] = by_repo.get(k, 0) + 1
        total_chars = sum(len(c) for c in self.session.fetched_files.values())
        approx_tokens = total_chars // 4
        if not by_repo and not self.session.fetched_files:
            return
        parts = [f"{repo}: {n} file(s)" for repo, n in by_repo.items()]
        parts.append(f"~{approx_tokens:,} tokens")
        console.print(f"[dim]Context: {' · '.join(parts)}[/dim]\n")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(self, cmd: str) -> bool:
        """Return False to exit."""
        parts = cmd.strip().split(None, 1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name == "/exit":
            console.print("[dim]Goodbye![/dim]")
            return False

        elif name == "/add":
            if not arg:
                console.print("[yellow]Usage: /add <github-url>[/yellow]")
            else:
                try:
                    with console.status(f"[dim]Loading {arg}...[/dim]"):
                        spec = await self._load_repo(arg)
                    key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                    n = len(self.session.file_trees.get(key, []))
                    console.print(f"[green]Added[/green] {spec.owner}/{spec.repo} ({n} files, branch: {spec.branch})")
                except Exception as e:
                    console.print(f"[red]Error:[/red] {e}")

        elif name == "/clear":
            self.session.conversation.clear()
            self.session.fetched_files.clear()
            console.print("[green]Conversation and file cache cleared.[/green]")

        elif name == "/files":
            if not self.session.fetched_files:
                console.print("[dim]No files in context.[/dim]")
            else:
                by_repo: dict[str, list[str]] = {}
                for (owner, repo, branch, path) in self.session.fetched_files:
                    k = f"{owner}/{repo}@{branch}"
                    by_repo.setdefault(k, []).append(path)
                for repo_key, paths in by_repo.items():
                    console.print(f"[bold cyan]{repo_key}[/bold cyan]")
                    for p in sorted(paths):
                        console.print(f"  {p}")

        elif name == "/tree":
            target = arg.lower() if arg else None
            for spec in self.session.repos:
                short = spec.short_name()
                if target and target not in short.lower():
                    continue
                key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                files = self.session.file_trees.get(key, [])
                console.print(f"\n[bold cyan]{short}[/bold cyan] ({spec.branch}, {len(files)} files)")
                for f in files:
                    console.print(f"  {f}")

        else:
            console.print(f"[yellow]Unknown command:[/yellow] {name}  (try /add, /clear, /files, /tree, /exit)")

        return True

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        console.print(
            Panel.fit(
                "[bold blue]repo-chat[/bold blue]  Ask questions about GitHub repositories without cloning them",
                border_style="blue",
            )
        )

        errors: dict[str, str] = {}
        with console.status(f"[dim]Fetching {len(self.initial_urls)} repo(s)...[/dim]"):
            async def _load(url: str) -> tuple[str, str | None]:
                try:
                    await self._load_repo(url)
                    return url, None
                except Exception as e:
                    return url, str(e)

            results = await asyncio.gather(*[_load(u) for u in self.initial_urls])

        for url, err in results:
            if err:
                errors[url] = err

        if self.session.repos:
            table = Table(title="Loaded Repositories", show_header=True, header_style="bold cyan")
            table.add_column("Repository")
            table.add_column("Branch")
            table.add_column("Files", justify="right")
            for spec in self.session.repos:
                key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                n = len(self.session.file_trees.get(key, []))
                table.add_row(spec.short_name(), spec.branch or "?", str(n))
            console.print(table)

        for url, err in errors.items():
            console.print(f"[red]Error loading {url}:[/red] {err}")

        if not self.session.repos:
            console.print("[red]No repos loaded. Exiting.[/red]")
            sys.exit(1)

        console.print(
            "\n[dim]Commands: /add <url>  /clear  /files  /tree [repo]  /exit[/dim]\n"
        )

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------

    def _prompt(self) -> str:
        names = [r.short_name() for r in self.session.repos]
        return f"[{', '.join(names)}] > "

    async def run(self) -> None:
        await self._startup()

        # Enable readline history if available
        try:
            import readline  # noqa: F401
        except ImportError:
            pass

        try:
            while True:
                try:
                    console.print(f"[bold green]{self._prompt()}[/bold green]", end="")
                    user_input = input()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye![/dim]")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    if not await self._handle_command(user_input):
                        break
                    continue

                try:
                    # Stage 1 — decide which files to fetch
                    with console.status("[dim]Deciding which files to read...[/dim]"):
                        file_specs = await self._select_files(user_input)

                    # Stage 2 — fetch new files
                    if file_specs:
                        new = [
                            s for s in file_specs
                            if (s["owner"], s["repo"], s["branch"], s["path"])
                            not in self.session.fetched_files
                        ]
                        if new:
                            repo_count = len({(s["owner"], s["repo"]) for s in new})
                            with console.status(
                                f"[dim]Fetching {len(new)} file(s) across {repo_count} repo(s)...[/dim]"
                            ):
                                await self._fetch_selected(file_specs)

                    # Stage 3 — stream the answer
                    await self._answer(user_input)
                    self._show_context_summary()

                except (KeyboardInterrupt, asyncio.CancelledError):
                    console.print("\n[dim]Cancelled.[/dim]\n")
                except RateLimitError as e:
                    console.print(f"\n[red]Rate limit:[/red] {e}")
                except Exception as e:
                    console.print(f"\n[red]Error:[/red] {e}")
        finally:
            await self.gh.aclose()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        console.print(
            "Usage: [bold]repo-chat[/bold] <github-url> [<github-url> ...]\n\n"
            "Example:\n"
            "  repo-chat https://github.com/anthropics/anthropic-sdk-python\n"
            "  repo-chat https://github.com/owner/repo1 https://github.com/owner/repo2"
        )
        sys.exit(0 if "--help" in sys.argv or "-h" in sys.argv else 1)

    try:
        asyncio.run(RepoChatCLI(sys.argv[1:]).run())
    except KeyboardInterrupt:
        pass
