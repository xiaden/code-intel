---
name: copilot-hooks
description: Use when creating or updating VS Code Copilot agent hook files in .github/hooks. Covers all 8 hook event types (PreToolUse, PostToolUse, SessionStart, SubagentStart, SubagentStop, Stop, UserPromptSubmit, PreCompact), the stdin/stdout JSON contract, permissionDecision allow/deny/ask, exit code semantics, hook command properties, and repository conventions for Python validator scripts.
---

# Copilot Hooks

VS Code Copilot hooks execute shell commands at lifecycle points during agent sessions. They provide deterministic, code-driven automation that instructions cannot: blocking dangerous operations, auto-formatting, injecting context, and logging.

**Feature status:** Preview. Enable with `chat.hookFilesLocations[".github/hooks"] = true`.

---

## Hook Lifecycle Events

| Event | Fires when | Common uses |
| --- | --- | --- |
| `SessionStart` | First prompt of a new session | Inject project context, validate environment |
| `UserPromptSubmit` | User submits any prompt | Audit requests, inject system context |
| `PreToolUse` | Before agent invokes a tool | Block dangerous ops, validate args, require approval |
| `PostToolUse` | After tool completes successfully | Run formatters, log results, trigger follow-up |
| `PreCompact` | Before context compaction | Export context, save state |
| `SubagentStart` | Subagent is spawned | Inject shared context into the child session |
| `SubagentStop` | Subagent completes | Aggregate results, clean up resources |
| `Stop` | Agent session ends | Generate reports, cleanup, notifications |

---

## File Locations

- **Repository hooks:** `.github/hooks/*.json`
- **User hooks:** `~/.copilot/hooks`
- **Agent-scoped hooks:** `hooks` field in `.agent.md` frontmatter (requires `chat.useCustomAgentHooks: true`)

---

## Hook Configuration Format

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": "python .github/hooks/my-validator.py",
        "windows": "py -3 .github/hooks/my-validator.py",
        "timeout": 15
      }
    ]
  }
}
```

| Property | Required | Description |
| --- | --- | --- |
| `type` | Yes | Must be `"command"` |
| `command` | Yes | Default command |
| `windows` / `linux` / `osx` | No | OS override (extension host platform determines which runs) |
| `cwd` | No | Working directory relative to repo root |
| `env` | No | Extra environment variables |
| `timeout` | No | Seconds before timeout (default: 30) |

---

## stdin / stdout Contract

All events receive common fields on stdin: `timestamp`, `cwd`, `sessionId`, `hookEventName`, `transcript_path`.

Common stdout fields:

| Field | Description |
| --- | --- |
| `continue` | `false` stops the session (drastic — prefer `permissionDecision: deny`) |
| `stopReason` | Required when `continue` is `false` |
| `systemMessage` | Warning shown to user regardless of other decisions |

### Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success — parse stdout as JSON |
| `2` | Blocking error — show stderr to the model, stop processing |
| Other | Non-blocking warning — show to user, continue |

Exit code `2` is the simplest way to block without writing JSON.

### Decision Priority

When multiple hooks run for the same event, the most restrictive wins: `deny` > `ask` > `allow`.

---

## Event Reference

### PreToolUse

Extra input: `tool_name`, `tool_input`, `tool_use_id`

`hookSpecificOutput` fields:

| Field | Values | Description |
| --- | --- | --- |
| `permissionDecision` | `"allow"` / `"deny"` / `"ask"` | Controls tool execution |
| `permissionDecisionReason` | string | Reason shown to user |
| `updatedInput` | object | Modified tool input (schema must match — check logs if ignored) |
| `additionalContext` | string | Injected into the model's conversation |

### PostToolUse

Extra input: `tool_name`, `tool_input`, `tool_use_id`, `tool_response`

`hookSpecificOutput`: `additionalContext` (string). Top-level `decision: "block"` + `reason` blocks further processing.

### SessionStart

Extra input: `source: "new"`. `hookSpecificOutput`: `additionalContext` only.

### UserPromptSubmit

Extra input: `prompt`. Uses common output format only.

### SubagentStart

Extra input: `agent_id`, `agent_type`. `hookSpecificOutput`: `additionalContext` only.

### SubagentStop

Extra input: `agent_id`, `agent_type`, `stop_hook_active`. Top-level `decision: "block"` + `reason` re-queues the subagent.

**Always check `stop_hook_active`** before emitting `decision: block` — infinite loops consume premium requests.

### Stop

Extra input: `stop_hook_active`. Block via `hookSpecificOutput.decision: "block"` + `reason`.

**Always check `stop_hook_active`.**

### PreCompact

Extra input: `trigger: "auto"`. Uses common output format only.

---

## Repository Conventions

### Script pattern

See existing scripts (e.g. `validate-runsubagent-agent.py`) for the full boilerplate. Key rules:

- Read JSON from `stdin`, write JSON to `stdout` (use `json.dumps` — never raw `print`)
- Handle empty or malformed stdin gracefully — never crash
- Policy hooks emit `allow` for all non-matching tool names
- Observational hooks always emit `allow` — wrap capture logic in `try/except`, never deny on failure
- Deny reasons must be actionable: state what failed and what to do instead

### Config convention

Pair each script with a `.json` of the same base name. Always use `"windows": "py -3 ..."` for Python scripts.

### Hook ordering

Hooks run alphabetically by filename. Use prefixes (`10-validate-...`, `20-capture-...`) when order matters. Capture hooks should mirror validator logic defensively so they never write state for denied calls.

### Tool name normalization

VS Code uses `camelCase` names. Normalize before matching:

```python
normalized = tool_name.strip().lower().replace("-", "_")
```

### VS Code vs Claude Code

- Property names: Claude uses `snake_case` (`file_path`), VS Code uses `camelCase` (`filePath`)
- Matchers (`"Edit|Write"`) are parsed but **ignored** — all hooks fire on every matching event

---

## Existing Hooks

| Hook | Event | Purpose |
| --- | --- | --- |
| `validate-runsubagent-agent` | `PreToolUse` | Deny `runSubagent` calls targeting unknown agents (missing/undeclared `agentName`) |
| `shared-context-pretooluse` | `PreToolUse` | Capture spawn envelope for eligible `runSubagent` calls (never denies) |
| `shared-context-subagentstart` | `SubagentStart` | Correlate child start with pending spawn envelope (never denies) |

---

## Authoring Checklist

- [ ] Config `.json` in `.github/hooks/` with `type: "command"`
- [ ] `windows:` override uses `py -3` for Python scripts
- [ ] Reads JSON from stdin, writes JSON to stdout
- [ ] Handles empty/malformed stdin gracefully
- [ ] Policy hooks emit `allow` for non-matching tool names
- [ ] Observational hooks always emit `allow`
- [ ] Deny reasons are actionable
- [ ] `stop_hook_active` checked before emitting `decision: block`
- [ ] Timeout ≤15s for validators
- [ ] README.md in `.github/hooks/` updated
- [ ] Test file in `.github/hooks/tests/` added

---

## Debugging

- **Check loaded hooks:** VS Code → View Logs → search `Load Hooks`
- **View hook output:** Output panel → `GitHub Copilot Chat Hooks`
- **Dump raw payload:** `dump-hook-input.py` logs stdin to stderr
- Hook not running: verify `.json` extension, `type: "command"`, correct directory
- JSON parse error: use `json.dumps` — never raw `print`
- `updatedInput` ignored: schema mismatch — check agent debug logs
