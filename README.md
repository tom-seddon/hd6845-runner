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
./runner.py              # start; prints the `tail -f` command for its log
```

`./runner.py --help` lists the options: the first package (`--package`), what to do at the
usage limit (`--on-quota wait|stop`), how often to check the quota while blocked, the
effort level of the runs (`--effort`, default `high`), the permission mode of the runs
(nobody is there to approve anything; default `auto`), macOS notifications (`--notify`).
The effort level and permission mode are passed on every run, new or resumed, so that they
do not depend on Claude Code's defaults.

## Following it

| File | What |
|---|---|
| `var/runner.log` | one line per event: `START`, `RUN`, `DONE`, `MERGED`, `NOMERGE`, `ENDED`, `BLOCKED`, `RESUME`, `STOPPED` (`tail -f`) |
| `var/status` | one line: what it is doing now |
| `var/runs/NNN.log` | what run NNN did, one line per tool call or message |
| `var/runs/NNN-report.md` | the run's final report |

The sessions themselves are ordinary Claude Code sessions: `claude --resume` in the trace
repository lists them.

## Steering it

- `touch STOP`: stop gracefully, between runs or while waiting for quota (a run in progress
  finishes first). Ctrl-C stops at once; the next start resumes the interrupted session.
- `touch GO`: while waiting for quota, check now instead of at the next interval.

## What each run is told

Every run, new or resumed, gets a short addition to its system prompt
(`UNATTENDED_PROMPT` in `runner.py`): it is one run of an unattended loop, so it asks
nothing and makes the skill's open choices by the skill's own rules; it does one round of
one package and ends its turn after the step 9 report; at the hand-off it keeps the claim
(`--continue`) while the package has work left and releases it when the package is finished.
It overrides none of the skill's rules.

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
- **A run that hangs.** No output for 90 minutes (`--idle-timeout`): stopped and resumed, at
  most twice per session.
- **Two runners at once.** A lock file; the trace workflow is one package at a time.
- **Sleep.** The Mac is kept awake (`caffeinate`) while the runner is going.
