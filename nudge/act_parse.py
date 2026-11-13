#!/usr/bin/env python3
"""Plain-language summary of a Bash tool call for the nudge judge (the user 2026-11-10 16:50: "We need to make a parser
script to pre-process Bash calls. What we're passing in has way too much noise. The model needs to see that it's
calling python, and we need a way of summarizing what it does without including all the details.").

  describe_bash(command, description="", background=False, timeout_ms=None) -> str

One line per step of the command: what program runs, on what, with what effect. Inline Python (a heredoc or -c) is
summarized from its syntax tree: the files it reads, writes or deletes, the programs it runs, the URLs it fetches,
whether it loops. A heredoc written to a file is "writes FILE (N lines)". Risky steps are marked [!].
Deterministic: no model reads the command here; the judge reads this summary.
"""
import ast, os, re, shlex, shutil, warnings

RISKY = {"cp": "copies, overwriting the destination", "rsync": "syncs files", "rm": "deletes", "rmdir": "deletes", "kill": "stops processes", "pkill": "stops processes by name",
         "killall": "stops processes by name", "launchctl": "manages a launchd service", "systemctl": "manages a systemd service",
         "dd": "writes raw disk data", "mv": "moves or renames", "chmod": "changes permissions", "sudo": "runs as root"}
GIT_RISKY = {"push": "pushes to the remote", "reset": "resets the branch", "checkout": "switches or discards files",
             "restore": "discards file changes", "stash": "stashes uncommitted work", "clean": "deletes untracked files",
             "rebase": "rewrites history", "commit": "commits"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)", re.S)


def _short(s):
    """Paths the judge needs to recognise, not read: the home folder as ~, a session scratch folder as <scratchpad>."""
    s = re.sub(r"/(?:private/)?tmp/claude-\d+/[^/\s]+/[^/\s]+/scratchpad", "<scratchpad>", s or "")
    return re.sub(r"/Users/[^/\s]+|/home/[^/\s]+", "~", s)


