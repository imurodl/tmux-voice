"""Key terms sent to the STT provider so project names, files and tools are spelled correctly."""

import json
import re
import subprocess
import time
import tomllib
from pathlib import Path

from .config import CONFIG_DIR

BUILTIN = [
    "Claude", "Claude Code", "CLAUDE.md", "AGENTS.md", "Codex", "tmux", "Neovim", "README",
    "TypeScript", "JavaScript", "Python", "React", "Next.js", "Node.js", "FastAPI", "Postgres",
    "Docker", "Docker Compose", "Kubernetes", "GitHub", "pull request", "commit", "rebase",
    "npm", "pnpm", "uv", "pytest", "useEffect", "useState", "middleware", "endpoint", "env",
    "localhost", "API", "JSON", "YAML", "regex", "refactor", "migration", "SSH",
]
BAD = re.compile(r"[<>{}\[\]\\]")
NOISE = re.compile(r"(^node_modules$|^__pycache__$|\.lock$|-lock\.ya?ml$|^(LICENSE|Makefile)$)")
_cache: dict[str, tuple[float, list[str]]] = {}


def _git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _ok(t: str) -> bool:
    return 1 < len(t) < 50 and len(t.split()) <= 5 and not BAD.search(t) and not NOISE.search(t)


def global_terms(cfg: dict) -> list[str]:
    v = cfg["vocab"]
    terms = list(BUILTIN) if v["builtin"] else []
    terms += v["extra"]
    custom = CONFIG_DIR / "vocab.txt"
    if custom.exists():
        terms += [ln.strip() for ln in custom.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    if v["projects_dir"]:
        root = Path(v["projects_dir"]).expanduser()
        if root.is_dir():
            recent = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                      and time.time() - p.stat().st_mtime < 60 * 86400]
            terms += [p.name for p in sorted(recent, key=lambda p: -p.stat().st_mtime)][: v["recent_projects"]]
    ssh = Path.home() / ".ssh" / "config"
    if v["ssh_hosts"] and ssh.exists():
        for line in ssh.read_text(errors="ignore").splitlines():
            m = re.match(r"\s*Host\s+(.+)", line)
            if m:
                terms += [h for h in m.group(1).split() if "*" not in h]
    return terms


def project_terms(cwd: Path, cfg: dict) -> tuple[list[str], list[str]]:
    """(high priority: repo, branch, recently changed files; low priority: top-level entries, dependencies)."""
    v = cfg["vocab"]
    root = _git(cwd, "rev-parse", "--show-toplevel").strip()
    root = Path(root) if root else cwd
    branch = _git(root, "branch", "--show-current").strip()
    high = [root.name] + ([branch] if branch not in ("", "main", "master") else [])
    low: list[str] = []
    if v["project_files"]:
        changed = [ln[3:] for ln in _git(root, "status", "--porcelain").splitlines()]
        changed += _git(root, "log", "-n", "30", "--name-only", "--format=").splitlines()
        names = [Path(f.strip()).name for f in changed if f.strip()]
        high += [n for n in names if not n.startswith(("test_", "conftest"))][:15]
        try:
            low += [p.name for p in root.iterdir() if not p.name.startswith(".")][:30]
        except OSError:
            pass
    if v["dependencies"]:
        deps: list[str] = []
        pkg = root / "package.json"
        if pkg.exists():
            try:
                d = json.loads(pkg.read_text())
                deps += list(d.get("dependencies", {})) + list(d.get("devDependencies", {}))
            except ValueError:
                pass
        py = root / "pyproject.toml"
        if py.exists():
            try:
                reqs = tomllib.loads(py.read_text()).get("project", {}).get("dependencies", [])
                deps += [re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0] for d in reqs]
            except (ValueError, tomllib.TOMLDecodeError):
                pass
        low += [d for d in deps if d.count("-") < 2][:25]
    return high, low


def terms_for(cwd: str | None, cfg: dict) -> list[str]:
    key = cwd or ""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    high, low = project_terms(Path(cwd), cfg) if cwd and Path(cwd).is_dir() else ([], [])
    seen, out = set(), []
    for t in high + global_terms(cfg) + low:
        t = t.strip()
        if _ok(t) and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    out = out[: cfg["vocab"]["max_terms"]]
    _cache[key] = (time.time(), out)
    return out
