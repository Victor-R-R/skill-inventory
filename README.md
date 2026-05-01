# 🗂️ skill-inventory

> Manage, audit, and clean your [Claude Code](https://claude.ai/code) skills — global, local, and plugins — from a single CLI.

---

## ✨ Features

- 📦 **Inventories all skill sources** — `~/.claude/skills/`, project-local skills, and installed plugins
- 🏷️ **Categorizes automatically** — SEO, LinkedIn, Testing, Vercel, SDD, Security, and more
- 🔍 **Semantic audit via Claude** — detects duplicates and unused skills across your entire setup
- 🧹 **Zero-API cleanup** — run `audit` once, then `clean` as many times as you want without touching the API
- 💾 **Safe deletions** — every removed skill is backed up before deletion

---

## 📋 Requirements

| Dependency | Version |
|------------|---------|
| Python | 3.9+ |
| curl | any |
| ANTHROPIC_API_KEY | for `audit` only |

---

## 🚀 Installation

```bash
git clone https://github.com/Victor-R-R/skill-inventory.git
cd skill-inventory
bash install.sh
```

The installer copies `skill-inventory` to `~/.local/bin/` and checks your PATH.

> **Note:** `ANTHROPIC_API_KEY` is only needed when running `audit`. All other commands work without it.

---

## 🛠️ Commands

| Command | Description | Needs API? |
|---------|-------------|------------|
| `scan`  | Discover projects and count skills | ❌ |
| `list`  | Full categorized inventory | ❌ |
| `audit` | Semantic analysis → saves report | ✅ |
| `clean` | Interactive cleanup from report | ❌ |

---

## 📖 Usage

### 🔎 `scan` — Quick overview

```bash
skill-inventory scan
```

```
▸ Scanning system
  ·  Projects found: 10
  ·  Global skills (~/.claude/skills/): 84
  ·  Plugin skills (~/.claude/plugins/cache/): 122
  ·  Total skills: 230
```

---

### 📋 `list` — Categorized inventory

```bash
skill-inventory list
```

```
▸ Skills inventory

  Global (~/.claude/skills/) — by category

    SEO & Content  (13)
      seo-audit · seo-content · seo-geo · ...

    Spec-Driven Dev (SDD)  (11)
      sdd-apply · sdd-archive · sdd-design · ...

    Testing  (10)
      strict-tdd · playwright-skill · go-testing · ...

    LinkedIn  (8)
      linkedin-article-agent · linkedin-post-agent · ...

  Plugins (~/.claude/plugins/) — by namespace

    vercel  (32)  ·  vercel-plugin  (46)
    superpowers  (14)  ·  agent-skills  (21)
    claude-mem  (8)  ·  engram  (1)
```

---

### 🧠 `audit` — AI-powered analysis

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
skill-inventory audit
```

Uses **Claude Haiku** to semantically compare all your skills and detect:
- 🔁 **Duplicates** — two skills doing the same thing with different names
- 💤 **Unused** — skills not referenced in any active project

Results are saved to `~/.claude/skill-inventory-report.json`.

```
▸ Auditing skills with Claude
  ·  Querying Claude for semantic analysis...

  Duplicate skills (1 group)

  [1] Duplicate
      Reason: Both handle SEO page audits
      Keep:    ~/.claude/skills/seo-audit/SKILL.md
      Remove:  ~/.claude/skills/audit-seo/SKILL.md

  ✓  Report saved → ~/.claude/skill-inventory-report.json
  ·  Run skill-inventory clean to apply changes (no API needed)
```

---

### 🧹 `clean` — Interactive cleanup

```bash
skill-inventory clean
```

Reads the saved report — **no API call**. Review and delete skills one by one:

```
▸ Interactive cleanup
  Report from: 2026-05-01T10:30:00
  2 action(s) proposed. Let's review them one by one.

  [1/2]  ⚠ Duplicate
  Skill:   ~/.claude/skills/audit-seo/SKILL.md
  Reason:  Both handle SEO page audits

  Preview:
  ---
  name: audit-seo
  description: Audits SEO issues on a page
  ---

  Delete? [y/N/full view]: y
  ✓  Deleted. Backup at ~/.claude/skills-backup/SKILL.md
```

Every deleted file is backed up to `~/.claude/skills-backup/` before removal.

---

## 🔄 Recommended workflow

```bash
# Step 1 — run once (uses API)
skill-inventory audit

# Step 2 — repeat as needed (zero API)
skill-inventory clean

# Step 3 — re-audit after cleanup
skill-inventory audit
```

---

## 📁 Where skills are scanned

| Source | Path |
|--------|------|
| 🌐 Global | `~/.claude/skills/` |
| 🔌 Plugins | `~/.claude/plugins/cache/` |
| 📁 Local | `<project>/skills/` |
| 🗂️ Projects | `~/` (folders with `.git`, `CLAUDE.md`, or `package.json`) |

---

## 🏗️ Architecture

```
skill-inventory <cmd>
       │
       ▼
 build_snapshot()
       ├── ~/.claude/skills/          → global skills
       ├── ~/.claude/plugins/cache/   → plugin skills (latest version only)
       └── ~/*/skills/                → local project skills
       │
       ├── scan   → print summary
       ├── list   → categorized display (by category / project / namespace)
       ├── audit  → Claude Haiku → ~/.claude/skill-inventory-report.json
       └── clean  → read report → interactive delete + backup (zero API)
```

---

## 📄 License

MIT