def _clip(s, n=160):
    s = re.sub(r"\s+", " ", _short(s)).strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def describe_python(src):
    """What a Python script does, from its syntax tree: files read/written/deleted, programs run, URLs fetched."""
    lines = len([l for l in src.splitlines() if l.strip()])
    try:
        with warnings.catch_warnings():   # odd escapes in the agent's script are its business, not the service log's
            warnings.simplefilter("ignore")
            tree = ast.parse(src)
    except SyntaxError:
        return f"Python script ({lines} lines; could not be parsed): {_clip(src, 200)}"
    reads, writes, deletes, runs, urls, other = [], [], [], [], [], set()
    names = {}   # simple NAME = "string" (or expanduser("string")) assignments, so a path shows as the path, not "p"
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = n.value
            if isinstance(v, ast.Call) and ast.unparse(v.func).split(".")[-1] in ("expanduser", "Path", "PurePath") and v.args: v = v.args[0]
            if isinstance(v, ast.Constant) and isinstance(v.value, str): names[n.targets[0].id] = v.value
            elif isinstance(v, ast.JoinedStr):
                names[n.targets[0].id] = "".join(x.value if isinstance(x, ast.Constant) else
                                                 names.get(x.value.id, "{" + ast.unparse(x.value) + "}") if isinstance(x.value, ast.Name) else
                                                 "{" + ast.unparse(x.value) + "}" for x in ((y.value if isinstance(y, ast.FormattedValue) else y) and y for y in v.values))

    def lit(n):
        if n is None: return ""
        if isinstance(n, ast.Constant) and isinstance(n.value, str): return n.value
        if isinstance(n, ast.Name) and n.id in names: return names[n.id]
        if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in ("expanduser", "Path", "PurePath") and n.args: return lit(n.args[0])
        if isinstance(n, ast.JoinedStr):
            return "".join(x.value if isinstance(x, ast.Constant) else lit(x.value) if isinstance(x, ast.FormattedValue) and isinstance(x.value, ast.Name) and x.value.id in names
                           else "{" + ast.unparse(x.value if isinstance(x, ast.FormattedValue) else x) + "}" for x in n.values)
        return ast.unparse(n)
    for n in ast.walk(tree):
        if isinstance(n, (ast.For, ast.While)): other.add("loops")
        if not isinstance(n, ast.Call): continue
        f = ast.unparse(n.func)
        a0 = n.args[0] if n.args else None
        if f == "open":
            mode = lit(n.args[1]) if len(n.args) > 1 else next((lit(k.value) for k in n.keywords if k.arg == "mode"), "r")
            (writes if any(c in mode for c in "wax") else reads).append(lit(a0))
        elif f.endswith((".write_text", ".write_bytes")): writes.append(lit(n.func.value))
        elif f.endswith((".read_text", ".read_bytes")): reads.append(lit(n.func.value))
        elif f in ("os.remove", "os.unlink", "shutil.rmtree", "os.rmdir") or f.endswith(".unlink"): deletes.append(lit(a0) or ast.unparse(n.func))
        elif f in ("shutil.copy", "shutil.copy2", "shutil.move", "os.rename", "os.replace"):
            writes.append(lit(n.args[1]) if len(n.args) > 1 else f)
        elif f.startswith("subprocess.") or f in ("os.system", "os.popen"):
            cmd = a0
            if isinstance(cmd, (ast.List, ast.Tuple)) and cmd.elts:
                runs.append(" ".join(lit(e) for e in cmd.elts[:4]))
            else:
                runs.append(_clip(lit(cmd), 60))
        elif "urlopen" in f or f.startswith(("requests.", "httpx.")):
            urls.append(_clip(lit(a0), 80))
        elif f.endswith(("ThreadPoolExecutor", "Pool")): other.add("runs work in parallel")
        elif f == "print": other.add("prints results")
    flow = _rewrites(tree, lit)
    if flow:   # a write that keeps the file's old text is not an overwrite: say what it does instead
        kept = {f for f, _ in flow}
        writes = [w for w in writes if w not in kept]
        reads = [r for r in reads if r not in kept and not any(r == f for f in kept)]
        if len(kept) > 1: reads = []   # a reused path name makes the plain read list unreliable; the rewrite lines say it
    out = [f"Python script ({lines} line{'s' if lines != 1 else ''})"]
    for f, how in flow: out.append(f"rewrites {_clip(f, 80)} keeping its old contents ({how})")
    fmt = lambda xs: ", ".join(dict.fromkeys(_clip(x, 80) for x in xs if x))[:300]
    if reads: out.append("reads " + fmt(reads))
    if writes: out.append("[!] writes " + fmt(writes))
    if deletes: out.append("[!] deletes " + fmt(deletes))
    if runs: out.append("runs " + fmt(runs))
    if urls: out.append("calls the network: " + fmt(urls))
    if other: out.append(", ".join(sorted(other)))
    return "; ".join(out)


