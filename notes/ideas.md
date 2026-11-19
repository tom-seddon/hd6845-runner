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
- [ ] **Waiting for reviewers.** After the changes of 2026-11-19 14:25 (Made, below): a
  headless run's reviewer runs to the end, its report reaches the run (in the same turn, or
  in a new turn after the run ended its turn to wait), and the run goes on to the merge gate.
  Look in `var/runs/NNN.log` for the reviewer's `[agent]` lines ending, then the gate. If the
  run ends with the reviewer's report never taken in, the background wait is not enough.
- [ ] **No polling.** The run does not wait for a reviewer in a loop of Bash commands
  (`until ... sleep`), and stops any background command it started before its final report.
- [ ] **New checks quiet when they should be.** `--check` and each run's start do not stop
  for this conversation or an idle app session (`others: none tracing`).

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

### Made 2026-11-19 14:25 +0000 (agreed 13:56, revised after the resumed run 1)

Agreed with the user at 13:56, looked at again once the resumed run 1 had handed off
(`eof` piece `ff3` on main at `1cefdb8`, hand-off `fe19d36`, claim kept), and made at 14:25.
The run added 6 and 7 and changed 1 and 4; each item says what was made.

1. **Replace the "reviewers in the foreground" sentence** of `UNATTENDED_PROMPT` (part of the
   uncommitted reviewer fix). In this version of Claude Code reviewer agents always run in the
   background: both of run 1's reviewers started as "Async agent launched successfully", the
   second (13:28) although the agent asked for `run_in_background: false`. Asking for the
   foreground cannot be done and led the resumed session into polling loops (Bash waits of 10
   minutes for an `end_turn` marker that a hand-back does not write; the report arrived only
   when a wait timed out). The resumed run confirmed it: the polling began because the
   message it was given asked for the reviewer "in the foreground". **Made:** reviewers run in
   the background and each report is delivered as a message when they finish; do not poll
   their files or wait in a loop; if there is nothing else to do, end the turn, and the run
   stays open until the report arrives; stop any background commands before the final
   report. How a headless run takes the report after its turn has ended is the "Waiting for
   reviewers" check above.
2. **Lost reviewers in `RESUME_PROMPT`.** Add: a reviewer stopped before it reported means its
   round did not happen; run that round again with a new reviewer, so that every piece has its
   own review recorded. (The merge gate checks only the last "Round N:" line, which can be an
   earlier piece's clean round: see "Outside the runner".) **Made** as worded.
3. **`--reset`**: forget the saved session and the counters (`no_progress`, `resumes`,
   `blocked`), instead of editing `var/state.json` by hand; refuse while a runner is going
   (the lock). **Made**; the run number and the cost total are kept, so run logs are not
   overwritten. A `RESET` line goes in the log.
4. **Refuse to start while another Claude Code session works in the trace repo.** First
   planned as any `claude` process whose working folder is the main checkout or one of the
   `hd6845-trace-*` working copies. Two sessions on one package in one working copy is the one
   real risk found so far (the interactive resume of run 1, 2026-11-19 13:26). **Changed:**
   that test would always refuse, since idle desktop sessions and this conversation all work
   in the main checkout (`claude agents --json`, 14:13: three sessions there). **Made:**
   before each run, from `claude agents --json`, a session counts only if it is busy and
   either works in a package's working copy, or works in the main checkout with a tracing call
   (the tracing wrapper, a ship, an edit of a package's files, in its own history or its
   reviewers') in the last 15 minutes. If the sessions cannot be listed, one `WARNING` line,
   and no check.
5. **A continued session, and auto-mode refusals.** (a) When a runner session is resumed
   elsewhere, its file ends with a `continued-in` entry naming the new ID (run 1:
   `a23411a2-...` continued in `49d3c249-...`). Before resuming a saved session, look for it
   and stop with a message, rather than resume the old ID and fork the history. (b) Tool
   results that say "denied by the Claude Code auto mode classifier" (a reviewer's read of the
   notes, 13:44, "Undermining Oversight"): a line in the run log for each and a count in the
   `DONE` line, since nobody can approve them in a headless run. **Made**: (a) checked by
   `--check`, at start and before each resume; (b) `REFUSED by auto mode (reason), after:
   <the tool call before it>` in the run log, and the count in the run's summary line.
6. **A finite background wait** (from the resumed run 1). Its polling loops, moved to the
   background when they timed out, kept running until it killed them by hand at 13:56. With
   the wait at 0 (for ever), a stray background command would hold a finished headless run
   open until the idle timeout, which would then kill and resume it for nothing. **Made:** the
   wait is set to the idle timeout (90 minutes by default; reviews so far took 10-17), and the
   unattended wording asks for background commands to be stopped before the final report.
7. **Keep every result in the report** (from the resumed run 1). After its final report, a
   notification from one of the killed loops started one more short turn ("That notification
   is ... nothing new came of it"). In a headless run that is a second result, and the report
   was written from the last result only, so the real report would have been replaced by the
   one-liner. **Made:** `var/runs/NNN-report.md` gets every result's text, in order, each with
   a heading comment (time, subtype).

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
  session, so this never showed there. First fixed with the wait at 0 and the wording asking
  for reviewers in the foreground; both revised at 14:25 (Made, items 1 and 6). The runner was
  interrupted in run 2 to apply the fix. Run 1's package was then finished by resuming run 1's
  session interactively (13:26-13:56; review round 7 of `ff3`, merged `1cefdb8`, hand-off
  `fe19d36`, claim kept); run 2's session was not resumed, and the saved state is cleared
  with `--reset`.
- **Quitting an interactive session leaves it running** (2026-11-19 14:25 +0000). `/exit` in
  the resumed run 1 (14:19) only detached the terminal: the session went on as an idle
  background session (`claude agents --json`: kind `background`, state `done`), about 560 MB
  of memory and no quota. `claude stop <id>` ends it (done 14:21); its history stays.
