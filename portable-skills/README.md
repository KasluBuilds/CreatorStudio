# Portable Claude Code skills and tools

This folder contains portable, version-controlled setup assets. It is deliberately outside `.claude/` so it can be reviewed and distributed without making assumptions about a machine-local Claude Code installation.

## Skills

- `lean-engineering/SKILL.md`: combines Ponytail's minimal-but-complete engineering with Caveman's concise, accurate communication.
- `github-sync/SKILL.md`: safe Git/GitHub inspection, synchronization, branching, review and reporting.

For a global install on Windows, run the included safe installer from this directory:

```powershell
.\Install-PortableSkills.ps1 -WhatIf
.\Install-PortableSkills.ps1
```

It installs both skills into `$HOME\.claude\skills\`. Existing files are never overwritten; identical installs are skipped and differing existing skills are left untouched.

For a single repository, copy a skill folder into `<repo>\.claude\skills\<skill-name>\SKILL.md`. Claude Code discovers skills from personal and project skill directories. See the official [Claude Code skills documentation](https://docs.anthropic.com/en/docs/claude-code/skills).

## Separate CLI: RTK

RTK (Rust Token Killer) is a standalone CLI, not a prompt skill. See `RTK_SETUP.md`. Installing the binary and enabling its Claude Code hook are separate actions; do not assume either is installed.

## Explicitly not enabled

`claude-mem` was considered as a pilot, then deliberately turned off. GitHub remains the primary shared project memory. Do not install or re-enable claude-mem unless the user explicitly revisits that decision.
