# 🗂️ skill-inventory

> Manage, audit, and clean your [Claude Code](https://claude.ai/code) skills — global, local, and plugins — from a single CLI.

---

## ✨ Features

- 📦 **Inventories all skill sources** — `~/.claude/skills/`, project-local skills, and installed plugins
- 🏷️ **Categorizes automatically** — SEO, LinkedIn, Testing, Vercel, SDD, Security, and more
- 🔍 **Local duplicate detection** — compares skill descriptions with `difflib`, zero API needed
- 🧹 **Zero-API cleanup** — run `audit` once, then `clean` as many times as you want
- 💾 **Safe deletions** — every removed skill is backed up before deletion

---

## 📋 Requirements

| Dependency | Version |
|------------|---------|
| Python | 3.9+ |

No API key needed. Everything runs locally.

---

## 🚀 Installation

```bash
git clone https://github.com/Victor-R-R/skill-inventory.git
cd skill-inventory
bash install.sh
```

The installer copies `skill-inventory` to `~/.local/bin/` and checks your PATH.

---

## 🛠️ Commands

| Command | Description | Needs API? |
|---------|-------------|------------|
| `scan`  | Discover projects and count skills | ❌ |
| `list`  | Full categorized inventory | ❌ |
| `audit` | Local analysis → saves report | ❌ |
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
  ·  Global skills (~/.claude/skills/): 62
  ·  Plugin skills (~/.claude/plugins/cache/): 122
  ·  Total skills: 205
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

### 🔍 `audit` — Local duplicate detection

```bash
skill-inventory audit
```

Reads every skill's YAML frontmatter description and compares all pairs using `difflib.SequenceMatcher`. No API call, no tokens, instant results.

Detects:
- 🔁 **Duplicates** — two skills with very similar descriptions or names
- ⚠️ **Stubs** — skills under 100 bytes with no real content

Results are saved to `~/.claude/skill-inventory-report.json`.

```
▸ Auditing skills  (local analysis — no API)
  Comparing 83 skills by description and name…

  2 duplicate group(s) and 0 warning(s) found.

  [1] Duplicate  Descriptions 89% similar
      Keep:    ~/.claude/skills/prfeature/SKILL.md
      desc: Create a feature branch, commit staged changes, push, open PR...
      Remove:  ~/.claude/skills/prfix/SKILL.md
      desc: Create a fix branch, commit staged changes, push, open PR...

  ✓  Report saved → ~/.claude/skill-inventory-report.json
  ·  Run skill-inventory clean to apply changes
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
  Skill:   ~/.claude/skills/prfix/SKILL.md
  Reason:  Descriptions 89% similar

  Preview:
  ---
  name: prfix
  description: Create a fix branch, commit staged changes, push, open PR...
  ---

  Delete? [y/N/full view]: y
  ✓  Deleted. Backup at ~/.claude/skills-backup/SKILL.md
```

Every deleted file is backed up to `~/.claude/skills-backup/` before removal.

---

## 🔄 Recommended workflow

```bash
# Step 1 — analyze (zero API, instant)
skill-inventory audit

# Step 2 — clean up (repeat as needed)
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
       ├── ~/.claude/skills/          → global skills (SKILL.md or flat .md)
       ├── ~/.claude/plugins/cache/   → plugin skills (latest version only)
       └── ~/*/skills/                → local project skills
       │
       ├── scan   → print summary
       ├── list   → categorized display (by category / project / namespace)
       ├── audit  → difflib comparison → ~/.claude/skill-inventory-report.json
       └── clean  → read report → interactive delete + backup (zero API)
```

### How duplicate detection works

1. Extract `description:` from each skill's YAML frontmatter (handles inline values and `>` / `|` block scalars)
2. Normalize skill names (strip namespace prefix, replace `-`/`_` with spaces)
3. Compare every pair with `difflib.SequenceMatcher`
4. Flag as duplicate if description similarity ≥ 80%, name similarity ≥ 90%, or both ≥ 70%

---

## 📄 License

MIT
