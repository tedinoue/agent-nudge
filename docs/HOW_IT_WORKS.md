# How it works

## The pieces

| file | role |
|---|---|
| `nudge/nudge_hook.py` | the Claude Code PreToolUse hook: forwards the act to the service, turns the answer into a pause, a note or nothing |
| `nudge/nudge_service.py` | one per machine on 127.0.0.1:8395: holds the key, decides pause/note/none, logs, circuit breaker |
| `nudge/nudge_core.py` | the judge's prompt, the act summary, the transcript reader, the API call |
| `nudge/rules.md` | the rules the judge reads |

Why a separate service: the hook runs on every act, and the service keeps state between acts (the repeat window,
the pause-once memory, the breaker). With the `api` backend it also reads the key once: fetching it from a secret
store took 16.8 seconds on one of our machines.

## Who pays: the backend

`NUDGE_BACKEND=subscription` (the default) runs each judgment as `claude -p --model <haiku> --system-prompt <the judge
prompt> --json-schema <the verdict schema> --tools "" --setting-sources "" --strict-mcp-config
--no-session-persistence --output-format json`, from the temp folder, with `ANTHROPIC_API_KEY` removed from its
environment so the Claude login pays, not a stray key. Empty setting sources mean no hooks run in the child, so the
nudge never judges itself. The `claude -p` call runs at `--effort low`. `NUDGE_BACKEND=api` calls the
Messages API directly with the rules as a cached system prompt. Timing on our Mac: subscription 2 to 4 s on simple acts and 3 to 13 s on long, complex ones
(a replay of six real commands; through the CLI, Haiku writes far more before it answers); API median 1.35 s, slowest
4.9 s. The service's budget is 15 s for subscription and 5 s for the API. The hook waits up to 16 s, so register it
with a timeout of at least 17 (the README uses 20). With a 6 s budget, the subscription route timed out on 6 of 14
real acts in our first try, the complex ones, which are the acts most worth judging.

## What the judge sees

`nudge_core.act_summary` builds one short text per act:

- **MACHINE**: one line you set (`NUDGE_MACHINE`). Ours names the OS and whose copy of the repository lives there.
  Without it, the judge guessed at programs missing on the wrong machine.
- **THE ACT**: the tool and its input. A shell command is clipped to 3,000 characters, keeping both ends; an edit's new
  text to 500; a subagent prompt to 600.
- **RESULT OF THE AGENT'S PREVIOUS ACT** (500, both ends). Without it, the judge flagged claims the previous act had
  already checked.
- **THE USER'S LAST TWO MESSAGES** (1,300, both ends), including one sent while the agent is mid-turn.
- **THE AGENT'S LAST WORDS** before the act (the last 700 characters).
- For a commit only, **PROJECT SPEC**: the `SPEC*.md` in each committed file's folder or a parent below the repository
  root, and whether the commit carries it. This is a file-existence check; the judge decides what it means.

Nothing else of the conversation. The transcript is read from the end, so a 6 MB transcript costs about 10 ms.

## The judge's instructions

The full prompt is `SYSTEM` in `nudge_core.py`. The lines that mattered most:

- "Speak only when ONE rule clearly applies to THIS act AND what you see suggests the agent may not be following it."
- "a reminder that doesn't apply wastes the agent's time and teaches it to ignore you, which is worse than silence."
- "a rule applies only when THIS act is the kind of act the rule describes, not when the conversation is about that
  topic." (Added after it warned about quick edits because the conversation had been about long renders.)
- Be strict about evidence: the long-work rule applies only to a foreground command that clearly processes something
  big, never to a quick read or a short inline script. A key-shaped string is the secret itself, never a variable's
  name.

Output is JSON (schema-constrained): `speak`, `rule`, `line` (at most 25 words, what to do now), `certainty`
(`sure` only when the act clearly breaks the rule and doing it anyway is likely to waste time or cause harm).

## Policy in the service

- `sure` pauses the call once. The same call re-issued unchanged goes through; `nudge_fires.jsonl` records it as
  "same call re-issued".
- `maybe` becomes PreToolUse `additionalContext`, which reaches the agent with the call's result, after the call has
  run. A note is a correction after the fact, not a prevention.
- One rule fires at most once per 15 judged acts per session.
- Budget 15 seconds per verdict with the subscription backend, 5 with the API (the hook waits up to 16). A late verdict is dropped and not recorded, so a pause is never
  spent where the agent can't see it.
- Three failures in a row open a 5-minute breaker: every answer is "none" at once, a `nudge_service_FAILING` file is
  written to the log folder, and each session is told once per run of the service.
- POST requires the header `x-nudge: 1`, so a web page can't reach the service.
- Fail open everywhere: an error, timeout or dead service means the act runs.

## Logs

- `nudge_calls.jsonl`: every judged act: time, session, tool, seconds, rule, certainty, action, model and token usage
  (input, output, cache read, cache write). A verdict dropped after the budget was still paid for; its usage is logged
  with `late` when it arrives. Summing these gives the judge's cost. On our first logged calls a judgment read about
  5,000 cached tokens of rules and sent a few hundred fresh ones; the first four cost $0.0013 together at API prices.
- `nudge_fires.jsonl`: every reminder with the act it judged, written when the session's next act arrives, with
  `next`: "changed" or "same call re-issued". For a note, "changed" means little: the act already ran.

## Speed

On the API backend: median 1.35 s (one machine) and 1.45 s (the other) over the first day, slowest 4.9 s. A kept-open
HTTPS connection gave no gain; the time is on the server side. The subscription backend adds about a second for
starting `claude`.

## Operating it

- Health: `curl -s http://127.0.0.1:8395/`.
- After a code change, restart the service and check its process and health.
- Stop a service by its PID or its unit. Never by a pattern match: `pkill -f` once took out half of our services.
- We investigate every wrong reminder the moment it happens, while the act is fresh, and fix what the judge lacked
  or misread. A weekly review reads every reminder and looks for misses.
