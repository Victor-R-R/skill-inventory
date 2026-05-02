#!/usr/bin/env python3
"""
skill-inventory — Manage your Claude Code skills across projects.

Commands:
  skill-inventory scan       Scan projects and skills
  skill-inventory list       List all skills found
  skill-inventory audit      Analyze skills locally — zero API needed
  skill-inventory clean      Interactive cleanup from last audit report
"""

import sys
import json
import re
import shutil
import datetime
import urllib.request
import urllib.error
import ssl
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# ── Colors ───────────────────────────────────────────────────────────────────
R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"

def h1(t): print(f"\n{BOLD}{CYAN}{t}{R}")
def h2(t): print(f"\n{BOLD}{t}{R}")
def ok(t):   print(f"  {GREEN}✓{R}  {t}")
def warn(t): print(f"  {YELLOW}⚠{R}  {t}")
def info(t): print(f"  {BLUE}·{R}  {t}")
def err(t):  print(f"  {RED}✗{R}  {t}")
def dim(t):  print(f"  {DIM}{t}{R}")
def sep():   print(f"\n{DIM}{'─' * 56}{R}")

# ── Config ───────────────────────────────────────────────────────────────────
HOME = Path.home()
PROJECT_ROOTS = [HOME]
GLOBAL_SKILLS_DIR = HOME / ".claude" / "skills"
PLUGIN_CACHE_DIR = HOME / ".claude" / "plugins" / "cache"
GLOBAL_CLAUDE_MD = HOME / ".claude" / "CLAUDE.md"
REPORT_FILE = HOME / ".claude" / "skill-inventory-report.json"

# ── Discover projects ─────────────────────────────────────────────────────────
def find_projects() -> list[Path]:
    """Find all projects under the root folders."""
    projects = []
    for root in PROJECT_ROOTS:
        if not root.exists():
            continue
        # A project is any folder with CLAUDE.md, package.json, .git, or pyproject.toml
        for candidate in sorted(root.iterdir()):
            if candidate.is_dir() and not candidate.name.startswith("."):
                markers = ["CLAUDE.md", "package.json", ".git", "pyproject.toml"]
                if any((candidate / m).exists() for m in markers):
                    projects.append(candidate)
    return projects

# ── Read skills from a directory ──────────────────────────────────────────────
SKIP_NAMES = {"README.md", "LICENSE.md", "CHANGELOG.md", "CONTRIBUTING.md"}
SKIP_DIRS  = {"_shared", "references", "pdf", "docs", "assets", "examples"}

def read_skills_in_dir(skills_dir: Path) -> list[dict]:
    """Read skill entry points from a directory.

    A skill is either:
      - A top-level .md file: skills/foo.md
      - A SKILL.md inside a direct subfolder: skills/foo/SKILL.md

    Sub-files, shared helpers, and reference docs are skipped.
    """
    skills = []
    if not skills_dir.exists():
        return skills

    scope = "global" if str(skills_dir).startswith(str(GLOBAL_SKILLS_DIR)) else "local"

    def _add(f: Path, name: str) -> None:
        content = safe_read(f)
        skills.append({
            "path": str(f),
            "name": name,
            "scope": scope,
            "project": _project_of(f),
            "content": content[:2000],
            "size": len(content),
            "description": _parse_description(content),
        })

    for entry in sorted(skills_dir.iterdir()):
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if entry.is_file() and entry.suffix == ".md":
            if entry.name not in SKIP_NAMES and entry.stem.upper() not in ("LICENSE", "CHANGELOG"):
                _add(entry, entry.stem)
        elif entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.exists():
                _add(skill_md, entry.name)
            # else: directory without SKILL.md — skip (plugin, archive, etc.)

    return skills

