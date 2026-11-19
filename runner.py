#!/usr/bin/env python3
"""Run /trace6845 back to back, one Claude Code session per run, on Claude Fable 5.2 only.

Each run is `claude -p /trace6845` in the trace repository's main checkout, with the model
pinned and no fallback model. When the usage limit stops a run, the loop waits, checks every
few minutes with a tiny request, and resumes the interrupted session when the quota is back.
It stops by itself when two finished runs in a row put nothing on main, and on request.

Feedback:
  var/runner.log    one line per event; follow it with `tail -f` (the path is printed at start)
  var/status        one line, the current state, rewritten as things happen
  var/runs/NNN.log  what run NNN did, one line per tool call or message
  var/current.log   a link to the current run's log: `tail -F` follows it from run to run
  var/runs/NNN-report.md  the run's reports: the text of every result, numbered in order

Each run is refused if another Claude Code session is tracing in the repository, and a saved
session that was resumed elsewhere is not resumed again. `--reset` forgets the saved session.

Control (files in this folder, created by hand, e.g. `touch STOP`):
  STOP   stop gracefully: between runs, or while waiting for quota. A run in progress
         finishes first. Ctrl-C stops at once (the session is resumed on the next start).
  GO     while waiting for quota: check now instead of at the next interval.

Only the Python standard library is used; Python 3.9 or later.
"""
import argparse
import datetime
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
VAR = os.path.join(HERE, 'var')
RUNS = os.path.join(VAR, 'runs')
LOG = os.path.join(VAR, 'runner.log')
CURRENT = os.path.join(VAR, 'current.log')     # link to the current run's log, for `tail -F`
STATUS = os.path.join(VAR, 'status')
STATE = os.path.join(VAR, 'state.json')
LOCK = os.path.join(VAR, 'runner.lock')
STOP = os.path.join(HERE, 'STOP')
GO = os.path.join(HERE, 'GO')

MODEL = 'claude-fable-5-2'     # the project's rule: tracing is done with Fable 5.2 only

# Fable 5.2's API prices, $ per million tokens: input, output, 5-minute cache write, 1-hour cache
# write, cache read (as tools/costs.py in the trace repository has them). Used only to estimate
# the cost of a run that ends without a result (interrupted, stalled, a model mismatch).
PRICES = (8.00, 40.00, 10.00, 16.00, 0.20)
SYNTHETIC = '<synthetic>'      # Claude Code's own messages (errors, limits), not a model
SKILL = os.path.join('.claude', 'skills', 'trace6845', 'SKILL.md')

# Wording of a usage-limit message. Matched only against Claude Code's own messages, the
# text of a failed result and stderr, never against what the model writes.
LIMIT_RE = re.compile(r'usage limit|hit your (usage |weekly )?limit|limit reached|limit will reset|'
                      r'out of (extra )?usage|resets? (at|in|on) |rate.?limit|quota', re.I)

FAST_FAIL_SECONDS = 180        # a run that ends this quickly having done nothing counts as blocked
MAX_RESUMES = 2                # resumes of one session after errors or stalls, before giving up on it
MAX_NO_PROGRESS = 2            # finished runs in a row with nothing put on main, before stopping
MAX_BLOCKED = 3                # runs in a row that do not start although the quota check passes

# Headless Claude Code waits only 10 minutes after a turn ends for background tasks, then stops
# them: run 1 (2026-11-19) ended its turn to wait for its background reviewer, which was stopped
# part-way, so nothing merged. The wait is set to the idle timeout instead (run_env): long enough
# for any review seen so far (10-17 minutes), and finite, so that a stray background command (the
# resumed run 1 left two polling loops running) cannot hold a finished run open for ever.
BG_WAIT_VAR = 'CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'

# Text of a tool result that auto mode refused. Nobody can approve it in a headless run.
REFUSED = 'denied by the Claude Code auto mode classifier'
REFUSED_REASON_RE = re.compile(r'Reason:\s*\[?([^\].]+)')

# A tracing call, as tools/costs.py in the trace repository counts one: the tracing wrapper run
# for a package, a ship of its files, or an edit of them.
T_RUN_RE = re.compile(r'tools/t\.sh\s+[a-z][a-z0-9_]*\s+\S+\.py')
SHIP_RUN_RE = re.compile(r'tools/ship\.sh\s+["\']')
PKG_FILE_RE = re.compile(r'(?:^|/)(?:trace/[a-z][a-z0-9_]*\.txt|notes/blocks/[a-z][a-z0-9_]*\.md|'
                         r'floorplan\.d/[a-z][a-z0-9_]*\.txt|ports\.d/[a-z][a-z0-9_]*\.txt)$')
