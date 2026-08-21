# Progress Tracker Skill (`progress-tracker`)

A standardized, reusable Antigravity skill for **Autonomous Execution & Visual Progress Tracking** using `progress.md` and Mermaid diagrams.

---

## What It Does

- Enforces deterministic, transparent milestone management during agent workflows.
- Generates and dynamically updates a top-of-file Mermaid flowchart representing project state with standard color codes:
  - 🟢 **Done (`#2ecc71`)**: Completed & verified
  - 🟠 **Active (`#f39c12`)**: In progress
  - 🔴 **Blocked (`#e74c3c`)**: Blocked / waiting for input
  - ⚪ **Todo (`#bdc3c7`)**: Pending
- Synchronizes actionable markdown checklists (`- [x]`) and session handoff records.

---

## How to Use in Other Projects

### Method 1: Project-Level Skill (Workspace)
To add this skill to any existing git repository or project:
1. Copy the `.agents/skills/progress-tracker/` folder into your repository root:
   ```bash
   mkdir -p .agents/skills
   cp -r /path/to/progress-tracker .agents/skills/
   ```
2. Whenever you start work, the agent will automatically discover the skill and maintain `progress.md`.

### Method 2: Global Skill (Across all projects)
To make this skill available across all workspaces on your machine:
1. Place the folder into your global Antigravity config:
   - **Path:** `~/.gemini/config/skills/progress-tracker/`
2. All Antigravity sessions on your machine will automatically have access to this skill.

---

## Quick Start Template
To manually initialize `progress.md` in any new project, copy [`resources/template.md`](./resources/template.md) to `progress.md` in the project root.