# ── Read plugin skills from cache ─────────────────────────────────────────────
def read_plugin_skills() -> list[dict]:
    """Scan ~/.claude/plugins/cache and return skills from the latest version of each plugin."""
    if not PLUGIN_CACHE_DIR.exists():
        return []

    def parse_ver(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    from collections import defaultdict
    ver_map: dict[tuple, list[str]] = defaultdict(list)
    for skill_md in PLUGIN_CACHE_DIR.rglob("SKILL.md"):
        rel = skill_md.relative_to(PLUGIN_CACHE_DIR)
        parts = rel.parts
        if len(parts) >= 3:
            mp, ns, ver = parts[0], parts[1], parts[2]
            ver_map[(mp, ns)].append(ver)

    latest = {k: sorted(set(v), key=parse_ver)[-1] for k, v in ver_map.items()}

    skills = []
    seen: set[str] = set()

    for skill_md in sorted(PLUGIN_CACHE_DIR.rglob("SKILL.md")):
        rel = skill_md.relative_to(PLUGIN_CACHE_DIR)
        parts = rel.parts
        if len(parts) < 3:
            continue
        mp, ns, ver = parts[0], parts[1], parts[2]
        if ver != latest.get((mp, ns)):
            continue  # skip old versions
        try:
            skills_idx = parts.index("skills")
            skill_name = parts[skills_idx + 1]
        except (ValueError, IndexError):
            continue
        if skill_name.startswith("_"):
            continue  # internal helper
        key = f"{ns}:{skill_name}"
        if key in seen:
            continue
        seen.add(key)
        content = safe_read(skill_md)
        skills.append({
            "path": str(skill_md),
            "name": f"{ns}:{skill_name}",
            "scope": "plugin",
            "namespace": ns,
            "plugin_name": skill_name,
            "content": content[:2000],
            "size": len(content),
            "description": _parse_description(content),
        })
    return skills

def _parse_description(content: str) -> str:
    """Extract the 'description' field from YAML frontmatter.
    Handles inline values, quoted strings, and multi-line block scalars (> and |).
    """
    match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return ""
    lines = match.group(1).splitlines()
    for i, line in enumerate(lines):
        if line.lower().startswith("description:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            # YAML block scalars: > or | mean the value is on the next indented lines
            if value in (">", "|", "|-", ">-", ">+", "|+"):
                desc_lines = []
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith((" ", "\t")):
                        desc_lines.append(lines[j].strip())
                    elif lines[j].strip() == "":
                        continue
                    else:
                        break
                return " ".join(desc_lines)
            return value
    return ""

def _project_of(path: Path) -> Optional[str]:
    """Return the project name a path belongs to."""
    for root in PROJECT_ROOTS:
        try:
            rel = path.relative_to(root)
            return rel.parts[0] if rel.parts else None
        except ValueError:
            continue
    return None

# ── Read CLAUDE.md ────────────────────────────────────────────────────────────
def read_claude_md(project: Path) -> str:
    p = project / "CLAUDE.md"
    return safe_read(p) if p.exists() else ""

def safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

# ── Full system snapshot ──────────────────────────────────────────────────────
def build_snapshot() -> dict:
    h1("▸ Scanning system")

    projects = find_projects()
    info(f"Projects found: {len(projects)}")

    all_skills = []
    project_contexts = []

    # Global skills
    global_skills = read_skills_in_dir(GLOBAL_SKILLS_DIR)
    all_skills.extend(global_skills)
    info(f"Global skills (~/.claude/skills/): {len(global_skills)}")

    # Plugin skills (from ~/.claude/plugins/cache/)
    plugin_skills = read_plugin_skills()
    all_skills.extend(plugin_skills)
    if plugin_skills:
        from collections import Counter
        ns_counts = Counter(s["namespace"] for s in plugin_skills)
        info(f"Plugin skills (~/.claude/plugins/cache/): {len(plugin_skills)}")
        for ns, cnt in sorted(ns_counts.items()):
            info(f"  {ns}: {cnt}")

    # Skills and context per project
    for proj in projects:
        local_skills_dir = proj / "skills"
        local_skills = read_skills_in_dir(local_skills_dir)
        all_skills.extend(local_skills)

        claude_md = read_claude_md(proj)
        project_contexts.append({
            "name": proj.name,
            "path": str(proj),
            "claude_md": claude_md[:1500],
            "local_skills": [s["name"] for s in local_skills],
        })
        if local_skills:
            info(f"  {proj.name}: {len(local_skills)} local skill(s)")

    sep()
    info(f"Total skills: {len(all_skills)}")

    return {
        "projects": project_contexts,
        "skills": all_skills,
        "global_claude_md": safe_read(GLOBAL_CLAUDE_MD)[:1500],
    }

# ── Similarity helpers ────────────────────────────────────────────────────────
def _similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 similarity between two strings (case-insensitive)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _normalize_name(name: str) -> str:
    """Strip namespace prefix and separators for name comparison."""
    name = name.split(":")[-1]          # remove namespace (vercel:foo → foo)
    return re.sub(r"[-_]", " ", name)   # hyphens/underscores → spaces

# ── Audit: local analysis, zero API ──────────────────────────────────────────
DUPLICATE_DESC_THRESHOLD = 0.80   # description similarity alone → duplicate
DUPLICATE_NAME_THRESHOLD = 0.90   # name similarity alone → duplicate
COMBINED_THRESHOLD       = 0.70   # desc + name both above this → duplicate
MIN_DESC_LENGTH          = 25     # min chars for a description to count
EMPTY_SIZE_THRESHOLD     = 100    # bytes → flagged as empty

def _is_duplicate(a: dict, b: dict) -> tuple[bool, str]:
    """Return (is_dup, reason) for two skills."""
    a_desc = a["description"] if len(a["description"]) >= MIN_DESC_LENGTH else ""
    b_desc = b["description"] if len(b["description"]) >= MIN_DESC_LENGTH else ""
    desc_sim = _similarity(a_desc, b_desc)
    name_sim = _similarity(_normalize_name(a["name"]), _normalize_name(b["name"]))
    is_dup = (
        desc_sim >= DUPLICATE_DESC_THRESHOLD
        or name_sim >= DUPLICATE_NAME_THRESHOLD
        or (desc_sim >= COMBINED_THRESHOLD and name_sim >= COMBINED_THRESHOLD)
    )
    reason = (
        f"Descriptions {int(desc_sim * 100)}% similar"
        if desc_sim >= DUPLICATE_DESC_THRESHOLD
        else f"Names {int(name_sim * 100)}% similar"
    )
    return is_dup, reason


def cmd_audit(snapshot: dict) -> None:
    h1("▸ Auditing skills  (local analysis — no API)")

    non_plugin = [s for s in snapshot["skills"] if s["scope"] != "plugin"]
    plugin_skills = [s for s in snapshot["skills"] if s["scope"] == "plugin"]
    dim(f"  Comparing {len(non_plugin)} global/local skills · cross-checking {len(plugin_skills)} plugin skills…")

    duplicates: list[dict] = []      # same-scope duplicates
    shadowed:   list[dict] = []      # global/local skill duplicated by a plugin
    warnings:   list[dict] = []
    seen_pairs: set[frozenset] = set()

    # ── Same-scope comparison (global vs global, local vs local) ─────────────
    for i, a in enumerate(non_plugin):
        if a["size"] < EMPTY_SIZE_THRESHOLD:
            warnings.append({
                "path": a["path"],
                "reason": f"Very small skill ({a['size']} bytes) — may be empty or a stub",
            })
            continue
        if not a["description"]:
            warnings.append({
                "path": a["path"],
                "reason": "No 'description' field in frontmatter",
            })

        for b in non_plugin[i + 1:]:
            pair = frozenset([a["path"], b["path"]])
            if pair in seen_pairs:
                continue
            is_dup, reason = _is_duplicate(a, b)
            if is_dup:
                seen_pairs.add(pair)
                keep, remove = (a, b) if a["size"] >= b["size"] else (b, a)
                duplicates.append({
                    "group": [a["path"], b["path"]],
                    "reason": reason,
                    "desc_a": a["description"] or "(none)",
                    "desc_b": b["description"] or "(none)",
                    "keep": keep["path"],
                    "remove": [remove["path"]],
                })

    # ── Cross-scope: global/local shadowed by a plugin ───────────────────────
    for a in non_plugin:
        if a["size"] < EMPTY_SIZE_THRESHOLD:
            continue
        for b in plugin_skills:
            pair = frozenset([a["path"], b["path"]])
            if pair in seen_pairs:
                continue
            is_dup, reason = _is_duplicate(a, b)
            if is_dup:
                seen_pairs.add(pair)
                shadowed.append({
                    "group": [a["path"], b["path"]],
                    "reason": reason,
                    "desc_local": a["description"] or "(none)",
                    "desc_plugin": b["description"] or "(none)",
                    "plugin_name": b["name"],
                    "keep": b["path"],
                    "remove": [a["path"]],
                })

    # ── Build action list ─────────────────────────────────────────────────────
    actions = (
        [{"action": "remove", "path": r, "reason": g["reason"], "type": "duplicate"}
         for g in duplicates for r in g["remove"]]
        + [{"action": "remove", "path": r, "reason": f"Shadowed by plugin '{g['plugin_name']}' — {g['reason']}", "type": "shadowed"}
           for g in shadowed for r in g["remove"]]
        + [{"action": "warn", "path": w["path"], "reason": w["reason"], "type": "warning"}
           for w in warnings]
    )

    total_issues = len(duplicates) + len(shadowed) + len(warnings)
    summary = (
        f"{len(duplicates)} duplicate(s), {len(shadowed)} shadowed by plugin, {len(warnings)} warning(s)."
        if total_issues else "All clean — no duplicates or issues detected."
    )

    # ── Save report ───────────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "method": "local (difflib — no API)",
        "summary": summary,
        "duplicates": duplicates,
        "shadowed": shadowed,
        "warnings": warnings,
        "actions": actions,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # ── Display results ───────────────────────────────────────────────────────
    sep()
    print(f"\n  {summary}\n")

    if not total_issues:
        ok(f"Report saved → {_short(str(REPORT_FILE))}")
        return

    if duplicates:
        h2(f"  Duplicate skills ({len(duplicates)} group(s))")
        for i, g in enumerate(duplicates, 1):
            print(f"\n  {BOLD}[{i}] Duplicate{R}  {DIM}{g['reason']}{R}")
            print(f"      {GREEN}Keep:{R}    {_short(g['keep'])}")
            if g["desc_a"]:
                print(f"      {DIM}desc: {g['desc_a'][:80]}{R}")
            for r in g["remove"]:
                print(f"      {RED}Remove:{R}  {_short(r)}")
            if g["desc_b"]:
                print(f"      {DIM}desc: {g['desc_b'][:80]}{R}")

    if shadowed:
        h2(f"  Shadowed by plugin ({len(shadowed)} group(s))")
        for i, g in enumerate(shadowed, 1):
            print(f"\n  {BOLD}[{i}] Shadowed{R}  {DIM}{g['reason']}{R}")
            print(f"      {MAGENTA}Plugin:{R}  {g['plugin_name']}")
            if g["desc_plugin"]:
                print(f"      {DIM}desc: {g['desc_plugin'][:80]}{R}")
            for r in g["remove"]:
                print(f"      {RED}Remove:{R}  {_short(r)}  {DIM}(your global copy){R}")
            if g["desc_local"]:
                print(f"      {DIM}desc: {g['desc_local'][:80]}{R}")

    if warnings:
        h2(f"  Warnings ({len(warnings)})")
        for w in warnings:
            warn(f"{_short(w['path'])}")
            dim(f"    {w['reason']}")

    sep()
    ok(f"Report saved → {_short(str(REPORT_FILE))}")
    info(f"Run  {CYAN}skill-inventory clean{R}  to apply changes")

# ── Interactive cleanup — reads report, zero API ───────────────────────────────
def cmd_clean(yes_all: bool = False) -> None:
    if not REPORT_FILE.exists():
        err(f"No audit report found at {_short(str(REPORT_FILE))}")
        info(f"Run  {CYAN}skill-inventory audit{R}  first to generate one.")
        sys.exit(1)

    try:
        report = json.loads(REPORT_FILE.read_text())
    except Exception as e:
        err(f"Could not read report: {e}")
        sys.exit(1)

    actions: list[dict] = report.get("actions", [])
    generated_at = report.get("generated_at", "unknown")

    removable = [a for a in actions if a["action"] == "remove"]
    warnings  = [a for a in actions if a["action"] == "warn"]

    h1("▸ Cleanup" + (" (--yes)" if yes_all else " (interactive)"))
    info(f"Report from: {generated_at}")

    if not actions:
        ok("Report shows no issues. Nothing to clean.")
        return

    if yes_all:
        # ── Non-interactive: show summary then delete all ─────────────────────
        print(f"\n  {len(removable)} skill(s) to delete · {len(warnings)} warning(s) ignored.\n")
        removed = []
        skipped = []
        backup_dir = HOME / ".claude" / "skills-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)

        for act in removable:
            path = Path(act["path"])
            label = "Duplicate" if act["type"] == "duplicate" else "Unused" if act["type"] == "unused" else "Shadowed"
            if not path.exists():
                dim(f"  {_short(str(path))}  (already gone)")
                skipped.append(str(path))
                continue
            backup_path = backup_dir / path.name
            shutil.copy2(path, backup_path)
            path.unlink()
            ok(f"[{label}] {_short(str(path))}")
            removed.append(str(path))

        sep()
        h2("  Cleanup summary")
        ok(f"Deleted:  {len(removed)}")
        if skipped:
            info(f"Already gone: {len(skipped)}")
        if warnings:
            warn(f"Warnings (not deleted): {len(warnings)}")
        if removed:
            dim(f"Backups at: ~/.claude/skills-backup/")
            REPORT_FILE.unlink(missing_ok=True)
            dim("Report cleared — run audit again to refresh.")
        return

    # ── Interactive mode ──────────────────────────────────────────────────────
    print(f"  {len(actions)} action(s) proposed. Let's review them one by one.\n")

    removed = []
    skipped = []
    backup_dir = HOME / ".claude" / "skills-backup"

    for i, act in enumerate(actions, 1):
        path = Path(act["path"])
        label = (
            "Duplicate" if act["type"] == "duplicate"
            else "Shadowed by plugin" if act["type"] == "shadowed"
            else "Unused" if act["type"] == "unused"
            else "Warning"
        )
        print(f"  {BOLD}[{i}/{len(actions)}]{R}  {YELLOW}{label}{R}")
        print(f"  Skill:   {_short(str(path))}")
        print(f"  Reason:  {act['reason']}")

        if not path.exists():
            dim("  (file no longer exists, skipping)")
            skipped.append(str(path))
            print()
            continue

        preview = safe_read(path)[:300].strip()
        if preview:
            print(f"\n  {DIM}Preview:{R}")
            for line in preview.split("\n")[:6]:
                print(f"  {DIM}{line}{R}")

        print()
        answer = input(f"  Delete? [y/N/full view]: ").strip().lower()

        if answer == "full view":
            print(f"\n{DIM}{safe_read(path)}{R}\n")
            answer = input(f"  Delete now? [y/N]: ").strip().lower()

        if answer == "y":
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / path.name
            shutil.copy2(path, backup_path)
            path.unlink()
            ok(f"Deleted. Backup at ~/.claude/skills-backup/{path.name}")
            removed.append(str(path))
        else:
            dim("  Skipped.")
            skipped.append(str(path))
        print()

    sep()
    h2("  Cleanup summary")
    ok(f"Deleted:  {len(removed)}")
    if skipped:
        info(f"Skipped:  {len(skipped)}")
    if removed:
        dim(f"Backups at: ~/.claude/skills-backup/")
        REPORT_FILE.unlink(missing_ok=True)
        dim("Report cleared — run audit again to refresh.")

# ── GitHub match — repo tech stack vs skills ──────────────────────────────────

def _parse_github_url(url: str) -> tuple[str, str]:
    url = url.strip().rstrip("/").removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot parse GitHub URL: {url}")
    return parts[0], parts[1]


def _ssl_ctx() -> ssl.SSLContext:
    """Return an SSL context that works on macOS even without certificate installation."""
    try:
        ctx = ssl.create_default_context()
        # Quick probe to confirm it works
        urllib.request.urlopen(
            urllib.request.Request("https://api.github.com", headers={"User-Agent": "probe"}),
            context=ctx, timeout=5,
        )
        return ctx
    except Exception:
        # Fall back to unverified context (macOS Python fresh install issue)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

_SSL_CTX: Optional[ssl.SSLContext] = None

def _get_ssl() -> ssl.SSLContext:
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_ctx()
    return _SSL_CTX


def _gh_api(path: str) -> Optional[dict]:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "skill-inventory/1.0"},
    )
    try:
        with urllib.request.urlopen(req, context=_get_ssl(), timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _raw_file(owner: str, repo: str, path: str) -> Optional[str]:
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "skill-inventory/1.0"})
        try:
            with urllib.request.urlopen(req, context=_get_ssl(), timeout=8) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


