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
- [x] **Reviewer activity.** Whether the reviewer agents' tool calls come through in the
  run's output (lines marked `[agent]` in `var/runs/NNN.log`), or the log goes quiet while
  a reviewer works. If it goes quiet, check `--idle-timeout` (90 min) is long enough for a
  full review. **Seen 2026-11-19 13:18 +0000: they come through** (run 1's log has the
  reviewer's steps as `[agent]` lines, 13:05-13:15).
- [ ] **Waiting for reviewers.** After the fix of 2026-11-19 (Findings, below): a run's
  reviewer runs to the end, and the run goes on to the merge gate in the same turn.

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
  Less pressing since reviewer steps turned out to come through in the output (checks,
  above); still a safeguard if that ever changes.
- **Cost of an interrupted run** (2026-11-19 13:18 +0000). A run stopped by Ctrl-C, the idle
  timeout or a model mismatch ends without a result message, so the log shows its cost as
  $0.00 and the running total is too low (run 2: 2 minutes of work shown as $0.00). Sum the
  usage of the run's own responses from the stream instead, or note "cost unknown".

### Agreed, waiting for the resumed run 1 to finish (2026-11-19 13:56 +0000)

Agreed with the user; not made yet. Wait until the resumed run 1 has handed off, then look at
these again in the light of anything that run shows. 1-3 are needed before the runner next
starts; 4 and 5 are inexpensive safeguards.

1. **Replace the "reviewers in the foreground" sentence** of `UNATTENDED_PROMPT` (part of the
   uncommitted reviewer fix). In this version of Claude Code reviewer agents always run in the
   background: both of run 1's reviewers started as "Async agent launched successfully", the
   second (13:28) although the agent asked for `run_in_background: false`. Asking for the
   foreground cannot be done and led the resumed session into polling loops (Bash waits of 10
   minutes for an `end_turn` marker that a hand-back does not write; the report arrived only
   when a wait timed out). New wording: reviewers run in the background and their report is
   delivered as a message when they finish; do not poll their files; ending the turn while
   waiting is fine, since `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` keeps the run open until
   they report. How a headless run takes the report after its turn has ended is the "Waiting
   for reviewers" check above.
2. **Lost reviewers in `RESUME_PROMPT`.** Add: a reviewer stopped before it reported means its
   round did not happen; run that round again with a new reviewer, so that every piece has its
   own review recorded. (The merge gate checks only the last "Round N:" line, which can be an
   earlier piece's clean round: see "Outside the runner".)
3. **`--reset`**: forget the saved session and the counters (`no_progress`, `resumes`,
   `blocked`), instead of editing `var/state.json` by hand; refuse while a runner is going
   (the lock).
4. **Refuse to start while another Claude Code session works in the trace repo**: any `claude`
   process whose working folder is the main checkout or one of the `hd6845-trace-*` working
   copies (`lsof -a -c claude -d cwd`). Two sessions on one package in one working copy is the
   one real risk found so far (the interactive resume of run 1, 2026-11-19 13:26).
5. **A continued session, and auto-mode refusals.** (a) When a runner session is resumed
   elsewhere, its file ends with a `continued-in` entry naming the new ID (run 1:
   `a23411a2-...` continued in `49d3c249-...`). Before resuming a saved session, look for it
   and stop with a message, rather than resume the old ID and fork the history. (b) Tool
   results that say "denied by the Claude Code auto mode classifier" (a reviewer's read of the
   notes, 13:44, "Undermining Oversight"): a line in the run log for each and a count in the
   `DONE` line, since nobody can approve them in a headless run.

## Outside the runner

- **The merge gate checks only the last review round** (2026-11-19 13:56 +0000; for the
  trace's coordinator, `tools/mergecheck.py`). It passes when the last "Round N:" line of the
  notes' Review section is clean, so a piece shipped after an earlier piece's clean round can
  be merged without a review of its own: `eof`'s `ff3` (run 1, 2026-11-19) had no round while
  round 6, for `g7700`, was the last line. Worth raising with the coordinator.

## Findings

- **Background reviewers are stopped after 10 minutes** (2026-11-19 13:18 +0000). Run 1
  shipped piece `ff3` of `eof`, started its reviewer in the background and ended its turn to
  wait for it. Headless Claude Code waits only 10 minutes after a turn ends, then stops
  background tasks (`var/runs/001.stderr`: "Background tasks still running 10m after the last
  turn (subagent "Review eof piece ff3"); stopping them. Set
  CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."). The run ended as a success
  with nothing merged (`NOMERGE`). In the desktop app a finished background agent wakes the
  session, so this never showed there. Fixed two ways: each run gets
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` (`RUN_ENV`), and `UNATTENDED_PROMPT` asks for
  reviewer agents in the foreground and for no turn to end while waiting. Not yet seen in a
  run (checks, above). The runner was interrupted in run 2 to apply this; run 2's session
  is marked interrupted and is resumed on the next start.
