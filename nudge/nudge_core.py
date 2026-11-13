#!/usr/bin/env python3
"""The judging core of the before-act nudge: a "second brain" that looks at what an agent is about to do and reminds
it of a rule it has learned the hard way, when the act looks like it may break one.

act_summary() turns a Claude Code PreToolUse payload plus recent context into a short "about to" text; judge() asks
Haiku whether ONE of the rules in rules.md clearly applies to THIS act and the act may not follow it, and returns
{"rule": "G12", "line": "...", "certainty": "sure"|"maybe"} or None. "sure" pauses the act once; "maybe" rides along
as a note. Stdlib only. Used by nudge_service.py.

Environment: NUDGE_MODEL (default claude-haiku-5-5), NUDGE_RULES (default rules.md beside this file), NUDGE_MACHINE
(one line telling the judge which machine this is and anything machine-specific), NUDGE_REPO (the repository whose
SPEC files the spec rule checks; default the service's working directory), NUDGE_SKIP_PREFIXES (comma-separated tool
name prefixes to skip as read-only).
"""
import json, os, re, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("NUDGE_MODEL", "claude-haiku-5-5")
# Who pays for each judgment. "subscription": a one-shot `claude -p`
# signed in as the user's Claude Code (subscription or whatever that login is): 2-4 s on simple acts and 3-13 s on long,
# complex ones even at --effort low (11-10 replay of 6 real commands; the CLI's structured output writes far more tokens);
# "api": the Messages API with ANTHROPIC_API_KEY, billed per call, 1.35 s median.
BACKEND = os.environ.get("NUDGE_BACKEND", "subscription")
CLAUDE_BIN = os.environ.get("NUDGE_CLAUDE_BIN") or next((p for p in (os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude",
                                                                     "/opt/homebrew/bin/claude") if os.access(p, os.X_OK)), "claude")
RULES = open(os.environ.get("NUDGE_RULES") or os.path.join(HERE, "rules.md")).read()
MACHINE = os.environ.get("NUDGE_MACHINE") or ("macOS" if sys.platform == "darwin" else "Linux, GNU tools")  # rules like G4 are machine-specific


def _code_only(line):
    """A rule whose only trigger is CODE (a tool already enforces it exactly): the judge would only be guessing."""
    m = re.search(r"\[Fires when: (.*)", line)
    return bool(m) and m.group(1).startswith("CODE:") and not re.search(r"\b(ACT|RESULT|SAID):", m.group(1))


# Code-only rules ("Fires when: CODE:" and no other trigger) stay out of the judge's list: a tool already enforces them
# exactly, and the judge could only guess.
# NUDGE_KEEP_CODE_RULES=1 keeps them.
KEEP_CODE_RULES = os.environ.get("NUDGE_KEEP_CODE_RULES") == "1"
if not KEEP_CODE_RULES:
    RULES = "\n".join(l for l in RULES.split("\n") if not (re.match(r"[GC]\d+\.", l) and _code_only(l)))
RULE_IDS = set(re.findall(r"^([GC]\d+)\.", RULES, re.M))
# The C rules
# are for seats that build software; every other seat's judge sees the G rules only, and a C verdict there is dropped.
CODING_SEATS = {x for x in os.environ.get("NUDGE_CODING_SEATS", "").split(",") if x}
RULES_G = "\n".join(l for l in RULES.split("\n") if not re.match(r"C\d+\.", l))
RULE_IDS_G = {r for r in RULE_IDS if r.startswith("G")}


def is_coding(seat):
    """Whether this seat's judge sees the C rules too. An empty NUDGE_CODING_SEATS makes every seat a coding seat."""
    return not CODING_SEATS or seat in CODING_SEATS