TECH_ALIASES: dict[str, list[str]] = {
    "next":       ["next", "nextjs", "vercel", "react"],
    "nextjs":     ["nextjs", "next", "vercel", "react"],
    "react":      ["react", "frontend", "ui"],
    "vue":        ["vue", "frontend"],
    "angular":    ["angular", "frontend"],
    "svelte":     ["svelte", "frontend"],
    "typescript": ["typescript", "ts"],
    "javascript": ["javascript", "js"],
    "python":     ["python"],
    "go":         ["go", "golang"],
    "rust":       ["rust"],
    "prisma":     ["prisma", "database", "orm"],
    "postgres":   ["postgres", "postgresql", "database"],
    "pg":         ["postgres", "postgresql", "database"],
    "mongodb":    ["mongodb", "database"],
    "redis":      ["redis", "cache"],
    "graphql":    ["graphql", "api"],
    "trpc":       ["trpc", "api"],
    "tailwind":   ["tailwind", "css", "ui"],
    "playwright": ["playwright", "testing", "browser"],
    "vitest":     ["vitest", "testing"],
    "jest":       ["jest", "testing"],
    "fastapi":    ["fastapi", "python", "api"],
    "django":     ["django", "python"],
    "flask":      ["flask", "python"],
    "express":    ["express", "nodejs"],
    "node":       ["nodejs", "javascript"],
    "vercel":     ["vercel", "deployment"],
    "seo":        ["seo"],
    "linkedin":   ["linkedin"],
}

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "use", "can",
    "are", "you", "all", "any", "not", "new", "has", "add", "get",
}


