# Calibration before going live

Question: would it speak rarely enough to be heard, and would what it said be worth hearing?

## Method

- 300 real tool calls from past work, drawn at random (seed 1109): 150 from each of the two agents it would watch.
  Each act was paired with the agent's last words before it, from the session archive.
- Plus 3 known slips from that day, to see whether the judge caught them.
- Each act went through the same `judge()` the live service uses.
- A larger model (Sonnet) read every reminder blind, with the act and the agent's last words, and rated it:
  HELPFUL (points at a real risk in this act the agent might have missed), NEUTRAL (harmless but not needed),
  or DISTRACTING (doesn't fit the act, or would push the agent to do something wrong or wasteful).
  The rating is a model's judgment, not a script's: whether a reminder fits an act is a question of meaning.

## Results

- Version 1 spoke too often, and was rejected.
- Version 2 added the sure/maybe split and a stricter prompt. On the 300 random acts: 34 reminders (11.3%), 1 "sure".
  Sonnet rated 29 helpful, 5 neutral, 0 distracting. Errors: 0.
- Known slips: 2 of 3 drew a reminder, but only 1 from the rule meant for it.
- Median judgment about 1.1 seconds in this run.

## What we learned

- Haiku's certainty varies between runs on the same act: the same slip can come back "sure" once and "maybe" the
  next. The design tolerates it, since "maybe" still delivers the reminder.
- Small fixes after going live (a bug, a missing piece of context) were checked on the act that went wrong and on one
  real slip, not by re-running the whole calibration. The user: "For this type of fix, I wouldn't bother doing more
  metrics. You're fixing a bug and adding a little more information, that's not enough to warrant another test."