def _rewrites(tree, lit):
    """[(file, how)] for each file the script reads into a variable, changes, and writes back with its old text still in
    it: "adds text before the old contents", "replaces 2 passages". A variable rebuilt without its old value is not listed."""
    state, out, env = {}, [], {}   # env: NAME -> path string as the script runs, in order (a name can be reused)

    def path(n):
        if isinstance(n, ast.Name) and n.id in env: return env[n.id]
        if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in ("Path", "PurePath", "expanduser") and n.args: return path(n.args[0])
        return lit(n)

    def reads_of(v):   # open(P).read() / Path(P).read_text() / P.read_text()
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in ("read", "read_text"):
            o = v.func.value
            if isinstance(o, ast.Call) and ast.unparse(o.func) in ("open", "io.open") and o.args: return path(o.args[0])
            return path(o)
        return None

    def uses(node, name):
        return any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(node))

    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name, v = st.targets[0].id, st.value
            if (isinstance(v, ast.Constant) and isinstance(v.value, str)) or (isinstance(v, ast.Call) and ast.unparse(v.func).split(".")[-1] in ("Path", "PurePath", "expanduser")):
                env[name] = path(v); state.pop(name, None); continue
            src = reads_of(v)
            if src is not None:
                state[name] = {"file": src, "ops": []}; continue
            if name in state:
                if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add) and uses(v, name):
                    state[name]["ops"].append("adds text after the old contents" if isinstance(v.left, ast.Name) and v.left.id == name
                                              else "adds text before the old contents")
                elif isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "replace" and uses(v.func.value, name):
                    state[name]["ops"].append("replace")
                elif not uses(v, name):
                    state.pop(name)   # rebuilt from scratch: its old text is gone
        call = st.value if isinstance(st, ast.Expr) else None
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in ("write", "write_text") and call.args:
            a = call.args[0]
            var = a.id if isinstance(a, ast.Name) and a.id in state else next((x.id for x in ast.walk(a) if isinstance(x, ast.Name) and x.id in state), None)
            if var is None: continue
            if not isinstance(a, ast.Name):   # written as an expression around the old text: new + old, old + new
                state[var]["ops"].append("adds text after the old contents" if isinstance(a, ast.BinOp) and isinstance(a.left, ast.Name) and a.left.id == var
                                         else "adds text before the old contents" if isinstance(a, ast.BinOp) else "changes it")
            o = call.func.value
            dest = path(o.args[0]) if isinstance(o, ast.Call) and ast.unparse(o.func) in ("open", "io.open") and o.args else path(o)
            if dest == state[var]["file"]:
                ops = state[var]["ops"]
                n = ops.count("replace")
                how = [x for x in dict.fromkeys(ops) if x != "replace"] + ([f"replaces {n} passage{'s' if n > 1 else ''}"] if n else [])
                out.append((dest, "; ".join(how) or "unchanged"))
    return out


def _split_steps(cmd):
    """The command's steps, splitting on ; && || | and newlines outside quotes. Heredoc bodies are taken out first."""
    bodies = {}
    def take(m):
        k = f"__HEREDOC{len(bodies)}__"; bodies[k] = m.group(3); return f" {k}\n"
    cmd = HEREDOC.sub(take, cmd)
    steps, cur, q, i, depth = [], "", None, 0, 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and q != "'":                      # an escaped character is never a separator or a quote
            cur += cmd[i:i + 2]; i += 2; continue
        if q:
            cur += c
            if c == q: q = None
        elif c in "'\"":
            q = c; cur += c
        elif cmd[i:i + 2] == "$(":
            depth += 1; cur += "$("; i += 2; continue
        elif c == ")" and depth:
            depth -= 1; cur += c
        elif depth:
            cur += c
        elif c in ";\n" or cmd[i:i + 2] in ("&&", "||"):
            steps.append(cur); cur = ""; i += 1 if c in ";\n" else 2; continue
        elif c == "|":
            steps.append(cur + " |"); cur = ""
        else:
            cur += c
        i += 1
    steps.append(cur)
    return [s.strip() for s in steps if s.strip() and s.strip() != "|"], bodies


KEYWORDS = ("do", "then", "else", "{", "(")
ENDS = {"done", "fi", "esac", "}", ")", "do", "then", "else"}


