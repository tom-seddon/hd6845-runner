# hd6845-runner

Runs `/trace6845` of the HD6845S die trace (`../hd6845-trace`) back to back, one headless
Claude Code session per run, on Claude Fable 5.2 only. `README.md` says what it does.

## Before changing anything

Read `notes/ideas.md` first, all of it. It is the runner's list of work:

- **To do** at the top: every change still to be made. **Ideas are things to do, not a wish
  list** (user, 2026-11-19): an idea goes on this list beside the issues that actual use has
  shown, and is worked off the same way. Add new ideas and new issues there, each saying where
  it came from and what it waits for; tick an entry when it is made.
- **Waiting on checks**: behaviour only a real run shows. Tick a check, with the date and the
  evidence, when a run has shown it.
- **Findings**: what real runs have shown, with dates and evidence.

## Rules

- `runner.py` stays one file, Python 3.9 or later, standard library only; options through
  `argparse`. Keep `README.md` and the docstring in step with it.
- Every run is pinned to `claude-fable-5-2`, with no fallback model: the trace's merge gate
  fails a package that another model worked on.
- The user starts and stops the runner. Do not start it, and do not interrupt one that is
  going, without being asked. `./runner.py --check` runs nothing and is always safe.
- Test changes offline (compile under Python 3.9 and 3.14; feed the run handler made-up
  stream events) before the runner next starts. Do not put sample tracing commands
  (`tools/t.sh ...`) in shell commands of a session working in the trace repository: its
  history would then look like tracing to the runner's busy-session check for 15 minutes.
  Put them in a test file instead.
- Dates and times from `date`.
