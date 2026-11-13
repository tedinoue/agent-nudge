# Installing agent-nudge in your own Claude Code

This guide takes you from a fresh clone to a working before-act nudge: a small model that looks at each action your
Claude Code agent is about to take and, now and then, reminds it of one of your rules. Allow about half an hour, most
of it spent writing your first rules.

## What you need

- **Claude Code**, installed and signed in.
- **Python 3.** Nothing to install from pip: the code uses only the standard library.
- **macOS or Linux.** The service runs in the background under launchd (macOS) or systemd (Linux); templates for
  both are in `units/`.
- **One of two ways to pay for the judge**, which makes one small model call per judged action:
  - *Subscription* (the default): the service runs `claude -p`, so the calls count against your Claude plan. No API
    key. Slower: 2 to 13 seconds per action.
  - *API*: the Messages API with your `ANTHROPIC_API_KEY`, billed per call. Faster: about 1.4 seconds per action.

## 1. Get the code

```
git clone https://github.com/tedinoue/agent-nudge ~/agent-nudge
mkdir -p ~/.claude/logs
```

The rest of this guide assumes `~/agent-nudge`. If you put it elsewhere, change the paths to match.

## 2. Write your first rules

The judge only knows the rules you give it, in `nudge/rules.md`. The file in the repo is a short generic example;
replace it with rules about your own agents' mistakes.

Start small: five to ten rules, each a mistake your agent has actually made more than once. Each rule is one line,
followed by the moment it applies:

```
G1. Before telling the user something is ready, use it the way they will: run the real command, open the real page.  [Fires when: SAID: a reply says ready, fixed, live or done.]
G2. Before restarting or stopping a running service, check who else is using it.  [Fires when: ACT: a restart or stop of a running service.]
```

- `G` rules apply to every agent; `C` rules only to agents that write software (see `NUDGE_CODING_SEATS` below).
- The moment says what the judge looks at: `ACT` (the action itself), `SAID` (what the agent or the user just
  wrote), `RESULT` (what the previous action returned).
- `docs/WRITING_RULES.md` explains how we turned thousands of written-down lessons into a short list, and how to do
  the same with your own.

## 3. Start the service and check it

Try it in a terminal first. On macOS:

```
cd ~/agent-nudge
NUDGE_MACHINE="macOS; BSD tools, no GNU coreutils" NUDGE_REPO="$HOME/your-project" python3 nudge/nudge_service.py
```

On Linux, use `NUDGE_MACHINE="Linux, GNU tools"`. Then, from a second terminal:

```
curl -s http://127.0.0.1:8395/
```

The reply names the backend and the number of rules the judge was given. Rules whose only trigger is `CODE:` (a
tool you run already enforces them) are left out of that count, so it can be lower than the number of rules in the
file; any other difference means the file did not parse as you meant.

- For the API backend, add `NUDGE_BACKEND=api` and make `ANTHROPIC_API_KEY` available to the service. Read it from
  your keychain or secret store rather than typing it, so it stays out of your shell history.
- `NUDGE_REPO` is the project whose `SPEC*.md` files the specification rule checks. It defaults to the folder the
  service starts in.

## 4. Run it in the background

Once it works in the terminal, stop it (Ctrl-C) and install it as a background service.

**macOS (launchd):**

1. Copy `units/agent-nudge.plist` to `~/Library/LaunchAgents/`.
2. Replace `HOME_DIR` and `YOUR_REPO_DIR` with real paths.
3. For the subscription backend, launchd cannot see your interactive Claude login. Run `claude setup-token` once and
   give the service the token as `CLAUDE_CODE_OAUTH_TOKEN`. For the API backend, point the plist at a small wrapper
   script that reads the key from your keychain, exports it and starts the service; never paste the key into the
   plist.
4. Start it:
   ```
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/agent-nudge.plist
   curl -s http://127.0.0.1:8395/
   ```

**Linux (systemd, user service):**

1. Copy `units/agent-nudge.service` to `~/.config/systemd/user/`.
2. Set `NUDGE_REPO` in it. For the API backend, put both `NUDGE_BACKEND=api` and `ANTHROPIC_API_KEY=...` in
   `~/.config/agent-nudge.env` and run `chmod 600` on that file.