SYSTEM = """You are a second brain watching an AI agent just before it acts. You know the rules below, each learned from real mistakes. Your job is to catch the agent about to fall into a trap one of them describes.

Each act comes with the machine it runs on, the result of the agent's previous act, the user's last two messages to the agent, and the agent's last words before the act (all clipped). They are context for judging the act: a rule applies only when THIS act is the kind of act the rule describes, not when the conversation is about that topic.

Speak only when ONE rule clearly applies to THIS act AND what you see suggests the agent may not be following it (for example: about to start a long job in the foreground; about to report "fixed" without having checked; about to run a command with a program the machine lacks; about to use a name not in the directory). If the act already follows the rule, or the rule only loosely relates, stay silent. Most acts need no reminder; a reminder that doesn't apply wastes the agent's time and teaches it to ignore you, which is worse than silence.

Be strict about evidence. The long-work rule (run it in the background) applies only to a FOREGROUND call whose command clearly processes something big (many files or items, a network call per item, a model, a build or render), never to a quick read, list, grep, cat, status, an inline script or heredoc that reads or edits local files (seconds), or a call already in the background. A key-shaped string is the secret itself (a long random token), never the name of a variable or of the tool that fetches it. A rule about checking claims applies only when the act itself states or sends a claim (a reply, a letter, a commit message, a memory) that the agent's last words and the previous result show was not checked.

When you speak, give the rule id, one plain sentence to the agent, at most 25 words, saying what to do now for THIS act ("This build will take minutes: run it in the background and set a check on it."), and certainty: "sure" only when the act as shown clearly breaks the rule and doing it anyway is likely to waste time or cause harm; otherwise "maybe".

RULES:
""" + RULES
if not KEEP_CODE_RULES:   # the speak examples must point at rules the judge still has
    SYSTEM = SYSTEM.replace("; about to run a command with a program the machine lacks; about to use a name not in the directory)",
                            "; about to overwrite data without a backup)", 1)
SYSTEM_G = SYSTEM[:-len(RULES)] + RULES_G
# The v2 layout names its sections in plain words; the instructions name them the same way.
_V1_INPUTS = ("Each act comes with the machine it runs on, the result of the agent's previous act, the user's last two messages to the agent, "
              "and the agent's last words before the act (all clipped).")
_V2_INPUTS = ("Each act comes as: the machine, whose session it is, the user's last two prompts, what the agent's previous action "
              "returned, what the agent last said, and the action itself, described step by step with the files and programs it "
              "touches (\"[!]\" marks a step that changes or deletes something, and \"Checked on this machine\" lists facts read "
              "from the machine itself, which you can rely on).")
assert SYSTEM.count(_V1_INPUTS) == 1
SYSTEM_V2, SYSTEM_G_V2 = SYSTEM.replace(_V1_INPUTS, _V2_INPUTS), SYSTEM_G.replace(_V1_INPUTS, _V2_INPUTS)   # the same prompt with the C (coding) rules left out

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["speak", "rule", "line", "certainty"], "properties": {
    "speak": {"type": "boolean"}, "rule": {"type": "string"}, "line": {"type": "string"}, "certainty": {"type": "string", "enum": ["sure", "maybe"]}}}