def _step(s, bodies):
    piped = s.endswith(" |")
    s = s[:-2].strip() if piped else s
    s = re.sub(r"\s*\d?>&\d\b|\s*2>/dev/null", "", s).strip()   # stream plumbing is noise to the judge
    for k in KEYWORDS:
        if s.startswith(k + " "): s = s[len(k) + 1:].strip()
    if s in ENDS: return ""
    s = s.replace("\\$", "$")
    m = re.match(r"for\s+(\w+)\s+in\s+(.*)", s)
    if m: return f"for each {m.group(1)} in {_clip(m.group(2), 100)}:"
    m = re.match(r"(while|until|if)\s+(.*)", s)
    if m: return {"while": "repeat while", "until": "repeat until", "if": "if"}[m.group(1)] + f" {_clip(m.group(2), 100)}:"
    m = re.match(r"(\w+)=\$\((.*)\)$", s)
    if m:
        inner = "; ".join(x for x in (_step(t, bodies) for t in _split_steps(m.group(2))[0]) if x)
        return f"captures into {m.group(1)}: {inner}"
    hd = next((k for k in bodies if k in s), None)
    try:
        w = shlex.split(s.replace(hd, "") if hd else s, posix=True)
    except ValueError:
        w = s.split()
    while w and re.match(r"^\w+=", w[0]): w = w[1:]        # leading VAR=value
    if not w: return ""
    prog = w[0].split("/")[-1]
    args = w[1:]
    redir = re.search(r"(?:^|\s)(>>?|>\|)\s*(\S+)", s)
    tail = f", output {'appended' if redir and redir.group(1) == '>>' else 'written'} to {redir.group(2)}" if redir and redir.group(2) != "/dev/null" and not redir.group(2).startswith("&") else ""
    if prog in ("cd", "pushd"): return f"in {args[0] if args else '~'}"
    if re.match(r"python3?(\.\d+)?$", prog):
        code = bodies.get(hd) if hd else (args[args.index("-c") + 1] if "-c" in args and args.index("-c") + 1 < len(args) else None)
        if code is not None: return describe_python(code) + tail
        script = next((a for a in args if not a.startswith("-")), "")
        rest = " ".join(args[args.index(script) + 1:]) if script in args else ""
        return f"runs Python script {script}" + (f" with: {_clip(rest, 140)}" if rest else "") + tail
    if prog in ("bash", "sh", "zsh") and args and not args[0].startswith("-"):
        return f"runs shell script {args[0]}" + (f" {_clip(' '.join(args[1:]), 100)}" if args[1:] else "") + tail
    if prog in ("bash", "sh", "zsh") and "-c" in args:
        inner = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
        return "runs in a sub-shell: " + "; ".join(x for x in (_step(t, bodies) for t in _split_steps(inner)[0]) if x)
    if prog == "ssh":
        host = next((a for a in args if not a.startswith("-") and "=" not in a), "?")
        rest = args[args.index(host) + 1:] if host in args else []
        inner = " ".join(rest)
        sub = "; ".join(x for x in (_step(t, bodies) for t in _split_steps(inner)[0]) if x) if inner else "an interactive shell"
        return f"[!] on another machine ({host}): {sub}"
    if prog == "cat" and hd:
        dest = re.search(r">\s*(\S+)", s)
        return f"[!] writes {dest.group(1)} ({len(bodies[hd].splitlines())} lines)" if dest else f"prints a {len(bodies[hd].splitlines())}-line text"
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        mark = "[!] " if sub in GIT_RISKY else ""
        rest = [a for a in args if a != sub]
        if sub == "commit" and ("-m" in args or "--message" in args):
            i = args.index("-m") if "-m" in args else args.index("--message")
            msg = args[i + 1] if i + 1 < len(args) else ""
            return f"[!] git commit\n     commit message: {_clip(msg, 2000)}"
        return f"{mark}git {sub}" + (f" ({GIT_RISKY[sub]})" if sub in GIT_RISKY else "") + (f" {_clip(' '.join(rest), 100)}" if rest else "")
    if prog.endswith("git_safe_commit_push.sh"):
        paths = [a for a in args[1:] if not a.startswith("-")]
        return f"[!] commits and pushes {len(paths)} file(s): {_clip(' '.join(paths), 300)}\n     commit message: {_clip(args[0] if args else '', 2000)}"   # whole: the commit rules (C4, C6) read it
    if prog in RISKY:
        return f"[!] {prog} ({RISKY[prog]}): {_clip(' '.join(args), 140)}"
    if prog in ("curl", "wget"):
        url = next((a for a in args if a.startswith("http")), "")
        return f"calls the network: {url or _clip(' '.join(args), 100)}" + tail
    return f"{prog} {_clip(' '.join(args), 140)}".strip() + tail + (" (output piped on)" if piped else "")


