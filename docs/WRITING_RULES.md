# Writing the rules

## How we got ours

1. **Harvest.** From 999 of our past work sessions we collected 16,584 written lessons, each tied to a real mistake.
2. **Rewrite.** The lessons became nearly 9,000 one-line reminders.
3. **Split by reach.** 7,743 applied to one project or one tool. Those went into that project's reference files,
   not onto the list every agent carries. 1,246 were broad.
4. **Cluster.** 31 rules covered 1,002 of the broad reminders; 18 more covered 176; 68 were left over (fewer than five
   alike).
5. **Edit with the human.** The user edited the list through nine versions in one day, asking for broad rules, as few as
   possible. Rules for writing code moved to a separate list that only the coding agents carry. Today: 42 general and
   6 coding rules.
6. **Add rules from single observations.** A rule doesn't have to wait for a pile of mistakes. Rule C6 (update the
   project spec in the same commit) came from one remark and was live about fifteen minutes later.

## How to write one

Each rule is one line: the situation first, then the action, in positive words. Then a `[Fires when: ...]` line
naming the one moment it applies. That second line matters as much as the rule.

```
G29. Before restarting a service, check nobody is using it right now.  [Fires when: ACT: a restart or stop
(systemctl restart/stop, launchctl kickstart -k or bootout, docker restart) of a running service. Not loading a new job.]
```

Choose the moment in this order:

1. **CODE**: if a tool can enforce the rule, enforce it there and keep it off the list for the judge. Nothing to
   remember is better than a reminder.
2. **ACT** (the command or tool call itself) or **RESULT** (what came back). These are the judge's best cases.
3. **SAID**: what the agent is about to say or send. Use this only when the act can't show it.

Write exclusions into the trigger when a rule fires on look-alikes ("Not a background launch", "Not loading a new
job"). Most of our fixes were exclusions or added context, not new rules.

A rule names a situation the agent can recognise and one concrete check. "Be careful" is not a rule.

After editing `nudge/rules.md`, restart the service: it reads the rules once at start.

## The example

`nudge/rules.md` ships with eleven generic rules in this form, drawn from the general rules in our own list (ours
runs to 48 and is full of our own setup, so it is not published). Read them as examples of the form, then write your
own from your own agents' mistakes.