def _extract_keywords(owner: str, repo: str, repo_data: dict) -> set[str]:
    keywords: set[str] = set()

    lang = (repo_data.get("language") or "").lower()
    if lang:
        keywords.add(lang)
    for topic in repo_data.get("topics", []):
        keywords.add(topic.lower())
    for word in re.split(r'\W+', (repo_data.get("description") or "").lower()):
        if len(word) > 2 and word not in STOP_WORDS:
            keywords.add(word)

    # package.json
    raw = _raw_file(owner, repo, "package.json")
    if raw:
        try:
            pkg = json.loads(raw)
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for dep in pkg.get(section, {}):
                    name = dep.lstrip("@").split("/")[-1].lower()
                    keywords.add(name)
                    keywords.add(dep.lower())
        except Exception:
            pass

    # requirements.txt
    raw = _raw_file(owner, repo, "requirements.txt")
    if raw:
        for line in raw.splitlines():
            pkg_name = re.split(r'[>=<!;\s]', line)[0].strip().lower()
            if pkg_name and not pkg_name.startswith("#"):
                keywords.add(pkg_name)

    # go.mod
    raw = _raw_file(owner, repo, "go.mod")
    if raw:
        keywords.add("go")
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("//") and not line.startswith("go ") and not line.startswith("module"):
                parts = line.split()
                if parts:
                    keywords.add(parts[0].split("/")[-1].lower())

    # pyproject.toml
    raw = _raw_file(owner, repo, "pyproject.toml")
    if raw:
        keywords.add("python")
        for line in raw.splitlines():
            m = re.match(r'^\s*"?([a-zA-Z0-9_-]+)"?\s*[>=<]', line)
            if m:
                keywords.add(m.group(1).lower())

    # Cargo.toml
    if _raw_file(owner, repo, "Cargo.toml"):
        keywords.add("rust")

    return keywords


