# Bugs and fixes

Every wrong reminder is investigated at once and fixed at its cause. This is the log, newest last. "Checked" means
the wrong act was replayed through the judge (silent after the fix) along with a real slip of the same kind (still
fires).

- **2026-11-09, a late verdict was spent unseen.** A verdict that arrived after the hook stopped waiting still
  recorded a pause, so the next identical call was waved through. Fix: a 5-second budget; a late verdict is dropped
  unrecorded.
- **2026-11-09, the judge missed the end of the agent's words.** The agent's last 1,200 characters were clipped to
  their first 700, cutting exactly what it said right before acting. Fix: keep the end. The user's latest message was
  added in the same change. The user: "It can't nudge you if it doesn't see what you're doing."
- **2026-11-09, missing-program warnings on the wrong machine.** It warned that `seq` was missing on the Mac and
  `timeout` on the Linux box; both are installed. Fix: the act summary names the machine.
- **2026-11-09, a background launch at the end of a long command was unseen.** The command kept only its head.
  Fix: keep both ends.
- **2026-11-09, it judged the conversation's topic, not the act.** After a conversation about long audio renders it
  flagged quick file edits as long jobs; it flagged claims the previous act had already checked. Fix: the judge sees
  the previous result and is told a rule applies only to the kind of act it describes; short inline scripts that edit
  local files are named as seconds. Checked.
- **2026-11-09, the fires log cut the act off** at 600 characters once context came first. Fix: the act leads the
  summary, and the log keeps 4,000 characters.
- **2026-11-09, three misreadings.** It read a key's variable name as a leaked key; it read one agent's routine pull in
  its own clone as a risky stash in a shared one; it read the Bash tool's own time limit, shown as "(timeout N ms)",
  as the GNU `timeout` command. Fixes: a key-shaped string is the secret itself; the machine line says whose clone it
  is; the time limit is labelled as the tool's own. Checked.
- **2026-11-09, a reminder we called wrong was right.** The restart rule fired on a restart another agent had asked
  for. The user: "Just because [another agent] asked for the restart doesn't mean nobody else in the [household] might
  be affected." The
  rule now fires on every restart or stop of a running service, whoever asked.
- **2026-11-10, a pause for an overwrite that wasn't one.** A 2,859-character command added a changelog entry in front
  of the existing ones, but the 700-character window showed only both ends, and the part that kept the history sat
  at character 1,206. The mistake was the judge's: the agent's command was right. Fix: the command window is 3,000. That act: paused 2 of 2 at 700, silent 2 of 2 at 3,000.
  **Weak spot, not fixed:** a plain overwrite written inside a script was caught 1 of 3 times at 3,000; the
  overwrite rule is unreliable on overwrites buried in code.
- **2026-11-10, a reminder to wait for approval the user had already given.** A message the user sends while the
  agent is mid-turn is recorded in the transcript as a `queue-operation` (enqueue) entry, not as a user message, so
  the judge saw the user's previous message instead. Fix: the reader takes the newest of the two.
- **2026-11-10, a reminder to ask first about a move the user had approved one message earlier.** The approval was
  followed by a quick aside, and the judge saw only the latest message. Fix: it sees the user's last two. Replay:
  with both, silent 2 of 2; latest only, fired 2 of 2; no approval in either, still fired 2 of 2.
- **2026-11-10, a reminder that came too late to matter.** The review rule fired as a note on a commit of shared code
  that no one else had reviewed. A note arrives after the act, so the commit went out, and the review that followed
  found two real gaps. Fix: the rule's trigger says to pause ("sure") when neither the commit message nor the agent's
  last words name an independent review. Replay: the two recent unreviewed commits paused 6 of 6 (were notes); a
  commit that named its review never paused.
- **2026-11-10, the stricter review rule paused a one-line settings change.** Nothing there for a reviewer to test.
  Fix: the rule skips a commit whose files are not code (docs, rule text, an agent or skill definition, a setting),
  even when a script made the edit. Replay: that act silent 4 of 4; the real unreviewed code commits still paused 7
  of 8.
- **Known gap:** a reminder's record waits in memory for the session's next act, so restarting the service in
  between loses that record from `nudge_fires.jsonl` (`nudge_calls.jsonl` still has the call).
