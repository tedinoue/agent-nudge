# Nudge rules: a short starting example. Replace it with your own agents' mistakes (docs/WRITING_RULES.md).
# G = general (every agent); C = only agents that build software (NUDGE_CODING_SEATS). Each rule: one line, then the moment it fires.

G1. Before telling the user something is ready, use it the way they will: run the real command, open the real page, play the real file.  [Fires when: SAID: a reply to the user says ready, fixed, live, working or done.]
G2. When you start anything that runs more than a few minutes, run it in the background so the session stays free, and check its first output within a minute.  [Fires when: ACT: a long-running command started in the foreground.]
G3. Before restarting or stopping a running service, check who else is using it and tell the user first.  [Fires when: ACT: a restart, stop or kill of a running service or process.]
G4. Before deleting, overwriting or bulk-changing data, make a backup and confirm that what you remove is what was meant.  [Fires when: ACT: rm, an overwrite of an existing file, a bulk database change, a force push.]
G5. Keep keys, tokens and passwords out of anything you print, type into a command or save.  [Fires when: ACT: a key- or password-shaped string in a command, a file being written, or a message.]
G6. Before stating a number, a result or a cause, check it at its source in this session.  [Fires when: SAID: a reply gives a count, a measurement, a test result or a cause.]
G7. When the user asks for something, do exactly that and stop. Mention anything else in one line and wait for their go.  [Fires when: ACT: work that goes beyond what the user's last message asked for.]
G8. When the user is only asking a question or thinking out loud, answer it; start building only after they agree.  [Fires when: ACT: creating files or running a build right after a message from the user that asks or wonders rather than asks for work.]
C1. Before committing or pushing code, run the changed path at least once and read the result.  [Fires when: ACT: git commit or git push of code changed in this session.]
C2. After fixing a defect, search the code for the same pattern and fix every copy.  [Fires when: ACT: a commit that fixes a bug.]
C3. When code can't do its real job, make it fail loudly instead of returning a value that looks like success.  [Fires when: ACT: writing an error handler, fallback or default return value.]