def _score_skill(skill: dict, keywords: set[str]) -> int:
    text = re.sub(r'[-_:/]', ' ', " ".join([
        skill["name"].lower(),
        skill["description"].lower(),
        skill.get("content", "")[:600].lower(),
    ]))
    expanded: set[str] = set()
    for kw in keywords:
        expanded.add(kw)
        for alias in TECH_ALIASES.get(kw, []):
            expanded.add(alias)
    return sum(1 for kw in expanded if len(kw) > 2 and kw in text)


def _extract_keywords_local(project_path: Path) -> set[str]:
    """Extract tech keywords from a local project directory."""
    keywords: set[str] = set()

    def _read(rel: str) -> Optional[str]:
        p = project_path / rel
        return safe_read(p) if p.exists() else None

    # Detect language from file extensions
    exts = {f.suffix.lower() for f in project_path.rglob("*") if f.is_file()}
    lang_map = {".py": "python", ".go": "go", ".rs": "rust", ".ts": "typescript",
                ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".rb": "ruby"}
    for ext, lang in lang_map.items():
        if ext in exts:
            keywords.add(lang)

    # package.json
    raw = _read("package.json")
    if raw:
        try:
            pkg = json.loads(raw)
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for dep in pkg.get(section, {}):
                    keywords.add(dep.lstrip("@").split("/")[-1].lower())
                    keywords.add(dep.lower())
        except Exception:
            pass

    # requirements.txt
    raw = _read("requirements.txt")
    if raw:
        for line in raw.splitlines():
            name = re.split(r'[>=<!;\s]', line)[0].strip().lower()
            if name and not name.startswith("#"):
                keywords.add(name)

    # go.mod
    raw = _read("go.mod")
    if raw:
        keywords.add("go")
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("//") and not line.startswith("go ") and not line.startswith("module"):
                parts = line.split()
                if parts:
                    keywords.add(parts[0].split("/")[-1].lower())

    # pyproject.toml
    raw = _read("pyproject.toml")
    if raw:
        keywords.add("python")
        for line in raw.splitlines():
            m = re.match(r'^\s*"?([a-zA-Z0-9_-]+)"?\s*[>=<]', line)
            if m:
                keywords.add(m.group(1).lower())

    if (project_path / "Cargo.toml").exists():
        keywords.add("rust")

    return keywords


def _local_project_keywords(snapshot: dict) -> dict[str, set[str]]:
    """Return {project_name: keywords} for all detected local projects."""
    result = {}
    for p in snapshot["projects"]:
        path = Path(p["path"])
        kw = _extract_keywords_local(path)
        if kw:
            result[p["name"]] = kw
    return result


def cmd_prune(snapshot: dict) -> None:
    h1("▸ Prune — finding skills unused across all your projects")

    own_skills = [s for s in snapshot["skills"] if s["scope"] in ("global", "local")]
    projects = snapshot["projects"]

    if not projects:
        warn("No projects detected. Nothing to prune against.")
        return

    info(f"Scanning {len(projects)} project(s) for tech stack…")
    project_kw = _local_project_keywords(snapshot)

    if not project_kw:
        warn("Could not extract keywords from any project. Check that your projects have package.json / requirements.txt / go.mod.")
        return

    # Combined keywords across all projects
    all_kw: set[str] = set()
    for kw in project_kw.values():
        all_kw.update(kw)

    # Detected stack summary
    display_kw = sorted(kw for kw in all_kw if len(kw) > 2 and kw not in STOP_WORDS)
    if display_kw:
        dim(f"  Combined stack: {', '.join(display_kw[:25])}" + (" …" if len(display_kw) > 25 else ""))

    # Score each own skill against combined keywords
    scores: list[tuple[int, dict]] = []
    for sk in own_skills:
        score = _score_skill(sk, all_kw)
        scores.append((score, sk))
    scores.sort(key=lambda x: x[0])

    orphaned  = [(s, sk) for s, sk in scores if s == 0]
    low_score = [(s, sk) for s, sk in scores if 0 < s <= 1]
    used      = [(s, sk) for s, sk in scores if s > 1]

    sep()
    print(f"  {BOLD}Legend{R}")
    print(f"    {RED}[✗ UNUSED]{R}    Score 0 across all your projects — safe to remove")
    print(f"    {YELLOW}[? LOW]{R}       Score 1 — marginally relevant, review before removing")
    print(f"    {GREEN}[✓ USED]{R}      Score > 1 — actively useful, keep")
    sep()

    if orphaned:
        h2(f"  {RED}[✗ UNUSED]{R}  {len(orphaned)} skill(s) — zero relevance to your projects")
        for _, sk in orphaned[:30]:
            scope = f"{DIM}({sk['scope']}){R}"
            print(f"    {RED}✗{R}  {BOLD}{sk['name']}{R}  {scope}")
            if sk["description"]:
                print(f"       {DIM}{sk['description'][:85]}{R}")
        if len(orphaned) > 30:
            dim(f"    … and {len(orphaned) - 30} more")
    else:
        ok("All your skills are relevant to at least one project.")

    if low_score:
        h2(f"  {YELLOW}[? LOW]{R}  {len(low_score)} skill(s) — low relevance (score = 1)")
        for s, sk in low_score[:15]:
            print(f"    {YELLOW}?{R}  {BOLD}{sk['name']}{R}  {DIM}(score: {s}){R}")
        if len(low_score) > 15:
            dim(f"    … and {len(low_score) - 15} more")

    if used:
        h2(f"  {GREEN}[✓ USED]{R}  {len(used)} skill(s) — actively relevant")
        for s, sk in used[-10:][::-1]:
            print(f"    {GREEN}✓{R}  {BOLD}{sk['name']}{R}  {DIM}(score: {s}){R}")
        if len(used) > 10:
            dim(f"    … and {len(used) - 10} more (all relevant)")

    # ── Save to report for clean ──────────────────────────────────────────────
    actions = [
        {"action": "remove", "path": sk["path"], "reason": f"Score 0 across {len(projects)} project(s)", "type": "unused"}
        for _, sk in orphaned
    ]
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "method": "local prune (zero-relevance across projects)",
        "summary": f"{len(orphaned)} unused, {len(low_score)} low-score, {len(used)} active.",
        "duplicates": [],
        "shadowed": [],
        "warnings": [],
        "actions": actions,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    sep()
    if orphaned:
        ok(f"Report saved → {_short(str(REPORT_FILE))}")
        info(f"Run  {CYAN}skill-inventory clean{R}  to remove unused skills (with backup)")
    else:
        ok("Nothing to prune — all skills are relevant.")


