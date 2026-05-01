#!/usr/bin/env python3
"""
skill-inventory — Manage your Claude Code skills across projects.

Commands:
  skill-inventory scan       Scan projects and skills
  skill-inventory list       List all skills found
  skill-inventory audit      Analyze with Claude and save report (needs ANTHROPIC_API_KEY)
  skill-inventory clean      Interactive cleanup from last audit report (no API needed)
"""

import os
import sys
import json
import re
import subprocess
import shutil
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
def read_skills_in_dir(skills_dir: Path) -> list[dict]:
    """Read all skills (.md) in a given directory."""
    skills = []
    if not skills_dir.exists():
        return skills
    SKIP_FILES = {"README.md", "LICENSE.md", "CHANGELOG.md", "CONTRIBUTING.md"}
    for f in sorted(skills_dir.rglob("*.md")):
        if f.name in SKIP_FILES or f.stem.upper() in ("LICENSE", "CHANGELOG"):
            continue
        content = safe_read(f)
        # If the file is named SKILL.md, use the parent directory name instead
        name = f.parent.name if f.stem.upper() == "SKILL" else f.stem
        skills.append({
            "path": str(f),
            "name": name,
            "scope": "global" if str(f).startswith(str(GLOBAL_SKILLS_DIR)) else "local",
            "project": _project_of(f),
            "content": content[:2000],  # truncate for API
            "size": len(content),
        })
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
        })
    return skills

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