BUSY_WINDOW_MINUTES = 15       # a busy session with a tracing call this recent is tracing now
CONTINUED_RE = re.compile(r'"type":\s*"continued-in"')


def is_fable(model):
    """True for the project's model, also when the name carries a context suffix such as [1m]."""
    return (model or '').split('[')[0] == MODEL

# Appended to the system prompt of every run, new or resumed: a headless run does not know it
# is part of a loop, and the skill leaves it a few choices that a person would otherwise make.
UNATTENDED_PROMPT = (
    'This session is one run of an unattended loop (hd6845-runner). Nobody will see or answer '
    'questions until the run has ended, so do not ask the user anything. Where /trace6845 leaves '
    'a choice, make it by the skill\'s own rules and say what you chose. Do one round of one '
    'package. Reviewer agents run in the background here, whatever you ask for, and each '
    'reviewer\'s report is delivered to you as a message when it finishes. Do not poll a '
    'reviewer\'s files or wait for it in a loop: if there is nothing else to do while it works, '
    'end your turn, and the run stays open until the report arrives. Before your final report, '
    'stop any background commands you started. When the step 9 report is written, end your turn: '
    'the loop starts the next round in a new session. At step 8, use `--continue` when the package '
    'has work left (the next run carries on with it within minutes), and release the claim when '
    'it is finished. If a step fails in a way the skill does not cover, stop as the skill says '
    '(push the branch, make the notes say where to resume, report) and end your turn.')

RESUME_PROMPT = ('You were stopped part-way through /trace6845 (by the usage limit, an error, or '
                 'the runner being interrupted). Carry on from exactly where you stopped, following '
                 'the /trace6845 instructions and notes/workflow.md to the letter. Check the state '
                 'of the claim, the branch and main before acting. A reviewer that was stopped '
                 'before it reported means its round did not happen: run that round again with a '
                 'new reviewer, so that every piece has its own review recorded. If you had already '
                 'finished, say so and stop.')


def run_env(a):
    """The environment of each run: the background wait set to the idle timeout."""
    return dict(os.environ, **{BG_WAIT_VAR: str(a.idle_timeout * 60 * 1000)})


def project_dir(path):
    """Claude Code's folder for the session histories of a working folder."""
    return os.path.join(os.path.expanduser('~/.claude/projects'),
                        path.replace('/', '-').replace('.', '-'))


# ---------------------------------------------------------------- output

def now():
    return datetime.datetime.now().astimezone()


def stamp(t=None):
    return (t or now()).strftime('%Y-%m-%d %H:%M:%S %z')


def hm(t):
    return t.strftime('%H:%M')


class Out:
    """The event log, the status line, and optional macOS notifications."""

    def __init__(self, notify):
        self.notify_on = notify

    def log(self, kind, text, notify=False):
        line = '%s  %-8s %s' % (stamp(), kind, text)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
        print(line, flush=True)
        if notify:
            self.notify(kind, text)

    def status(self, kind, text):
        tmp = STATUS + '.tmp'
        with open(tmp, 'w') as f:
            f.write('%s  %-8s %s\n' % (stamp(), kind, text))
        os.replace(tmp, STATUS)

    def notify(self, kind, text):
        if not self.notify_on or not shutil.which('osascript'):
            return
        msg = text.replace('\\', '\\\\').replace('"', '\\"')[:200]
        subprocess.run(['osascript', '-e', 'display notification "%s" with title "hd6845 runner: %s"'
                        % (msg, kind)], capture_output=True)


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'run': 0, 'session': None, 'status': None, 'resumes': 0, 'no_progress': 0,
                'total_cost_usd': 0.0}