def cmd_match(github_url: str, snapshot: dict) -> None:
    h1(f"▸ Gap analysis  {github_url}")

    try:
        owner, repo = _parse_github_url(github_url)
    except ValueError as e:
        err(str(e))
        sys.exit(1)

    info(f"Fetching {owner}/{repo} from GitHub API…")
    repo_data = _gh_api(f"repos/{owner}/{repo}")
    if not repo_data or "id" not in repo_data:
        err("Could not fetch repo — check the URL and that the repo is public.")
        sys.exit(1)

    lang   = repo_data.get("language") or "unknown"
    desc   = repo_data.get("description") or ""
    topics = repo_data.get("topics", [])
    stars  = repo_data.get("stargazers_count", 0)

    info(f"Language: {lang}  ·  Stars: {stars}")
    if topics:
        info(f"Topics:   {', '.join(topics)}")
    if desc:
        dim(f"  {desc}")

    info("Extracting tech stack from dependency files…")
    keywords = _extract_keywords(owner, repo, repo_data)

    # Meaningful tech keywords (keys in TECH_ALIASES that appear in keywords)
    tech_keys = sorted(k for k in TECH_ALIASES if k in keywords)
    display_kw = sorted(kw for kw in keywords if len(kw) > 2 and kw not in STOP_WORDS)
    if display_kw:
        dim(f"  Stack: {', '.join(display_kw[:20])}" + (" …" if len(display_kw) > 20 else ""))

    # ── Cross-reference local projects ───────────────────────────────────────
    info("Scanning your local projects for tech stack…")
    local_project_kw = _local_project_keywords(snapshot)
    all_local_kw: set[str] = set()
    for kw in local_project_kw.values():
        all_local_kw.update(kw)

    # ── Score ALL skills ──────────────────────────────────────────────────────
    all_skills = snapshot["skills"]
    own_skills    = [s for s in all_skills if s["scope"] in ("global", "local")]
    plugin_skills = [s for s in all_skills if s["scope"] == "plugin"]

    scored_own = sorted(
        ((s, sk) for sk in own_skills if (s := _score_skill(sk, keywords)) > 0),
        key=lambda x: x[0], reverse=True,
    )
    scored_plugins = sorted(
        ((s, sk) for sk in plugin_skills if (s := _score_skill(sk, keywords)) > 0),
        key=lambda x: x[0], reverse=True,
    )

    # Deduplicate plugins by plugin_name (keep highest score per name)
    seen_plugin_names: set[str] = set()
    deduped_plugins = []
    for score, sk in scored_plugins:
        name = sk.get("plugin_name", sk["name"])
        if name not in seen_plugin_names:
            seen_plugin_names.add(name)
            deduped_plugins.append((score, sk))

    # Plugin-only = plugins not already covered by an own skill (name OR description similarity)
    def _covered_by_own(plugin_sk: dict) -> bool:
        p_name = _normalize_name(plugin_sk.get("plugin_name", plugin_sk["name"]))
        p_desc = plugin_sk.get("description", "")
        # Compare against ALL own skills — not just the ones that scored for this repo
        for own_sk in own_skills:
            o_name = _normalize_name(own_sk["name"])
            if _similarity(p_name, o_name) >= 0.70:
                return True
            shorter, longer = (o_name, p_name) if len(o_name) <= len(p_name) else (p_name, o_name)
            if shorter and shorter in longer:
                return True
            if p_desc and own_sk.get("description") and _similarity(p_desc, own_sk["description"]) >= 0.75:
                return True
        return False

    # Skills present in 2+ plugin namespaces are already "multi-covered" — no need to install globally
    plugin_ns_by_name: dict[str, set] = {}
    for sk in plugin_skills:
        pname = sk.get("plugin_name", sk["name"])
        plugin_ns_by_name.setdefault(pname, set()).add(sk.get("namespace", "?"))
    multi_covered = {name for name, nss in plugin_ns_by_name.items() if len(nss) > 1}

    plugins_only = [
        (score, sk) for score, sk in deduped_plugins
        if not _covered_by_own(sk) and sk.get("plugin_name", sk["name"]) not in multi_covered
    ]

    # ── Gap detection ─────────────────────────────────────────────────────────
    # For each known tech key in this repo, check if ANY skill covers it
    gaps = []
    for tech in tech_keys:
        aliases = set(TECH_ALIASES.get(tech, [tech]))
        covered = any(
            any(a in re.sub(r'[-_:/]', ' ', sk["name"].lower() + " " + sk["description"].lower())
                for a in aliases)
            for sk in all_skills
        )
        if not covered:
            gaps.append(tech)

    # ── Display ───────────────────────────────────────────────────────────────
    sep()

    # Legend
    print(f"  {BOLD}Legend{R}")
    print(f"    {GREEN}[✓ HAVE]{R}     Skill already installed in your global/local collection")
    print(f"    {YELLOW}[↓ INSTALL]{R}  Skill exists in a plugin but NOT yet in your global skills")
    print(f"    {RED}[✗ GAP]{R}      Tech area with no skill at all — not even in plugins")
    sep()

    max_score = max(
        (scored_own[0][0] if scored_own else 0),
        (plugins_only[0][0] if plugins_only else 0),
        1,
    )

    def _bar(score: int) -> str:
        n = max(1, round(score / max_score * 8))
        return "█" * n + "░" * (8 - n)

    # ── Section 1: Skills you already have ───────────────────────────────────
    if scored_own:
        h2(f"  {GREEN}[✓ HAVE]{R}  {len(scored_own)} skill(s) already installed")
        for score, sk in scored_own[:15]:
            print(f"    {GREEN}✓{R}  {BOLD}{sk['name']}{R}  {DIM}{_bar(score)}{R}")
            if sk["description"]:
                print(f"       {DIM}{sk['description'][:85]}{R}")
        if len(scored_own) > 15:
            dim(f"    … and {len(scored_own) - 15} more")
    else:
        warn("No matching skills in your global/local collection.")

    # ── Section 2: Available in plugins, not installed globally ───────────────
    if plugins_only:
        h2(f"  {YELLOW}[↓ INSTALL]{R}  {len(plugins_only)} skill(s) in plugins — not yet in your globals")
        dim(f"    These live in ~/.claude/plugins/cache/ but not in ~/.claude/skills/")
        dim(f"    Copy or symlink them to install globally.\n")
        for score, sk in plugins_only[:15]:
            ns = sk.get("namespace", "?")
            name = sk.get("plugin_name", sk["name"])
            local_score = _score_skill(sk, all_local_kw) if all_local_kw else 0
            priority = f"  {GREEN}★ also useful for YOUR projects{R}" if local_score > 0 else ""
            print(f"    {YELLOW}↓{R}  {BOLD}{name}{R}  {DIM}(from plugin: {ns})  {_bar(score)}{R}{priority}")
            if sk["description"]:
                print(f"       {DIM}{sk['description'][:85]}{R}")
        if len(plugins_only) > 15:
            dim(f"    … and {len(plugins_only) - 15} more")
    else:
        ok("All relevant plugin skills are already covered in your globals.")

    # ── Section 3: Tech areas with no skill at all ────────────────────────────
    if gaps:
        h2(f"  {RED}[✗ GAP]{R}  {len(gaps)} tech area(s) with zero skill coverage")
        dim(f"    No skill (yours or plugins) covers these technologies:")
        print(f"    {RED}{', '.join(gaps)}{R}")
    else:
        print(f"\n  {GREEN}✓{R}  {DIM}All detected tech areas have at least one skill — no gaps.{R}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    sep()
    total_relevant = len(scored_own) + len(plugins_only)
    if total_relevant == 0:
        warn(f"No relevant skills found for  {owner}/{repo}")
    elif not plugins_only and not gaps:
        ok(f"Fully covered for  {owner}/{repo}  — nothing to install.")
    else:
        have  = len(scored_own)
        avail = len(plugins_only)
        ngaps = len(gaps)
        print(f"\n  {BOLD}Verdict{R}")
        print(f"    {GREEN}✓ {have} skill(s) already installed{R}")
        if avail:
            print(f"    {YELLOW}↓ {avail} skill(s) to copy from plugins → ~/.claude/skills/{R}")
        if ngaps:
            print(f"    {RED}✗ {ngaps} tech area(s) with no skill anywhere{R}")
        print()


def _short(path: str) -> str:
    """Show the path relative to home."""
    try:
        return "~/" + str(Path(path).relative_to(HOME))
    except ValueError:
        return path

# ── Category detection ────────────────────────────────────────────────────────
# Maps prefix → category label. Order matters: first match wins.
CATEGORY_MAP = [
    # SEO
    ("seo",              "SEO & Content"),
    ("resources:seo",    "SEO & Content"),
    ("resources:comm",   "Communication"),
    ("resources:market", "Marketing"),
    ("resources:",       "Resources"),
    # Social / outreach
    ("linkedin",         "LinkedIn"),
    ("marketing",        "Marketing"),
    # Dev workflow
    ("sdd",              "Spec-Driven Dev (SDD)"),
    ("strict-tdd",       "Testing"),
    ("tdd",              "Testing"),
    ("playwright",       "Testing"),
    ("go-",              "Go"),
    ("branch",           "Git / Branches"),
    ("commit",           "Git / Branches"),
    ("pr",               "Pull Requests"),
    ("issue",            "Pull Requests"),
    # AI / plugins
    ("vercel",           "Vercel"),
    ("superpowers",      "Superpowers"),
    ("agent-skills",     "Agent Skills"),
    ("claude-mem",       "Claude Mem"),
    ("engram",           "Engram / Memory"),
    # Quality / security
    ("security",         "Security"),
    ("eval",             "Eval / Quality"),
    ("code-review",      "Eval / Quality"),
    ("audit",            "Eval / Quality"),
    ("judgment",         "Eval / Quality"),
    ("best-practices",   "Best Practices"),
    ("performance",      "Performance"),
    ("core-web",         "Performance"),
    ("accessibility",    "Accessibility"),
    # UI / design
    ("ui",               "UI / Design"),
    ("design",           "UI / Design"),
    ("interface",        "UI / Design"),
    ("landing",          "Landing Pages"),
    ("web",              "Web Quality"),
    # Infrastructure
    ("postgres",         "Databases"),
    ("tech",             "Tech Audit"),
    # Skills meta
    ("skill",            "Skill Management"),
    ("find-",            "Skill Management"),
    ("init",             "Skill Management"),
    ("tally",            "Forms"),
]

def categorize(name: str) -> str:
    lower = name.lower()
    for prefix, label in CATEGORY_MAP:
        if lower.startswith(prefix):
            return label
    return "General"

# ── list ──────────────────────────────────────────────────────────────────────
def cmd_list(snapshot: dict):
    h1("▸ Skills inventory")

    from collections import defaultdict

    global_skills = [s for s in snapshot["skills"] if s["scope"] == "global"]
    local_skills  = [s for s in snapshot["skills"] if s["scope"] == "local"]
    plugin_skills = [s for s in snapshot["skills"] if s["scope"] == "plugin"]

    # ── Global skills by category ─────────────────────────────────────────────
    if global_skills:
        h2("  Global (~/.claude/skills/)  — by category")
        by_cat: dict[str, list] = defaultdict(list)
        for s in global_skills:
            by_cat[categorize(s["name"])].append(s)
        for cat, skills in sorted(by_cat.items()):
            print(f"\n    {BOLD}{cat}{R}  {DIM}({len(skills)}){R}")
            for s in skills:
                print(f"      {CYAN}{s['name']}{R}  {DIM}({s['size']} chars){R}")

    # ── Local skills by project ───────────────────────────────────────────────
    if local_skills:
        h2("  Local — by project")
        by_project: dict[str, list] = defaultdict(list)
        for s in local_skills:
            by_project[s.get("project") or "?"].append(s)
        for proj, skills in sorted(by_project.items()):
            print(f"\n    {BOLD}{proj}{R}  {DIM}({len(skills)}){R}")
            for s in skills:
                print(f"      {CYAN}{s['name']}{R}  {DIM}({s['size']} chars){R}")

    # ── Plugin skills by namespace ────────────────────────────────────────────
    if plugin_skills:
        h2("  Plugins (~/.claude/plugins/)  — by namespace")
        by_ns: dict[str, list] = defaultdict(list)
        for s in plugin_skills:
            by_ns[s["namespace"]].append(s)
        for ns, skills in sorted(by_ns.items()):
            print(f"\n    {BOLD}{MAGENTA}{ns}{R}  {DIM}({len(skills)} skills){R}")
            for s in skills:
                print(f"      {CYAN}{s['plugin_name']}{R}  {DIM}({s['size']} chars){R}")

    sep()
    total = len(snapshot["skills"])
    info(f"Total: {total} skill(s)  ·  {len(global_skills)} global  ·  {len(plugin_skills)} plugins  ·  {len(local_skills)} local")

# ── Entrypoint ────────────────────────────────────────────────────────────────
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    args  = sys.argv[2:]
    yes_all = "--yes" in args or "-y" in args

    if cmd == "help" or cmd not in ("scan", "list", "audit", "clean", "match", "prune"):
        report_status = f"{GREEN}report ready{R}" if REPORT_FILE.exists() else f"{DIM}no report yet{R}"
        print(f"""
{BOLD}skill-inventory{R} — Claude Code skills manager

{BOLD}Commands:{R}
  {CYAN}scan{R}                Scan projects and skills (no analysis)
  {CYAN}list{R}                List all skills found
  {CYAN}audit{R}               Local analysis → save report        {DIM}(no API needed){R}
  {CYAN}clean{R}               Interactive cleanup from report     {DIM}(no API needed){R}
  {CYAN}clean --yes{R}         Delete all flagged skills at once   {DIM}(backups kept){R}
  {CYAN}match <github-url>{R}   Gap analysis — skills you have, need, or are missing
  {CYAN}prune{R}               Find global skills unused by any local project

{BOLD}Workflow:{R}
  1. skill-inventory audit                              # detect duplicates
  2. skill-inventory clean --yes                        # delete all at once
  3. skill-inventory match github.com/owner/repo        # find relevant skills
  4. skill-inventory prune                              # remove skills unused by your projects

{BOLD}Report:{R} {_short(str(REPORT_FILE))}  [{report_status}]

{DIM}Scanned folders: {', '.join(str(r) for r in PROJECT_ROOTS)}
Global skills:   ~/.claude/skills/
Plugin skills:   ~/.claude/plugins/cache/{R}
""")
        return

    # clean does NOT need a snapshot — reads report directly
    if cmd == "clean":
        cmd_clean(yes_all=yes_all)
        return

    snapshot = build_snapshot()

    if cmd == "scan":
        h2("  Detected projects")
        for p in snapshot["projects"]:
            has_skills = "  " + CYAN + str(len(p["local_skills"])) + " skill(s)" + R if p["local_skills"] else ""
            print(f"    {BOLD}{p['name']}{R}{has_skills}")
            print(f"    {DIM}{p['path']}{R}\n")

    elif cmd == "list":
        cmd_list(snapshot)

    elif cmd == "audit":
        cmd_audit(snapshot)

    elif cmd == "match":
        if len(sys.argv) < 3:
            err("Usage: skill-inventory match <github-url>")
            sys.exit(1)
        cmd_match(sys.argv[2], snapshot)

    elif cmd == "prune":
        cmd_prune(snapshot)

if __name__ == "__main__":
    main()
