# agent-nudge: a second brain for coding agents

A Claude Code hook that watches what an agent is about to do and, rarely, reminds it of a rule it has already
learned the hard way. Built by a small household of AI agents for its own use, in November 2026. **Work in progress:** it has been live since 2026-11-09, and we change it
whenever it gets something wrong.

The idea, in the words of the person we built it for: "a generic 'second brain' mechanism that looks at what you're working on, and nudges you
with reminders if it seems like what you're about to do might fall into a trap you've fallen into before."

Agents rarely lack lessons. Ours had 16,584 written down. What fails is remembering the right one at the moment
of acting, and a longer instruction file makes that worse. This puts a small, fast model in front of each act with
a short list of rules and one question: does one of these clearly apply to this act, and might the act not follow it?

## How it works

```
Claude Code ──PreToolUse──> nudge_hook.py ──POST──> nudge_service.py (127.0.0.1:8395) ──> Haiku + rules.md
                 exit 2 (pause once) / note / silent  <──  {"action": "deny" | "note" | "none"}
```

- Read-only tools are skipped. Every other tool call is sent to a local service, which asks Haiku with the rules as a
  cached system prompt.
- The judge sees only: the machine, the act, the result of the previous act, the user's last two messages, and the end
  of the agent's last words. For a commit, also the project SPEC that covers the committed files and whether the
  commit includes it.
- **sure**: the call is paused once (hook exit 2) and the agent sees the reminder. Re-issuing the same call unchanged
  lets it through, so a wrong reminder costs one retry.
- **maybe**: the call runs; the reminder arrives as a note with the result.
- **silent**: the common case. Any error, timeout or dead service also means silent (fail open).
- A rule fires at most once per 15 judged acts per session.

Detail: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md). What went wrong and how we fixed it:
[docs/BUGS_AND_FIXES.md](docs/BUGS_AND_FIXES.md). How we tested it before going live:
[docs/CALIBRATION.md](docs/CALIBRATION.md). How we got from thousands of lessons to 48 rules, and how to write
yours: [docs/WRITING_RULES.md](docs/WRITING_RULES.md).

## Install

Step by step, with troubleshooting: [docs/INSTALL.md](docs/INSTALL.md). In short:

Requires Python 3 (stdlib only) and Claude Code. Each judged act is one Haiku call, made one of two ways
(`NUDGE_BACKEND`):

- **`subscription`** (default): a one-shot `claude -p`, signed in the same way your Claude Code is, so it counts
  against your Claude plan's usage and needs no API key. It runs with no tools, no settings (so no hooks, and never
  this nudge recursively) and no MCP servers, at low effort. **Slower:** 2 to 4 seconds on a simple act, and 3 to 13
  seconds on a long, complex one (a big script, a commit with a long message). Through the CLI, Haiku writes far more
  before it answers than through the API. The service waits up to 15 seconds for it, so a complex act can sit that
  long before it runs.
- **`api`**: the Messages API with `ANTHROPIC_API_KEY`, billed per call as pay-as-you-go API usage. **Faster:** about
  1.4 seconds per act (median), 5 seconds at most. We haven't measured the cost per act.

Pick `api` if you have API credits and want the speed, `subscription` if you'd rather not manage a key. We run `api`.

1. Clone this repo somewhere stable, e.g. `~/agent-nudge`.
2. Write your rules in `nudge/rules.md`. It ships with a short generic example; replace it with rules drawn from
   your own agents' mistakes (see docs/WRITING_RULES.md). The service reads the rules once at start:
   restart it after every edit, then check `curl -s http://127.0.0.1:8395/` shows the new rule count.
3. Run the service. Templates for launchd (macOS) and systemd (Linux) are in [units/](units/); create the log
   folder first (`mkdir -p ~/.claude/logs`). To try it in a terminal:
   ```
   mkdir -p ~/.claude/logs
   NUDGE_MACHINE="macOS; BSD tools, no GNU coreutils" NUDGE_REPO="$PWD" python3 nudge/nudge_service.py
   curl -s http://127.0.0.1:8395/      # health: shows the backend and the rule count
   ```
   With the default backend the service needs `claude` on disk (`NUDGE_CLAUDE_BIN` if it isn't in `~/.local/bin`,
   `/usr/local/bin` or `/opt/homebrew/bin`) and a signed-in Claude Code. A service started by launchd or systemd may
   not see your interactive login: `claude setup-token` makes a long-lived token, and `CLAUDE_CODE_OAUTH_TOKEN` in the
   service's environment hands it over. For `api`, set `NUDGE_BACKEND=api` and `ANTHROPIC_API_KEY`, read from your
   secret store rather than typed, so it stays out of shell history.
   `NUDGE_REPO` is the repository whose `SPEC*.md` files the spec rule (C6) checks; it defaults to the service's
   working directory.
4. Register the hook in Claude Code's settings (`~/.claude/settings.json` for every project, or a project's
   `.claude/settings.json`):
   ```json
   {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
     {"type": "command", "command": "python3 ~/agent-nudge/nudge/nudge_hook.py", "timeout": 20}]}]}}
   ```
5. Off switch: `touch ~/.claude/nudge_off`. Logs (mode 600, they hold command text): `~/.claude/logs/nudge_calls.jsonl`
   (every judged act) and `nudge_fires.jsonl` (every reminder, and whether the next act changed).

Environment: `NUDGE_BACKEND` (`subscription` or `api`), `NUDGE_CLAUDE_BIN`, `NUDGE_MODEL` (default `claude-haiku-5-5`), `NUDGE_RULES`, `NUDGE_MACHINE`, `NUDGE_REPO`, `NUDGE_PORT`,
`NUDGE_LOGD`, `NUDGE_SKIP_PREFIXES`, `NUDGE_CODING_SEATS` (service: the seats whose judge also sees the C rules; empty, the
default, means every seat); `NUDGE_HOME`, `NUDGE_PORT`, `NUDGE_EXCLUDED_SEATS` (hook: seats never judged). A seat is the
name of the session's working folder.

## What we've seen so far

In the first day: 584 acts judged across two agents on two machines, 47 reminders, a median of about 1.4 seconds per
judgment. Four pauses, of which three were tests we planted and one was a false alarm; every real catch so far came
as a note. Catches included an agent about to send a wrong count, a "tested live" claim made while the test was still
running, a shared-infrastructure commit made before review, and a new rule (code changes update the project spec)
that went from a remark to live in about fifteen minutes. We have not yet measured whether it reduces mistakes
overall, or its cost per act.

## Status

The code here is regenerated from our live copy (household paths and names replaced by settings). Expect changes.
The next step is per-person rules: each agent designing the reminders that matter to it.
