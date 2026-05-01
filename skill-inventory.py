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

def cmd_audit(snapshot: dict) -> None:
    h1("▸ Auditing skills  (local analysis — no API)")

    skills = [s for s in snapshot["skills"] if s["scope"] != "plugin"]
    dim(f"  Comparing {len(skills)} skills by description and name…")

    duplicates: list[dict] = []
    warnings:   list[dict] = []
    seen_pairs: set[frozenset] = set()

    for i, a in enumerate(skills):
        # ── Flag empty / no-description skills ───────────────────────────────
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

        # ── Compare with every other skill ───────────────────────────────────
        for b in skills[i + 1:]:
            pair = frozenset([a["path"], b["path"]])
            if pair in seen_pairs:
                continue

            # Only use description if both are meaningful (not a stub or block scalar remnant)
            a_desc = a["description"] if len(a["description"]) >= MIN_DESC_LENGTH else ""
            b_desc = b["description"] if len(b["description"]) >= MIN_DESC_LENGTH else ""
            desc_sim = _similarity(a_desc, b_desc)
            name_sim = _similarity(
                _normalize_name(a["name"]),
                _normalize_name(b["name"])
            )

            is_dup = (
                desc_sim >= DUPLICATE_DESC_THRESHOLD
                or name_sim >= DUPLICATE_NAME_THRESHOLD
                or (desc_sim >= COMBINED_THRESHOLD and name_sim >= COMBINED_THRESHOLD)
            )
            if is_dup:
                seen_pairs.add(pair)
                # Keep the one with more content (likely more complete)
                keep, remove = (a, b) if a["size"] >= b["size"] else (b, a)
                reason = (
                    f"Descriptions {int(desc_sim * 100)}% similar"
                    if desc_sim >= DUPLICATE_DESC_THRESHOLD
                    else f"Names {int(name_sim * 100)}% similar"
                )
                duplicates.append({
                    "group": [a["path"], b["path"]],
                    "reason": reason,
                    "desc_a": a["description"] or "(none)",
                    "desc_b": b["description"] or "(none)",
                    "keep": keep["path"],
                    "remove": [remove["path"]],
                })

    # ── Build action list ─────────────────────────────────────────────────────
    actions = [
        {"action": "remove", "path": r, "reason": g["reason"], "type": "duplicate"}
        for g in duplicates for r in g["remove"]
    ] + [
        {"action": "warn", "path": w["path"], "reason": w["reason"], "type": "warning"}
        for w in warnings
    ]

    total_issues = len(duplicates) + len(warnings)
    summary = (
        f"{len(duplicates)} duplicate group(s) and {len(warnings)} warning(s) found."
        if total_issues else "All clean — no duplicates or issues detected."
    )

    # ── Save report ───────────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "method": "local (difflib — no API)",
        "summary": summary,
        "duplicates": duplicates,
        "warnings": warnings,
        "actions": actions,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # ── Display results ───────────────────────────────────────────────────────
    sep()
    print(f"\n  {summary}\n")

    if not duplicates and not warnings:
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

    if warnings:
        h2(f"  Warnings ({len(warnings)})")
        for w in warnings:
            warn(f"{_short(w['path'])}")
            dim(f"    {w['reason']}")

    sep()
    ok(f"Report saved → {_short(str(REPORT_FILE))}")
    info(f"Run  {CYAN}skill-inventory clean{R}  to apply changes")

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
  {CYAN}audit{R}   Local analysis → save report        {DIM}(no API needed){R}
  {CYAN}clean{R}   Interactive cleanup from report     {DIM}(no API needed){R}

{BOLD}Workflow:{R}
  1. skill-inventory audit    # runs locally, zero API cost
  2. skill-inventory clean    # interactive, repeatable

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