# ── Call Claude API ───────────────────────────────────────────────────────────
def call_claude(prompt: str, max_tokens: int = 1500) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        err("ANTHROPIC_API_KEY not set.")
        err("Run: export ANTHROPIC_API_KEY=\"sk-ant-...\"")
        sys.exit(1)

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })

    result = subprocess.run(
        ["curl", "-sf",
         "https://api.anthropic.com/v1/messages",
         "-H", "Content-Type: application/json",
         "-H", f"x-api-key: {api_key}",
         "-H", "anthropic-version: 2023-06-01",
         "-d", payload],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        err("Network error calling the API.")
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
        if "error" in data:
            err(f"API error: {data['error']['message']}")
            sys.exit(1)
        return data["content"][0]["text"]
    except Exception as e:
        err(f"Unexpected API response: {e}")
        sys.exit(1)

# ── Audit: analyze with Claude, save report ───────────────────────────────────
def cmd_audit(snapshot: dict) -> None:
    h1("▸ Auditing skills with Claude")

    skills_summary = [
        {
            "name": s["name"],
            "scope": s["scope"],
            "project": s.get("project"),
            "path": s["path"],
            "preview": s["content"][:400],
        }
        for s in snapshot["skills"]
    ]

    projects_summary = [
        {
            "name": p["name"],
            "local_skills": p["local_skills"],
            "claude_md_excerpt": p["claude_md"][:600],
        }
        for p in snapshot["projects"]
    ]

    prompt = f"""Analyze this Claude Code skills inventory and detect issues.

## Skills found
{json.dumps(skills_summary, indent=2, ensure_ascii=False)}

## Projects and their CLAUDE.md
{json.dumps(projects_summary, indent=2, ensure_ascii=False)}

## Global CLAUDE.md
{snapshot['global_claude_md']}

## Task
Respond ONLY in valid JSON with this exact structure (no markdown, no explanations):
{{
  "duplicates": [
    {{
      "group": ["path/skill-a.md", "path/skill-b.md"],
      "reason": "Both do X",
      "keep": "path/skill-a.md",
      "remove": ["path/skill-b.md"]
    }}
  ],
  "unused": [
    {{
      "path": "path/skill.md",
      "reason": "Not referenced in any CLAUDE.md or active project"
    }}
  ],
  "summary": "Brief sentence about overall state"
}}

If no duplicates or unused, return empty arrays. JSON only, nothing else."""

    dim("  Querying Claude for semantic analysis...")
    raw = call_claude(prompt, max_tokens=1200)

    # Strip possible markdown
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            err("Could not parse Claude's response.")
            print(f"{DIM}{raw[:500]}{R}")
            return

    # Build action list
    actions = [
        {"action": "remove", "path": r, "reason": g["reason"], "type": "duplicate"}
        for g in result.get("duplicates", []) for r in g.get("remove", [])
    ] + [
        {"action": "remove", "path": s["path"], "reason": s["reason"], "type": "unused"}
        for s in result.get("unused", [])
    ]

    # ── Save report ───────────────────────────────────────────────────────────
    import datetime
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "summary": result.get("summary", ""),
        "duplicates": result.get("duplicates", []),
        "unused": result.get("unused", []),
        "actions": actions,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # ── Display results ───────────────────────────────────────────────────────
    sep()
    if report["summary"]:
        print(f"\n  {report['summary']}\n")

    duplicates = result.get("duplicates", [])
    unused = result.get("unused", [])

    if not duplicates and not unused:
        ok("No issues found. All clean.")
        ok(f"Report saved → {_short(str(REPORT_FILE))}")
        return

    if duplicates:
        h2(f"  Duplicate skills ({len(duplicates)} group(s))")
        for i, g in enumerate(duplicates, 1):
            print(f"\n  {BOLD}[{i}] Duplicate{R}")
            print(f"      Reason: {g['reason']}")
            print(f"      {GREEN}Keep:{R}    {_short(g['keep'])}")
            for r in g.get("remove", []):
                print(f"      {RED}Remove:{R}  {_short(r)}")

    if unused:
        h2(f"  Unused skills ({len(unused)})")
        for s in unused:
            warn(f"{_short(s['path'])}  —  {s['reason']}")

    sep()
    ok(f"Report saved → {_short(str(REPORT_FILE))}")
    info(f"Run  {CYAN}skill-inventory clean{R}  to apply changes (no API needed)")

# ── Interactive cleanup — reads report, zero API ───────────────────────────────
def cmd_clean() -> None:
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

    h1("▸ Interactive cleanup")
    info(f"Report from: {generated_at}")

    if not actions:
        ok("Report shows no issues. Nothing to clean.")
        return

    print(f"  {len(actions)} action(s) proposed. Let's review them one by one.\n")

    removed = []
    skipped = []
    backup_dir = HOME / ".claude" / "skills-backup"

    for i, act in enumerate(actions, 1):
        path = Path(act["path"])
        label = "Duplicate" if act["type"] == "duplicate" else "Unused"
        print(f"  {BOLD}[{i}/{len(actions)}]{R}  {YELLOW}{label}{R}")
        print(f"  Skill:   {_short(str(path))}")
        print(f"  Reason:  {act['reason']}")

        if not path.exists():
            dim("  (file no longer exists, skipping)")
            skipped.append(str(path))
            print()
            continue

        # Show first lines of the skill
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

    # Remove report after cleanup so next clean forces a fresh audit
    if removed:
        REPORT_FILE.unlink(missing_ok=True)
        dim("Report cleared — run audit again to refresh.")

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

    if cmd == "help" or cmd not in ("scan", "list", "audit", "clean"):
        report_status = f"{GREEN}report ready{R}" if REPORT_FILE.exists() else f"{DIM}no report yet{R}"
        print(f"""
{BOLD}skill-inventory{R} — Claude Code skills manager

{BOLD}Commands:{R}
  {CYAN}scan{R}    Scan projects and skills (no analysis)
  {CYAN}list{R}    List all skills found
  {CYAN}audit{R}   Analyze with Claude → save report  {DIM}(needs ANTHROPIC_API_KEY){R}
  {CYAN}clean{R}   Interactive cleanup from report     {DIM}(no API needed){R}

{BOLD}Workflow:{R}
  1. skill-inventory audit    # run once, costs ~1 API call
  2. skill-inventory clean    # interactive, no API, repeatable

{BOLD}Report:{R} {_short(str(REPORT_FILE))}  [{report_status}]

{DIM}Scanned folders: {', '.join(str(r) for r in PROJECT_ROOTS)}
Global skills:   ~/.claude/skills/
Plugin skills:   ~/.claude/plugins/cache/{R}
""")
        return

    # clean does NOT need a snapshot — reads report directly
    if cmd == "clean":
        cmd_clean()
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

if __name__ == "__main__":
    main()