3. Start it:
   ```
   systemctl --user daemon-reload && systemctl --user enable --now agent-nudge
   curl -s http://127.0.0.1:8395/
   ```

The service reads the rules once, at start. **Restart it after every edit to `rules.md`,** then check the rule count
with `curl` again.

## 5. Connect it to Claude Code

Add the hook to Claude Code's settings: `~/.claude/settings.json` to cover every project, or a project's own
`.claude/settings.json` to cover just that one. If the file already has a `hooks` section, add this entry to its
`PreToolUse` list rather than replacing the section.

```json
{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
  {"type": "command", "command": "python3 ~/agent-nudge/nudge/nudge_hook.py", "timeout": 20}]}]}}
```

Start a new Claude Code session; hooks load when a session starts.

## 6. See it work

Ask your agent to do something one of your rules covers, for example to restart a service without saying why.

- When the judge is **sure** a rule applies, the action is paused once and the agent sees the reminder. If the
  agent repeats the same action unchanged, it goes through, so a wrong reminder costs only one retry.
- When the judge thinks a rule **may** apply, the action runs and the reminder arrives with its result.
- Most of the time it says nothing.

The logs in `~/.claude/logs/` show what happened. They are readable only by you, because they hold command text:

- `nudge_calls.jsonl`: every judged action, with the judge's answer.
- `nudge_fires.jsonl`: every reminder, and whether the agent's next action changed.
- `nudge_acts.jsonl`: the full text of every judged action and the user's last two messages. It is the largest and
  most sensitive of the logs; delete it whenever you like.
- `nudge_service.log`: the service's own messages, including why it stopped.

Read the reminders for the first few days. A rule that fires on the wrong things needs a sharper "Fires when"
line; a mistake that keeps slipping through needs a rule.

## Settings

All optional, set in the service's or the hook's environment:

| Setting | Where | What it does |
|---|---|---|
| `NUDGE_BACKEND` | service | `subscription` (default) or `api` |
| `NUDGE_MODEL` | service | the judge model; default `claude-haiku-5-5` |
| `NUDGE_RULES` | service | a rules file other than `nudge/rules.md` |
| `NUDGE_MACHINE` | service | one line describing the machine, for rules about commands and tools |
| `NUDGE_REPO` | service | the project whose `SPEC*.md` files the specification rule checks |
| `NUDGE_CODING_SEATS` | service | the seats that also get the `C` rules; empty means every seat |
| `NUDGE_CLAUDE_BIN` | service | where `claude` is, if not in `~/.local/bin`, `/usr/local/bin` or `/opt/homebrew/bin` |
| `NUDGE_PORT` | both | the service's port; default 8395 |
| `NUDGE_EXCLUDED_SEATS` | hook | seats never judged |
| `NUDGE_SEAT` | hook | the seat name for a session started outside a project folder |

A *seat* is the name of the folder a Claude Code session starts in. Use seats to give only your coding agents the
`C` rules, or to leave an agent out of judging altogether.

## Turning it off

- **For a moment:** `touch ~/.claude/nudge_off`. Delete the file to turn it back on.
- **For good:** remove the hook entry from `settings.json`, then stop the service
  (`launchctl bootout gui/$(id -u)/agent-nudge` on macOS, `systemctl --user disable --now agent-nudge` on Linux).

## When something is wrong

- **No reminders at all.** Check `curl -s http://127.0.0.1:8395/`. If the service is down, the hook stays silent by
  design, so a dead service looks the same as a quiet one. Check that the hook entry is in the settings file and that
  you started a new session.
- **It works in a terminal but not under launchd or systemd.** The service usually cannot see your Claude login or
  API key there. With the API backend and no key it stops at once (see `nudge_service.log`); with the subscription
  backend it starts, but every judgment fails and it goes quiet. See step 4.
- **Every action is slow.** The subscription backend takes seconds per action. Switch to the API backend, or exclude
  seats that do not need judging.
- **A reminder is wrong.** Re-issue the action unchanged and it goes through. Then fix the rule's "Fires when" line
  and restart the service.
