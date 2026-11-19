# hd6845-runner

Runs `/trace6845` (the package workflow of the HD6845S die trace) back to back, one Claude
Code session per run, on Claude Fable 5.2 only, until told to stop. It waits out the usage
limit and resumes the interrupted session when the quota is back.

One file, `runner.py`: Python 3.9 or later, standard library only. It expects the trace
repository's main checkout beside this folder (`../hd6845-trace`; `--repo` to change) and
the `claude` command on the PATH.

## Use

```
./runner.py --check      # check the setup and show what would run; runs nothing
./runner.py              # start; prints the `tail` commands for its log and the current run
./runner.py --reset      # forget the saved session and counters; runs nothing
```

`./runner.py --help` lists the options: the first package (`--package`), what to do at the
usage limit (`--on-quota wait|stop`), how often to check the quota while blocked, the
effort level of the runs (`--effort`, default `high`), the permission mode of the runs
(nobody is there to approve anything; default `auto`), macOS notifications (`--notify`).
The effort level and permission mode are passed on every run, new or resumed, so that they
do not depend on Claude Code's defaults.

`--reset` is for after a run has been finished or looked at by hand: the next start then
begins a new run instead of resuming the saved session.

## Following it

| File | What |
|---|---|
| `var/runner.log` | one line per event: `START`, `RUN`, `DONE`, `MERGED`, `NOMERGE`, `ENDED`, `BLOCKED`, `RESUME`, `STOPPED`, `RESET`, `WARNING` (`tail -f`) |
| `var/status` | one line: what it is doing now |
| `var/runs/NNN.log` | what run NNN did, one line per tool call or message, and each auto-mode refusal |
| `var/current.log` | a link to the current run's log: `tail -F var/current.log` follows it from one run to the next |
| `var/runs/NNN-report.md` | the run's reports: the text of every result, numbered in order (a headless run sends them all when it ends) |

A run's cost in the log is Claude Code's own figure. A run that ends without a result
(interrupted, stalled, a model mismatch) shows an estimate instead, from the usage of the
responses seen on the stream at Fable's API prices, marked as such.

The sessions themselves are ordinary Claude Code sessions: `claude --resume` in the trace
repository lists them.

## Steering it

- `touch STOP`: stop gracefully, between runs or while waiting for quota (a run in progress
  finishes first). Ctrl-C stops at once; the next start resumes the interrupted session.
- `touch GO`: while waiting for quota, check now instead of at the next interval.

## What each run is told

Every run, new or resumed, gets a short addition to its system prompt
(`UNATTENDED_PROMPT` in `runner.py`): it is one run of an unattended loop, so it asks
nothing and makes the skill's open choices by the skill's own rules; reviewer agents run in
the background and their reports are delivered to it, so it does not poll them and may end
its turn while one works; it stops any background commands it started before its final
report; it does one round of one package and ends its turn after the step 9 report; at the
hand-off it keeps the claim (`--continue`) while the package has work left and releases it
when the package is finished. A resumed run is also told that a reviewer stopped before it
reported means that round did not happen. None of this overrides the skill's rules.

## What it guards against

- **Another model.** Every run is started with `--model claude-fable-5-2` and no fallback
  model. If any response in a run comes from another model, the run is stopped at once, the
  session is not resumed, and the runner stops: the trace's merge gate fails a package that
  another model worked on.
- **Waiting at the usage limit.** A run stopped by the limit is resumed (same session,
  context intact) once a tiny check request succeeds; the checks run outside the repository
  and are not saved.
- **A stuck package.** A run counts as progress when a `Handoff` commit of yours reaches main.
  Two finished runs in a row without one stop the runner.
- **Reviewers cut off.** Headless Claude Code stops background tasks 10 minutes after a turn
  ends, and reviewer agents always run in the background. Each run gets
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` set to the idle timeout (90 minutes by default):
  long enough for a review, and finite, so that a stray background command cannot hold a
  finished run open.
- **A run that hangs.** No activity for 90 minutes (`--idle-timeout`): stopped and resumed, at
  most twice per session. Activity is output on the stream or growth of the session's history
  files (its own and its reviewers'), so a working reviewer keeps the timer going even if its
  steps did not come through on the stream.
- **Another session tracing.** Before each run, `claude agents --json` lists the Claude Code
  sessions on this Mac. The runner stops if one of them is busy in a package's working copy,
  or busy in the main checkout with a tracing call (the tracing wrapper, a ship, an edit of a
  package's files) in the last 15 minutes. Idle sessions, and conversations about the trace
  that do not trace, do not count.
- **A session resumed elsewhere.** A saved session that was continued outside the runner
  (its history ends with a `continued-in` entry) is not resumed again: the runner stops and
  says so; `--reset` after looking at it.
- **Refusals nobody can approve.** Each auto-mode refusal is a line in the run's log, and the
  run's summary line counts them.
- **Two runners at once.** A lock file; the trace workflow is one package at a time.
- **Sleep.** The Mac is kept awake (`caffeinate`) while the runner is going.
