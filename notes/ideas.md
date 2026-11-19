# Ideas and checks

Changes to the runner that are wanted but not made yet, and behaviour still to be seen in
a real run. Newest at the bottom of each list. Dates from `date`.

## Waiting on checks

Things that only a real run shows. Make the changes below only once these have been seen.

- [ ] **Usage limit.** A run stopped by the limit logs `BLOCKED` with the limit message
  (first expected about 55 minutes into run 1, which started 2026-11-19 12:46 +0000). If
  the run stops while `/usage` shows Fable at 100% and no `BLOCKED` line appears, the
  wording was not recognised: look at the end of `var/runs/NNN.log` and `NNN.stderr` and
  widen `LIMIT_RE`.
- [ ] **Resume after the quota is back.** After the one-off quota reset, `touch GO`: the log
  should show `RESUME`, then `RUN ... resuming session ...`, and the run carry on in the same
  session.
- [ ] **Orderly stop.** `touch STOP` while a run is going: the run finishes, then `STOPPED
  stop file`. A later start begins a new run (the stopped one had finished).
- [ ] **End of a run.** After the step 9 report the run ends its turn without asking
  anything (`UNATTENDED_PROMPT`); the log shows `DONE`, then `MERGED` or `NOMERGE`.
- [ ] **Reviewer activity.** Whether the reviewer agents' tool calls come through in the
  run's output (lines marked `[agent]` in `var/runs/NNN.log`), or the log goes quiet while
  a reviewer works. If it goes quiet, check `--idle-timeout` (90 min) is long enough for a
  full review.

## Ideas

- **`var/current.log`** (2026-11-19 12:56 +0000). A symlink to the current run's
  `var/runs/NNN.log`, updated when a run starts, so that `tail -F var/current.log` follows the
  main session's activity across runs (`tail -f` on `NNN.log` stops at the end of a run).
  Wanted; waiting until the quota-reset and orderly-stop checks above have been done.
- **Idle timer from the session's files** (2026-11-19 13:04 +0000). The idle timeout
  (`--idle-timeout`, 90 min) restarts only on output from `claude -p`. If reviewer agents'
  steps do not come through in that output (the "Reviewer activity" check above), a review
  longer than 90 minutes counts as silence: the run is stopped, the reviewer's work is lost,
  and the session is resumed, up to twice, after which the runner gives up on the session;
  in the worst case three reviewers' worth of quota is wasted. Fix: also count activity in
  the session's history files, the main
  `~/.claude/projects/<repo path with / as ->/<session-id>.jsonl` and the reviewers' files
  under `<session-id>/subagents/`, by their sizes or modification times, checked every few
  seconds. A working reviewer then always keeps the timer going, whatever the output shows.