BUILTINS = {"cd", "echo", "export", "local", "read", "return", "set", "shift", "source", ".", "test", "[", "[[", "true", "false",
            "unset", "wait", "eval", "exec", "exit", "printf", "pwd", "type", "ulimit", "umask", "alias", "break", "continue",
            "command", "builtin", "for", "while", "until", "if", "case", "time", "trap", "let", "declare", "pushd", "popd"}


def _facts(steps_text, cwd):
    """Checked on this machine, for steps that run here (not inside ssh): programs that are not installed, and for each
    file a step overwrites, whether it exists now and its size."""
    missing, sizes, cur = [], [], os.path.expanduser(cwd or os.getcwd())
    defined = set(re.findall(r"(?m)^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{", "\n".join(r for _, r in steps_text)))   # shell functions the command defines
    for desc, raw in steps_text:
        if raw.startswith(("cd ", "pushd ")):
            d = os.path.expanduser(raw.split(None, 1)[1].strip().strip("'\"")) if len(raw.split()) > 1 else os.path.expanduser("~")
            cur = d if os.path.isabs(d) else os.path.normpath(os.path.join(cur, d))
            continue
        if desc.startswith("[!] on another machine"): continue
        try: w = shlex.split(raw)
        except ValueError: w = raw.split()
        while w and (re.match(r"^\w+=", w[0]) or w[0] in ("do", "then", "else", "{", "(", "!", "sudo", "env", "nohup", "nice")): w = w[1:]
        if w and w[0] not in ("done", "fi", "esac", "}", ")"):
            prog = w[0]
            if prog not in BUILTINS and prog not in defined and not prog.startswith(("$", "__HEREDOC", "-")) and re.match(r"^[\w./~+-]+$", prog):
                local = "/" in prog or prog.startswith("~")   # a path, absolute or relative to where the command is: not looked up on PATH
                ok = os.path.exists(os.path.join(cur, os.path.expanduser(prog))) if local else shutil.which(prog) is not None
                if not ok: missing.append(prog)
        for m in re.finditer(r"(?:\[!\] writes |output written to )([^\s;,()]+)", desc):
            f = m.group(1)
            if f.startswith(("<", "{")): continue
            full = os.path.join(cur, os.path.expanduser(f))
            sizes.append(f"{f} exists ({os.path.getsize(full):,} bytes)" if os.path.isfile(full) else f"{f} is a new file")
    out = []
    if missing: out.append("[!] not installed on this machine: " + ", ".join(dict.fromkeys(missing)))
    if sizes: out.append("files it overwrites, now: " + "; ".join(dict.fromkeys(sizes)))
    return out


def describe_bash(command, description="", background=False, timeout_ms=None, cwd=None):
    steps, bodies = _split_steps(command or "")
    lines, env, pairs = [], {}, []
    for st in steps:
        if re.match(r"^\w+=\$\(\(", st.strip()): continue          # n=$((n+1)): a loop counter, not a step
        m = re.match(r"^(\w+)=(\S+)$", st.strip())
        if m and not m.group(2).startswith("$("):      # VAR=value: remembered; a path-like value is shown where it is used
            if "/" in m.group(2): env[m.group(1)] = m.group(2).strip("'\"")
            continue
        for k, v in env.items(): st = re.sub(r"\$\{?" + k + r"\b\}?", v, st)
        x = _step(st, bodies)
        if x: lines.append(x)
        pairs.append((x or "", st.strip()))
    head = f"Run a shell command{' in the background' if background else ''}"
    if timeout_ms: head += f" (time limit {round(timeout_ms / 60000, 1):g} min)"
    if description: head += f'. Its stated purpose: "{_clip(description, 160)}"'
    facts = _facts(pairs, cwd)
    return head + ":\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(lines, 1)) + \
        ("\nChecked on this machine: " + " | ".join(facts) if facts else "\nChecked on this machine: every program it runs is installed.")