SKIP_TOOLS = {"Read", "Grep", "Glob", "LS", "ToolSearch", "TaskOutput", "TodoWrite", "ListAgents", "ReadNotifications", "Monitor",
              "WebSearch", "WebFetch", "AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
SKIP_PREFIXES = tuple(p for p in os.environ.get("NUDGE_SKIP_PREFIXES", "mcp__claude-in-chrome__read,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__tabs_context,mcp__computer-use__screenshot").split(",") if p)


def worth_judging(tool_name):
    """Read-only tools are skipped to keep latency off the common path (routing, not a content judgment)."""
    return tool_name not in SKIP_TOOLS and not tool_name.startswith(SKIP_PREFIXES)


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + " ..."


def _tail(s, n):
    """The END of s (the words right before the act matter most)."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else "... " + s[-n:]


def _ends(s, n):
    """Both ends of s: a request often opens with the ask and closes with the constraint."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n // 2] + " ... " + s[-(n // 2):]


def act_summary(tool_name, tool_input, last_words="", user_said="", prev_result="", seat=""):
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        body = f"description: {_clip(ti.get('description'), 200)}\ncommand: {_ends(ti.get('command'), 3000)}"
        if ti.get("run_in_background"): body += "\n(run in the background)"
        if ti.get("timeout"): body += f"\n(the tool's own time limit for this call: {ti.get('timeout')} ms; not a timeout command)"
    elif tool_name in ("Edit", "Write", "NotebookEdit"):
        body = f"file: {ti.get('file_path') or ti.get('notebook_path')}\nnew text: {_clip(ti.get('new_string') or ti.get('content') or ti.get('new_source'), 500)}"
    elif tool_name == "Agent":
        body = f"description: {_clip(ti.get('description'), 200)}\nbackground: {bool(ti.get('run_in_background'))}\nprompt: {_clip(ti.get('prompt'), 600)}"
    else:
        body = _clip(json.dumps(ti, ensure_ascii=False), 700)
    spec = spec_context(tool_name, ti)
    if spec: body += f"\nPROJECT SPEC: {spec}"
    seat = re.sub(r"[^A-Za-z0-9_-]", "", seat or "")[:40]   # a folder name, never more text
    who = f"THE AGENT: a session working in the {seat} folder\n" if seat else ""
    return (f"MACHINE: {MACHINE}\n{who}"
            f"THE ACT: TOOL: {tool_name}\n{body}\n"
            f"RESULT OF THE AGENT'S PREVIOUS ACT: {_ends(prev_result, 500) or '(none)'}\n"
            f"THE USER'S LAST TWO MESSAGES TO THE AGENT: {_ends(user_said, 1300) or '(none)'}\n"
            f"THE AGENT'S LAST WORDS BEFORE THIS: {_tail(last_words, 700)}")


LAYOUT = os.environ.get("NUDGE_LAYOUT", "v1")   # v2: plain labels and a parsed action; v1 until it passes the user's test


def describe_action(tool_name, tool_input, cwd=None, check=True):
    """The action in plain words: Bash through act_parse, the file tools and subagents by their parts, any other tool as
    its fields, one per line."""
    import act_parse
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        d = act_parse.describe_bash(ti.get("command") or "", ti.get("description") or "", bool(ti.get("run_in_background")), ti.get("timeout"), cwd)
        return d if check else d.rsplit("\nChecked on this machine:", 1)[0]   # an act from another machine: no checks from this one
    if tool_name == "Write":
        c = ti.get("content") or ""
        return f"Write the file {act_parse._short(ti.get('file_path') or '')} ({len(c.splitlines())} lines), replacing any existing file. It begins:\n{_clip(c, 500)}"
    if tool_name == "Edit":
        return (f"Edit the file {act_parse._short(ti.get('file_path') or '')}{' (every occurrence)' if ti.get('replace_all') else ''}.\n"
                f"Replace: {_clip(ti.get('old_string'), 300)}\nWith: {_clip(ti.get('new_string'), 400)}")
    if tool_name == "Agent":
        return (f"Start a helper agent{' in the background' if ti.get('run_in_background') else ''}: {_clip(ti.get('description'), 150)}\n"
                f"Its instructions begin: {_clip(ti.get('prompt'), 500)}")
    name = tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name
    fields = "\n".join(f"  {k}: {_clip(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 400)}" for k, v in ti.items())
    return f"Use the tool {name} with:\n{fields}"


def act_summary_v2(tool_name, tool_input, last_words="", prompts=(), prev_result="", seat="", words_older=False, machine=None, cwd=None, check=True):
    """The act for the judge in plain labels: who and where, the user's last two prompts, the previous
    result, what the agent said, then the action, described rather than pasted."""
    seat = re.sub(r"[^A-Za-z0-9_-]", "", seat or "")[:40]
    ps = list(prompts)[:2][::-1]   # oldest first
    out = [f"Machine: {machine or MACHINE}."]
    if seat: out.append(f"Session: working in the {seat} folder.")
    out.append("")
    if ps:
        out.append("User prompts:")
        for k, p in enumerate(ps, 1):
            out.append(f"User prompt {k}{' (latest)' if k == len(ps) else ''}: {_ends(p, 800)}")
    else:
        out.append("User prompts: (none in this session yet)")
    # the phrase is built outside the f-string: a backslash inside an f-string expression does not parse before
    # Python 3.12, and the hook can run under /usr/bin/python3 3.9, where the import failed and the nudge was silently
    # off (review 2026-11-11)
    when = (" (before the user's latest prompt; nothing since)" if words_older == "prompt"
            else " (before its previous action; nothing since)" if words_older else ", just before this action")
    out += ["", f"What the agent's previous action returned: {_ends(prev_result, 500) or '(nothing yet)'}",
            "", f"What the agent last said{when}: {_tail(last_words, 700) or '(nothing)'}",
            "", "The action the agent is about to take:", describe_action(tool_name, tool_input, cwd, check)]
    spec = spec_context(tool_name, tool_input if isinstance(tool_input, dict) else {})
    if spec: out.append(f"Project spec: {spec}")
    return "\n".join(out)


ROOT = os.environ.get("NUDGE_REPO") or os.getcwd()
_COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b")


def spec_context(tool_name, tool_input):
    """For a commit: the project SPEC covering each committed file, and whether the commit carries it (rule C6).
    File existence only, no judgment: a SPEC*.md in the file's folder or a parent below the repository root."""
    cmd = (tool_input or {}).get("command", "") if tool_name == "Bash" else ""
    if not cmd or not _COMMIT.search(cmd):
        return ""
    toks = set(re.findall(r"[\w./~-]+\.\w+", cmd))
    files = []
    for t in toks:
        f = os.path.expanduser(t)
        f = f if os.path.isabs(f) else os.path.join(ROOT, f)
        if os.path.isfile(f) and os.path.realpath(f).startswith(os.path.realpath(ROOT) + os.sep):
            files.append(os.path.relpath(os.path.realpath(f), os.path.realpath(ROOT)))
    specs = {}
    for f in files:
        if os.path.basename(f).startswith("SPEC"):
            continue
        d = os.path.dirname(f)
        while d:  # stop at the repository root (a root-level SPEC is not one project's)
            try: hit = sorted(x for x in os.listdir(os.path.join(ROOT, d)) if x.startswith("SPEC") and x.endswith(".md"))
            except Exception: hit = []
            if hit:
                specs.setdefault(os.path.join(d, hit[0]), []).append(f); break
            d = os.path.dirname(d)
    out = [f"{sp} covers {', '.join(sorted(fs)[:4])}: {'IN this commit' if sp in files else 'NOT in this commit'}" for sp, fs in specs.items()]
    return "; ".join(out)


_TAGGED = re.compile(r"<([a-z][a-z0-9_-]*)[^>]*>.*?</\1>", re.S)


def transcript_context(path, max_bytes=2_000_000, wait_for="", max_wait=1.0, stats=None):
    words, user_msgs, res = transcript_parts(path, max_bytes, wait_for, max_wait, stats)[:3]
    # no transcript, or no message from the user in its last 2 MB: judge without the user's words; user_msgs[0] used to raise here and
    # the service then answered "none" with nothing logged (review 2026-11-11)
    user_msg = "" if not user_msgs else user_msgs[0] if len(user_msgs) < 2 else f"[earlier] {_ends(user_msgs[1], 500)} [latest] {user_msgs[0]}"
    return words, user_msg, res


def transcript_parts(path, max_bytes=2_000_000, wait_for="", max_wait=1.0, stats=None):
    """(the agent's last text, the user's latest message, the previous tool result) from a Claude Code transcript (jsonl);
    '' for what is missing. The user's messages are user records that carry text and no tool_result (origin.kind ==
    "human" where the transcript records it); a tool result is a user record carrying a tool_result block."""
    # The agent's text before this call reaches the transcript file a moment before the call's own row, and the hook can
    # run before either (11-10: 1 of 3 probed acts read the previous text). With the call's tool_use_id, wait until its
    # row is in the file (seen at once in the probes), at most max_wait, so the text written just before it is there.
    t0 = time.time()
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2); n = f.tell(); f.seek(max(0, n - max_bytes)); raw = f.read()
        except Exception:
            return "", [], ""
        seen = bool(wait_for) and wait_for.encode() in raw[-300_000:]
        if not wait_for or seen or time.time() - t0 >= max_wait:
            break
        time.sleep(0.05)
    if stats is not None:   # per call, for the act log: how long the read waited for the call's row, and whether it appeared (11-11)
        stats.update(wait_ms=int((time.time() - t0) * 1000), id_seen=seen if wait_for else None)
    tail = raw.decode("utf-8", "replace")
    words = res = ""
    older = ""
    user_msgs = []  # the user's last two messages, newest first (an approval often sits one message back)
    for line in reversed(tail.splitlines()):
        if words and len(user_msgs) >= 2 and res:
            break
        if not res and '"tool_result"' in line:
            try: d = json.loads(line)
            except Exception: d = {}
            c = (d.get("message") or {}).get("content") if d.get("type") == "user" else None
            for b in (c if isinstance(c, list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    x = b.get("content")
                    res = (x if isinstance(x, str) else " ".join(y.get("text", "") for y in (x or []) if isinstance(y, dict))).strip() or "(empty)"
                    break
            if res:
                continue
        if not words and '"assistant"' in line:
            try: d = json.loads(line)
            except Exception: continue
            m = d.get("message") or {}
            if d.get("type") == "assistant" and isinstance(m.get("content"), list):
                words = " ".join(b.get("text", "") for b in m["content"] if isinstance(b, dict) and b.get("type") == "text").strip()
                if words: older = "prompt" if user_msgs else ("action" if res else "")   # what came between the words and this act
        elif len(user_msgs) < 2 and '"queue-operation"' in line:  # a message sent mid-turn is queued, not a user record
            try: d = json.loads(line)
            except Exception: continue
            if d.get("type") == "queue-operation" and d.get("operation") == "enqueue" and isinstance(d.get("content"), str):
                t = _TAGGED.sub(" ", d["content"]).strip()
                if t and t not in user_msgs:  # the same message is also recorded as a user record; either may be read first (identical text is collapsed; an empty one, e.g. a stripped task notification, is skipped)
                    user_msgs.append(t)
        elif len(user_msgs) < 2 and '"user"' in line:
            try: d = json.loads(line)
            except Exception: continue
            o = d.get("origin")
            if d.get("type") == "user" and not d.get("isMeta") and ((o or {}).get("kind") == "human" if o else True):
                c = (d.get("message") or {}).get("content")
                t = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                t = _TAGGED.sub(" ", t).strip()
                if t and t not in user_msgs:  # a queued message is also recorded as a user record; either may be read first
                    user_msgs.append(t)
    return words, user_msgs, res, older   # user_msgs: the user's last two prompts, newest first; older: the words precede the previous action


def last_words_from_transcript(path, max_bytes=2_000_000):
    """The last assistant text in a Claude Code transcript (jsonl). '' if unreadable. (Calibration uses this.)"""
    return transcript_context(path, max_bytes)[0]


def _verdict(out, usage, coding=True):
    if out.get("speak") and out.get("rule") in (RULE_IDS if coding else RULE_IDS_G) and out.get("line", "").strip():
        return {"rule": out["rule"], "line": out["line"].strip(), "certainty": out.get("certainty", "maybe"), "usage": usage or {}}
    return None


def judge_subscription(summary, timeout=8.0, usage_out=None, coding=True):
    """One `claude -p` call: no tools, no settings (so no hooks, and never this nudge recursively), no MCP, no saved
    session, run from the temp folder. ANTHROPIC_API_KEY is removed from the child's environment so the login (not a
    stray key) pays. Raises on failure or timeout (the caller fails open)."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run([CLAUDE_BIN, "-p", "--model", MODEL, "--effort", "low", "--system-prompt", SYSTEM if coding else SYSTEM_G, "--json-schema", json.dumps(SCHEMA),
                        "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                        "--output-format", "json"], input=summary, capture_output=True, text=True, timeout=timeout,
                       cwd=tempfile.gettempdir(), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}: {(r.stderr or r.stdout)[:200]}")
    d = json.loads(r.stdout)
    if usage_out is not None:
        usage_out.update(d.get("usage") or {}); usage_out["cost_usd"] = d.get("total_cost_usd")
    if d.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(d.get('result'))[:200]}")
    out = d.get("structured_output")
    if not isinstance(out, dict):
        out = json.loads(d.get("result") or "{}")
    return _verdict(out, d.get("usage"), coding)


LOCAL_URL = os.environ.get("NUDGE_LOCAL_URL", "")        # an OpenAI-compatible server, e.g. http://host:8000/v1
LOCAL_MODEL = os.environ.get("NUDGE_LOCAL_MODEL", "")    # its served model name
LOCAL_THINKING = os.environ.get("NUDGE_LOCAL_THINKING", "off")   # off | on


def judge_local(summary, timeout=8.0, usage_out=None, coding=True, url=None, model=None, thinking=None):
    """The same judgment on a local model through /chat/completions (sglang, vLLM, llama.cpp): the same system prompt,
    the act as the user turn, the verdict schema as response_format. Thinking is switched per request through the chat
    template (Qwen-style enable_thinking); any reasoning the server returns inline is cut before the JSON is read."""
    url, model = (url or LOCAL_URL).rstrip("/"), model or LOCAL_MODEL
    think = (thinking or LOCAL_THINKING) == "on"
    body = {"model": model, "max_tokens": 4096 if think else 400, "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM if coding else SYSTEM_G}, {"role": "user", "content": summary}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": think}}
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    u = d.get("usage") or {}
    if usage_out is not None:   # under the Messages API names, so the service's token log reads them
        usage_out.update(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                         cache_read_input_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    ch = (d.get("choices") or [{}])[0]
    if ch.get("finish_reason") == "length":   # cut off: any JSON in it is a draft from the reasoning, not a verdict
        raise RuntimeError("local judge cut off at max_tokens")
    text = (ch.get("message") or {}).get("content") or ""
    text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if "<think>" in text:
        raise RuntimeError("local judge reasoning never closed")
    out = json.loads(text[text.index("{"):text.rindex("}") + 1])
    return _verdict(out, d.get("usage"), coding)


DS_URL = os.environ.get("NUDGE_DS_URL", "https://api.deepseek.com/chat/completions")
DS_MODEL = os.environ.get("NUDGE_DS_MODEL", "deepseek-flash")   # DeepSeek-V4.1-Flash
DS_FORMAT = ('\n\nAnswer with one JSON object and nothing else: {"speak": true or false, "rule": "<rule id, or empty>", '
             '"line": "<the reminder, or empty>", "certainty": "sure" or "maybe"}.')


def judge_deepseek(summary, api_key, timeout=20.0, usage_out=None, coding=True):
    """The same judgment on DeepSeek's own API. DeepSeek offers JSON mode,
    not a schema, so the system prompt gets one closing line naming the verdict's fields; thinking is off."""
    body = {"model": DS_MODEL, "max_tokens": 1000, "temperature": 0,
            "messages": [{"role": "system", "content": (SYSTEM if coding else SYSTEM_G) + DS_FORMAT}, {"role": "user", "content": summary}],
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    req = urllib.request.Request(DS_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    u = d.get("usage") or {}
    if usage_out is not None:
        usage_out.update(u)
    c = (d.get("choices") or [{}])[0]
    if c.get("finish_reason") == "length":
        raise RuntimeError("deepseek judge cut off at max_tokens")
    return _verdict(json.loads((c.get("message") or {}).get("content") or ""), u, coding)


def judge(summary, api_key, timeout=8.0, usage_out=None, coding=True):
    """{"rule", "line", "usage"} when Haiku speaks, None when silent. Raises on transport errors (the caller fails open).
    NUDGE_BACKEND=subscription (default) goes through `claude -p`; api uses api_key. usage_out, a dict, receives the
    call's token usage whether or not Haiku speaks (for cost accounting)."""
    if BACKEND == "subscription":
        return judge_subscription(summary, timeout, usage_out, coding)
    if BACKEND == "local":
        return judge_local(summary, timeout, usage_out, coding)
    body = {"model": MODEL, "max_tokens": 1500, "system": [{"type": "text", "text": SYSTEM if coding else SYSTEM_G, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": summary}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = r.status
        d = json.loads(r.read())
    if usage_out is not None:
        usage_out.update(d.get("usage") or {})
    if status != 200 or d.get("stop_reason") == "refusal":
        return None
    out = json.loads(next(b["text"] for b in d["content"] if b.get("type") == "text"))
    return _verdict(out, d.get("usage"), coding)