def save_state(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def point_current(runlog):
    """Point var/current.log at this run's log, so that `tail -F var/current.log` follows the
    current run from one run to the next. The link is replaced in one step (rename)."""
    open(runlog, 'a').close()
    tmp = CURRENT + '.tmp'
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    os.symlink(os.path.relpath(runlog, VAR), tmp)
    os.replace(tmp, CURRENT)


def take_file(path):
    """True if the control file exists; it is removed (consumed)."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- git

def git(repo, *args):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def main_head(repo):
    git(repo, 'fetch', '-q', 'origin')
    return git(repo, 'rev-parse', 'origin/main')


def handoffs_since(repo, base, who):
    """Hand-off commits by `who` that reached main after `base`: the sign that a run merged."""
    head = main_head(repo)
    if not base or not head or base == head:
        return []
    out = git(repo, 'log', '%s..%s' % (base, head), '--author=' + who, '--grep=^Handoff ',
              '--format=%h %s')
    return [l for l in (out or '').splitlines() if l]


# ---------------------------------------------------------------- one run

def short(s, n=140):
    s = ' '.join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + '…'


def describe_tool(block):
    name = block.get('name', '?')
    inp = block.get('input') or {}
    if name == 'Bash':
        what = inp.get('description') or inp.get('command', '')
    elif name in ('Read', 'Write', 'Edit'):
        what = inp.get('file_path', '')
    elif name in ('Agent', 'Task'):
        what = '%s (%s)' % (inp.get('description', ''), inp.get('model', 'model not given'))
    elif name == 'Skill':
        what = inp.get('skill', '')
    else:
        what = ''
    return '%s: %s' % (name, short(what, 120))


class Run:
    """One `claude -p` process, followed through its stream-json output."""

    def __init__(self, a, out, number, session, resume, prompt):
        self.a, self.out = a, out
        self.number, self.session, self.resume, self.prompt = number, session, resume, prompt
        self.runlog = os.path.join(RUNS, '%03d.log' % number)
        self.report = os.path.join(RUNS, '%03d-report.md' % number)
        self.result = None
        self.limit_text = None
        self.mismatch = None
        self.tool_calls = 0
        self.refusals = 0
        self.last = ''
        self.stalled = False
        self.interrupted = False
        self.usage = {}                # message id -> usage, for a cost without a result
        try:                           # results already in this run's report (a resumed run)
            with open(self.report) as f:
                self.results = sum(1 for line in f if line.startswith('<!-- result '))
        except OSError:
            self.results = 0

    def command(self):
        cmd = [self.a.claude, '-p', self.prompt, '--model', MODEL,
               '--output-format', 'stream-json', '--verbose',
               '--effort', self.a.effort,
               '--permission-mode', self.a.permission_mode,
               '--append-system-prompt', UNATTENDED_PROMPT]
        cmd += ['--resume', self.session] if self.resume else ['--session-id', self.session]
        return cmd

    def note(self, text):
        with open(self.runlog, 'a') as f:
            f.write('%s  %s\n' % (hm(now()), text))

    def handle(self, ev):
        kind = ev.get('type')
        if kind == 'system' and ev.get('subtype') == 'init':
            if ev.get('session_id'):
                self.session = ev['session_id']
            model = ev.get('model')
            if model and not is_fable(model):
                self.mismatch = 'session started on %s' % model
            self.note('session %s on %s' % (self.session, model))
        elif kind == 'assistant':
            msg = ev.get('message') or {}
            model = msg.get('model')
            agent = '  [agent] ' if ev.get('parent_tool_use_id') else ''
            content = msg.get('content') if isinstance(msg.get('content'), list) else []
            if model == SYNTHETIC:
                text = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                self.note('%sclaude code: %s' % (agent, short(text, 300)))
                if LIMIT_RE.search(text):
                    self.limit_text = short(text, 200)
                return
            if model and not is_fable(model):
                self.mismatch = 'a response came from %s' % model
            if msg.get('id') and isinstance(msg.get('usage'), dict):
                self.usage[msg['id']] = msg['usage']
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use':
                    self.tool_calls += 1
                    self.last = describe_tool(b)
                    self.note(agent + self.last)
                elif b.get('type') == 'text' and b.get('text', '').strip():
                    self.note('%ssays: %s' % (agent, short(b['text'], 200)))
        elif kind == 'user':
            msg = ev.get('message') or {}
            agent = '  [agent] ' if ev.get('parent_tool_use_id') else ''
            for b in msg.get('content') if isinstance(msg.get('content'), list) else []:
                if not isinstance(b, dict) or b.get('type') != 'tool_result':
                    continue
                c = b.get('content')
                text = c if isinstance(c, str) else ' '.join(
                    x.get('text', '') for x in c or [] if isinstance(x, dict))
                if REFUSED in text:
                    self.refusals += 1
                    m = REFUSED_REASON_RE.search(text)
                    self.note('%sREFUSED by auto mode (%s), after: %s'
                              % (agent, m.group(1).strip() if m else 'no reason given', self.last))
        elif kind == 'result':
            # A turn can follow the final report (a late notification from a background task
            # starts one), so every result is kept, in order: the last is not always the report.
            self.result = ev
            if ev.get('session_id'):
                self.session = ev['session_id']
            text = str(ev.get('result') or '')
            if ev.get('is_error') and LIMIT_RE.search(text):
                self.limit_text = short(text, 200)
            if text.strip():
                # Numbered, not timed: a headless run sends all its results when it ends, so the
                # time one arrives is not the time its turn ended (run 3: both 15:12:22).
                self.results += 1
                with open(self.report, 'a') as f:
                    f.write('<!-- result %d: %s%s, %s turns (received %s, when the run ended) -->'
                            '\n\n%s\n\n' % (self.results, ev.get('subtype'),
                                            ', error' if ev.get('is_error') else '',
                                            ev.get('num_turns'), stamp(), text))
            self.note('result: %s%s, %s turns, $%.2f' % (
                ev.get('subtype'), ' (error)' if ev.get('is_error') else '',
                ev.get('num_turns'), ev.get('total_cost_usd') or 0))

    def go(self, st):
        """Run to the end. Returns the outcome: finished, quota, blocked, error, stalled,
        interrupted or model_mismatch."""
        self.started = now()
        stderr_path = self.runlog[:-4] + '.stderr'
        self.note('--- %s, %s: %s' % (stamp(self.started), 'resume' if self.resume else 'new session',
                                      short(self.prompt, 200)))
        point_current(self.runlog)
        with open(stderr_path, 'a') as err:
            proc = subprocess.Popen(self.command(), cwd=self.a.repo, stdout=subprocess.PIPE,
                                    stderr=err, text=True, bufsize=1, env=run_env(self.a))
        lines = queue.Queue()

        def reader():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        last_event = time.monotonic()
        last_status = 0.0
        last_size = self.history_size()
        try:
            while True:
                try:
                    line = lines.get(timeout=5)
                except queue.Empty:
                    line = ''
                if line is None:
                    break
                if line:
                    last_event = time.monotonic()
                    try:
                        self.handle(json.loads(line))
                    except ValueError:
                        self.note('(not JSON) ' + short(line, 200))
                    if st.get('session') != self.session:
                        st['session'] = self.session
                        save_state(st)
                if self.mismatch:
                    self.note('MODEL MISMATCH: ' + self.mismatch)
                    self.stop(proc)
                    break
                if time.monotonic() - last_event > self.a.idle_timeout * 60:
                    self.stalled = True
                    self.note('no output for %d min: stopping the run' % self.a.idle_timeout)
                    self.stop(proc)
                    break
                if time.monotonic() - last_status > 10:
                    last_status = time.monotonic()
                    # Growth of the session's history files, its reviewers' among them, also
                    # counts as activity, whatever comes through on stdout.
                    size = self.history_size()
                    if size != last_size:
                        last_event, last_size = time.monotonic(), size
                    mins = (now() - self.started).total_seconds() / 60
                    self.out.status('RUNNING', 'run %d%s, %d min, %d tool calls; last: %s' % (
                        self.number, ' (resumed)' if self.resume else '', mins, self.tool_calls,
                        self.last or 'starting'))
        except KeyboardInterrupt:
            self.interrupted = True
            self.note('interrupted (Ctrl-C or SIGTERM)')
            self.stop(proc)
        proc.wait()
        self.ended = now()
        return self.outcome(stderr_path, proc.returncode)

    def stop(self, proc):
        if proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    def outcome(self, stderr_path, code):
        r = self.result or {}
        try:
            with open(stderr_path) as f:
                tail = f.read()[-2000:]
        except OSError:
            tail = ''
        if not self.limit_text and LIMIT_RE.search(tail):
            self.limit_text = short(tail.strip().splitlines()[-1], 200)
        self.reason = (self.limit_text or short(r.get('result') or '', 200)
                       or short(tail.strip().splitlines()[-1] if tail.strip() else '', 200)
                       or 'claude exited with status %s and no result' % code)
        if self.mismatch:
            return 'model_mismatch'
        if self.interrupted:
            return 'interrupted'
        if self.stalled:
            return 'stalled'
        if self.limit_text:
            return 'quota'
        failed = not r or r.get('is_error')
        quick = (self.ended - self.started).total_seconds() < FAST_FAIL_SECONDS
        if failed and quick and self.tool_calls == 0:
            return 'blocked'
        if failed:
            return 'error'
        return 'finished'

    def history_size(self):
        """Total size of the session's history files: the main one and its reviewers'."""
        base = os.path.join(project_dir(self.a.repo), self.session)
        total = 0
        for p in [base + '.jsonl'] + [os.path.join(base, 'subagents', f) for f in
                                      (os.listdir(os.path.join(base, 'subagents'))
                                       if os.path.isdir(os.path.join(base, 'subagents')) else [])]:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def estimated_cost(self):
        """The cost of the responses seen on the stream, at Fable's API prices."""
        total = 0.0
        for u in self.usage.values():
            cc = u.get('cache_creation') or {}
            w5 = cc.get('ephemeral_5m_input_tokens')
            w1 = cc.get('ephemeral_1h_input_tokens')
            if w5 is None and w1 is None:
                w5, w1 = u.get('cache_creation_input_tokens') or 0, 0
            tokens = (u.get('input_tokens') or 0, u.get('output_tokens') or 0, w5 or 0, w1 or 0,
                      u.get('cache_read_input_tokens') or 0)
            total += sum(t * p for t, p in zip(tokens, PRICES)) / 1e6
        return total

    def cost(self):
        """The run's cost: Claude Code's own figure from the last result, or, for a run that
        ended without one, an estimate from the responses seen on the stream."""
        r = self.result or {}
        if r.get('total_cost_usd') is not None:
            return float(r['total_cost_usd'])
        return self.estimated_cost()

    def cost_text(self):
        if (self.result or {}).get('total_cost_usd') is not None:
            return '$%.2f' % self.cost()
        return '$%.2f (estimated from the stream: no result)' % self.cost()


# ---------------------------------------------------------------- waiting for quota

def probe(a):
    """A tiny Fable request, run outside the repository and not saved. (ok, reason)."""
    cmd = [a.claude, '-p', 'Reply with the single word OK.', '--model', MODEL,
           '--output-format', 'json', '--no-session-persistence']
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, 'the check timed out'
    try:
        res = json.loads(r.stdout)
    except ValueError:
        return False, short(r.stderr or r.stdout or 'no output', 200)
    if res.get('is_error') or r.returncode != 0:
        return False, short(res.get('result') or r.stderr or 'error', 200)
    models = set((res.get('modelUsage') or {}).keys())
    if models and not any(is_fable(m) for m in models):
        return False, 'the check was answered by %s, not %s' % (', '.join(sorted(models)), MODEL)
    return True, 'ok'


def wait_for_quota(a, out, reason):
    """Wait until a check succeeds. Returns False if told to stop."""
    out.log('BLOCKED', '%s; checking every %d min (touch GO to check now, STOP to stop)'
            % (reason, a.check_every), notify=True)
    last_reason, last_logged = reason, time.monotonic()
    due = time.monotonic() + a.check_every * 60
    while True:
        if take_file(STOP):
            out.log('STOPPED', 'stop file, while waiting for quota', notify=True)
            out.status('STOPPED', 'stop file, while waiting for quota')
            return False
        forced = take_file(GO)
        if forced or time.monotonic() >= due:
            ok, why = probe(a)
            if ok:
                out.log('RESUME', 'quota available again', notify=True)
                return True
            if why != last_reason or time.monotonic() - last_logged > 3600:
                out.log('BLOCKED', 'still blocked: %s' % why)
                last_reason, last_logged = why, time.monotonic()
            due = time.monotonic() + a.check_every * 60
        nxt = now() + datetime.timedelta(seconds=max(0, due - time.monotonic()))
        out.status('BLOCKED', '%s; next check %s (touch GO to check now)' % (last_reason, hm(nxt)))
        time.sleep(5)


# ---------------------------------------------------------------- other sessions

def recent_entries(path, minutes, tail_bytes=4 << 20):
    """The entries of a session history file written in the last `minutes`."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        try:
            e = json.loads(line)
            t = datetime.datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00'))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if t >= cutoff:
            out.append(e)
    return out


def traced_recently(path, minutes):
    """True if the history file (or its reviewers' files) holds a tracing call in the last
    `minutes`: the tracing wrapper, a ship, or an edit of a package's files."""
    files = [path] + [os.path.join(path[:-6], 'subagents', f)
                      for f in (os.listdir(os.path.join(path[:-6], 'subagents'))
                                if os.path.isdir(os.path.join(path[:-6], 'subagents')) else [])
                      if f.endswith('.jsonl')]
    for p in files:
        try:
            if time.time() - os.path.getmtime(p) > minutes * 60:
                continue
        except OSError:
            continue
        for e in recent_entries(p, minutes):
            content = (e.get('message') or {}).get('content')
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get('type') != 'tool_use':
                    continue
                inp = b.get('input') or {}
                if b.get('name') == 'Bash':
                    cmd = str(inp.get('command', ''))
                    if T_RUN_RE.search(cmd) or SHIP_RUN_RE.search(cmd):
                        return True
                elif b.get('name') in ('Edit', 'Write') and PKG_FILE_RE.search(str(inp.get('file_path', ''))):
                    return True
    return False


def tracing_sessions(a):
    """Other Claude Code sessions tracing in the trace repository now, as descriptions, or None
    if the sessions could not be listed. A busy session counts if it works in one of the
    packages' working copies, or in the main checkout with a tracing call in the last
    BUSY_WINDOW_MINUTES. Idle sessions, and busy ones that are not tracing (a conversation
    about the trace, say), do not count."""
    try:
        r = subprocess.run([a.claude, 'agents', '--json'], capture_output=True, text=True, timeout=60)
        sessions = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    parent = os.path.dirname(a.repo)
    worktree = os.path.basename(a.repo) + '-'
    found = []
    for s in sessions if isinstance(sessions, list) else []:
        cwd = os.path.abspath(str(s.get('cwd') or ''))
        if s.get('status') != 'busy':
            continue
        in_worktree = os.path.dirname(cwd) == parent and os.path.basename(cwd).startswith(worktree)
        if cwd != a.repo and not in_worktree:
            continue
        sid = str(s.get('sessionId') or '')
        hist = os.path.join(project_dir(cwd), sid + '.jsonl')
        if in_worktree or traced_recently(hist, BUSY_WINDOW_MINUTES):
            found.append('%s (%s, %s, pid %s, in %s)' % (s.get('name') or sid[:8], sid[:8],
                                                        s.get('kind'), s.get('pid'), cwd))
    return found


def continued_in(a, session):
    """The ID a session was continued under elsewhere (resumed outside the runner), or None."""
    path = os.path.join(project_dir(a.repo), session + '.jsonl')
    nxt = None
    try:
        with open(path, errors='replace') as f:
            for line in f:
                if CONTINUED_RE.search(line):
                    try:
                        nxt = json.loads(line).get('continuedInSessionId') or nxt
                    except ValueError:
                        pass
    except OSError:
        return None
    return nxt


# ---------------------------------------------------------------- the loop

def take_lock():
    """The runner's lock, held until exit, or None if another runner holds it."""
    lock = open(LOCK, 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.seek(0)
        print('runner: another runner is already going (pid %s; %s is locked)'
              % (lock.read().strip() or '?', LOCK), file=sys.stderr)
        return None
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def check_setup(a):
    """Problems that stop the runner before it starts (empty list: none)."""
    problems = []
    if not a.claude:
        problems.append('the claude command was not found (give it with --claude)')
    if not os.path.isdir(os.path.join(a.repo, '.git')):
        problems.append('%s is not the trace repository\'s main checkout (give it with --repo)' % a.repo)
    elif not os.path.isfile(os.path.join(a.repo, SKILL)):
        problems.append('%s has no %s' % (a.repo, SKILL))
    if os.path.exists(STOP):
        problems.append('%s exists: remove it to start' % STOP)
    return problems


def parse_args():
    p = argparse.ArgumentParser(
        description='Run /trace6845 back to back, one Claude Code session per run, on %s only.' % MODEL,
        epilog='Steer a running loop with files in %s: STOP (stop between runs) and GO (check '
               'the quota now).' % HERE)
    p.add_argument('--repo', default=os.path.join(os.path.dirname(HERE), 'hd6845-trace'),
                   help='the trace repository\'s main checkout (default: %(default)s)')
    p.add_argument('--package', metavar='PREFIX',
                   help='package for the first new run (later runs take the next one themselves)')
    p.add_argument('--on-quota', choices=('wait', 'stop'), default='wait',
                   help='when the usage limit is reached: wait and resume when quota is back, '
                        'or stop (default: %(default)s)')
    p.add_argument('--check-every', type=int, default=10, metavar='MIN',
                   help='minutes between quota checks while blocked (default: %(default)s)')
    p.add_argument('--idle-timeout', type=int, default=90, metavar='MIN',
                   help='stop a run that shows no activity for this long (no output, and no '
                        'growth of its session\'s history files), and resume it; also how long a '
                        'run waits for background tasks after its turn ends (default: %(default)s)')
    p.add_argument('--effort', default='high', choices=('low', 'medium', 'high', 'xhigh', 'max'),
                   help='effort level of each run, new or resumed (default: %(default)s, as the '
                        'trace sessions in the desktop app have used)')
    p.add_argument('--permission-mode', default='auto',
                   choices=('auto', 'acceptEdits', 'bypassPermissions', 'dontAsk'),
                   help='permission mode of each run; nobody is there to approve anything '
                        '(default: %(default)s)')
    p.add_argument('--notify', action='store_true',
                   help='also show macOS notifications when blocked, resumed or stopped')
    p.add_argument('--claude', default=shutil.which('claude'),
                   help='the claude command (default: %(default)s)')
    p.add_argument('--check', action='store_true',
                   help='check the setup, show what would run, and exit without running anything')
    p.add_argument('--reset', action='store_true',
                   help='forget the saved session and the counters, so that the next start begins '
                        'a new run; runs nothing, and refuses while a runner is going')
    a = p.parse_args()
    a.repo = os.path.abspath(os.path.expanduser(a.repo))
    return a


def main():
    a = parse_args()
    os.makedirs(RUNS, exist_ok=True)
    out = Out(a.notify)

    st = load_state()
    if a.reset:
        lock = take_lock()
        if not lock:
            return 2
        old = 'session %s (%s), %d no-progress, %d resumes, %d blocked' % (
            st.get('session'), st.get('status'), st.get('no_progress', 0), st.get('resumes', 0),
            st.get('blocked', 0))
        st.update(session=None, status=None, resumes=0, no_progress=0, blocked=0)
        save_state(st)
        out.log('RESET', 'forgot %s; the next start begins run %d in a new session'
                % (old, st.get('run', 0) + 1))
        out.status('RESET', 'the next start begins a new run')
        return 0

    problems = check_setup(a)
    resume = bool(st.get('session')) and st.get('status') in ('running', 'interrupted')
    if resume and continued_in(a, st['session']):
        problems.append('the saved session %s was continued elsewhere as %s (resumed outside the '
                        'runner): look at it, then ./runner.py --reset'
                        % (st['session'], continued_in(a, st['session'])))
    others = tracing_sessions(a) if a.claude else None
    if others:
        problems.append('another Claude Code session is tracing in the repository: %s; start '
                        'when it has finished' % '; '.join(others))
    if a.check:
        print('log:     tail -f %s' % LOG)
        print('run:     tail -F %s' % CURRENT)
        print('status:  cat %s' % STATUS)
        print('claude:  %s' % a.claude)
        print('repo:    %s' % a.repo)
        print('model:   %s, effort %s, permission mode %s; background wait %d min'
              % (MODEL, a.effort, a.permission_mode, a.idle_timeout))
        print('state:   %s' % json.dumps(st))
        print('next:    %s' % ('resume session %s' % st['session'] if resume
                                else 'new session: /trace6845' + (' ' + a.package if a.package else '')))
        print('others:  %s' % ('could not list the sessions' if others is None
                                else 'none tracing' if not others else '; '.join(others)))
        print('problems: ' + ('; '.join(problems) if problems else 'none'))
        return 1 if problems else 0
    if problems:
        for pr in problems:
            print('runner: ' + pr, file=sys.stderr)
        return 2

    lock = take_lock()
    if not lock:
        return 2

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, on_sigterm)
    if shutil.which('caffeinate'):          # keep the Mac awake while the runner is going
        subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])

    print('Follow the log with:\n\n  tail -f %s\n\nand the current run, across runs, with:\n\n'
          '  tail -F %s\n' % (LOG, CURRENT), flush=True)
    out.log('START', 'pid %d; repo %s; model %s, effort %s; on quota: %s; permission mode: %s'
            % (os.getpid(), a.repo, MODEL, a.effort, a.on_quota, a.permission_mode))
    who = git(a.repo, 'config', 'user.name') or ''
    package = a.package
    warned_listing = False

    while True:
        if take_file(STOP):
            out.log('STOPPED', 'stop file', notify=True)
            out.status('STOPPED', 'stop file')
            return 0

        resume = bool(st.get('session')) and st.get('status') in ('running', 'interrupted')
        others = tracing_sessions(a)
        if others is None and not warned_listing:
            out.log('WARNING', 'could not list the Claude Code sessions; not checking for others')
            warned_listing = True
        if others:
            out.log('STOPPED', 'another Claude Code session is tracing in the repository: %s; '
                    'start again when it has finished' % '; '.join(others), notify=True)
            out.status('STOPPED', 'another session is tracing in the repository')
            return 7
        if resume and continued_in(a, st['session']):
            out.log('STOPPED', 'the saved session %s was continued elsewhere as %s (resumed '
                    'outside the runner): look at it, then ./runner.py --reset'
                    % (st['session'], continued_in(a, st['session'])), notify=True)
            out.status('STOPPED', 'saved session continued elsewhere; --reset after looking')
            return 6
        if resume:
            prompt, session = RESUME_PROMPT, st['session']
            out.log('RUN', 'run %d: resuming session %s' % (st['run'], session))
        else:
            st['run'] += 1
            st['resumes'] = 0
            st['base'] = main_head(a.repo)
            prompt = '/trace6845' + (' ' + package if package else '')
            package = None
            session = str(uuid.uuid4())
            out.log('RUN', 'run %d: new session %s: %s' % (st['run'], session, prompt))
        st.update(session=session, status='running', started=stamp())
        save_state(st)

        run = Run(a, out, st['run'], session, resume, prompt)
        result = run.go(st)
        st['session'] = run.session
        st['total_cost_usd'] = round(st.get('total_cost_usd', 0) + run.cost(), 2)
        mins = (run.ended - run.started).total_seconds() / 60
        summary = 'run %d: %d min, %s (all runs $%.2f)' % (st['run'], mins, run.cost_text(),
                                                          st['total_cost_usd'])
        if run.refusals:
            summary += ', %d refused by auto mode (see %s)' % (run.refusals, run.runlog)

        if result == 'model_mismatch':
            st['status'] = 'abandoned'
            save_state(st)
            out.log('STOPPED', '%s: MODEL MISMATCH, %s. Session %s is not resumed; the package\'s '
                    'merge gate would fail on it: look before going on.'
                    % (summary, run.mismatch, run.session), notify=True)
            out.status('STOPPED', 'model mismatch in run %d' % st['run'])
            return 3

        if result == 'interrupted':
            st['status'] = 'interrupted'
            save_state(st)
            out.log('STOPPED', '%s: interrupted; the next start resumes session %s'
                    % (summary, run.session), notify=True)
            out.status('STOPPED', 'interrupted; next start resumes run %d' % st['run'])
            return 130

        st['blocked'] = st.get('blocked', 0) + 1 if run.tool_calls == 0 else 0
        if result in ('quota', 'blocked'):
            # A new run that did nothing at all is started afresh rather than resumed;
            # a session that did some work is resumed when the quota is back.
            fresh = not resume and run.tool_calls == 0
            st['status'] = None if fresh else 'interrupted'
            if fresh:
                st['run'] -= 1
            save_state(st)
            out.log('ENDED', '%s: %s' % (summary, 'usage limit' if result == 'quota' else 'did not start'))
            if result == 'blocked' and st['blocked'] >= MAX_BLOCKED:
                out.log('STOPPED', '%d runs in a row did not start although the quota check '
                        'passed: %s (see %s)' % (st['blocked'], run.reason, run.runlog), notify=True)
                out.status('STOPPED', 'runs do not start; read the run log')
                st['blocked'] = 0
                save_state(st)
                return 5
            if result == 'quota' and a.on_quota == 'stop':
                out.log('STOPPED', 'usage limit (%s); the next start resumes session %s'
                        % (run.reason, run.session), notify=True)
                out.status('STOPPED', 'usage limit; next start resumes run %d' % st['run'])
                return 0
            if not wait_for_quota(a, out, run.reason):
                return 0
            continue

        if result in ('error', 'stalled'):
            st['resumes'] = st.get('resumes', 0) + 1
            if st['resumes'] <= MAX_RESUMES:
                st['status'] = 'interrupted'
                save_state(st)
                out.log('ENDED', '%s: %s (%s); resuming it (%d of %d)'
                        % (summary, result, run.reason, st['resumes'], MAX_RESUMES))
                continue
            out.log('ENDED', '%s: %s (%s); given up on this session after %d resumes'
                    % (summary, result, run.reason, MAX_RESUMES))
        else:
            out.log('DONE', '%s; report %s' % (summary, run.report))

        st['status'] = 'finished'
        merged = handoffs_since(a.repo, st.get('base'), who)
        if merged:
            st['no_progress'] = 0
            out.log('MERGED', 'run %d: %s' % (st['run'], '; '.join(merged)))
        else:
            st['no_progress'] = st.get('no_progress', 0) + 1
            out.log('NOMERGE', 'run %d put nothing on main (%d in a row)' % (st['run'], st['no_progress']))
        save_state(st)
        if st['no_progress'] >= MAX_NO_PROGRESS:
            out.log('STOPPED', '%d runs in a row put nothing on main: read %s'
                    % (st['no_progress'], run.report), notify=True)
            out.status('STOPPED', 'no progress; read the last report')
            st['no_progress'] = 0
            save_state(st)
            return 4


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
