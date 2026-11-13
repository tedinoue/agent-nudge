#!/usr/bin/env python3
"""Claude Code PreToolUse hook, all tools: the before-act nudge. Register it in settings.json (see README).

Read-only tools are skipped. The hook asks the machine's nudge service (nudge_service.py, 127.0.0.1:8395), which holds
the API key and asks Haiku whether one of the rules (rules.md) applies to THIS act:
  deny  -> exit 2 with the reminder: the act does not run this time; the same call re-issued unchanged goes through.
  note  -> additionalContext JSON (arrives with the tool result), exit 0.
  none, service down, timeout, any error -> exit 0, silent (fail open).
Off switch: touch ~/.claude/nudge_off. NUDGE_HOME: the folder holding nudge_core.py (default: this file's folder).
"""
import json, os, sys, urllib.request

EXCLUDED = set(x for x in os.environ.get("NUDGE_EXCLUDED_SEATS", "").split(",") if x)   # seats never judged
URL = "http://127.0.0.1:" + os.environ.get("NUDGE_PORT", "8395") + "/judge"


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    sid, tool = d.get("session_id", ""), d.get("tool_name", "")
    if os.path.exists(os.path.expanduser("~/.claude/nudge_off")):
        return 0
    sys.path.insert(0, os.environ.get("NUDGE_HOME") or os.path.dirname(os.path.abspath(__file__)))
    try:
        import nudge_core as N
        if not N.worth_judging(tool):
            return 0
    except Exception:
        return 0
    seat = os.environ.get("NUDGE_SEAT") or os.path.basename(d.get("cwd") or "")   # NUDGE_SEAT: set it for headless jobs
    if seat in EXCLUDED:
        return 0
    body = json.dumps({"sid": sid, "seat": seat, "tool_name": tool, "tool_input": d.get("tool_input") or {},
                       # the service waits up to 1 s for this id to reach the transcript it is given. A helper agent's
                       # call is written to its OWN file under subagents/, never to that transcript, so every helper
                       # act waited the full second for nothing (742 of 1,260 acts, review 2026-11-11): send no id.
                       "tool_use_id": "" if d.get("agent_id") else d.get("tool_use_id", ""),
                       "helper": bool(d.get("agent_id")),   # its words are not in this transcript either
                       "transcript_path": d.get("transcript_path", "")}).encode()
    try:
        req = urllib.request.Request(URL, data=body, method="POST", headers={"content-type": "application/json", "x-nudge": "1"})
        with urllib.request.urlopen(req, timeout=16) as r:  # an upper bound: the service answers within its own budget (5 s api, 15 s subscription)
            if r.status != 200:
                return 0
            a = json.loads(r.read())
    except Exception:
        return 0
    if a.get("action") == "deny" and a.get("text"):
        print(a["text"] + "\n(A reminder, not an error. The act did not run. If it already follows this, re-issue the same call unchanged and it will go through.)", file=sys.stderr)
        return 2
    if a.get("action") == "note" and a.get("text"):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": a["text"]}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
