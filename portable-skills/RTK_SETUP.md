# RTK (Rust Token Killer) — separate CLI setup

RTK is a standalone command-line utility that compresses common terminal output before it reaches a coding agent. It is not a Claude Code skill and is not part of the Creator Studio runtime.

## Windows installation

The official project currently documents native Windows support. Preferred installation:

```powershell
winget install rtk-ai.rtk
```

Open a new PowerShell terminal, then verify:

```powershell
rtk --version
rtk gain
```

The `rtk gain` command should display RTK's savings dashboard. This also helps distinguish Rust Token Killer from an unrelated Rust Type Kit package that uses the same `rtk` command name.

## Enable Claude Code integration

Only after confirming the binary is the expected Rust Token Killer build:

```powershell
rtk init -g
```

This configures the global Claude Code integration. Restart Claude Code if requested, then verify the hook/configuration using the instructions printed by RTK. Current native Windows releases support the native hook; older installations may need `rtk init -g` again to migrate.

## Safety and scope

- This is a user-machine installation, not a repository change.
- Inspect the changes RTK proposes to Claude Code settings before accepting them.
- Do not overwrite existing Claude settings blindly.
- Do not assume RTK is installed until `rtk --version` and `rtk gain` succeed.
- RTK compresses command output; it does not replace reading source files, inspecting diffs, or running tests.
- Some Claude Code built-in tools do not pass through shell hooks. Use RTK-aware shell commands when useful, but do not contort workflows merely to maximize compression.

Official sources:
- [RTK repository and Windows installation](https://github.com/rtk-ai/rtk)
- [RTK installation guide](https://www.rtk-ai.app/docs/getting-started/installation/)

The official project currently recommends `winget install rtk-ai.rtk` for Windows. Verify the instructions again before a future installation because releases and flags can change.
