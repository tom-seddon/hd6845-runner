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
  var/runs/NNN-report.md  the run's final report

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
STATUS = os.path.join(VAR, 'status')
STATE = os.path.join(VAR, 'state.json')
LOCK = os.path.join(VAR, 'runner.lock')
STOP = os.path.join(HERE, 'STOP')
GO = os.path.join(HERE, 'GO')

MODEL = 'claude-fable-5-2'     # the project's rule: tracing is done with Fable 5.2 only
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


def is_fable(model):
    """True for the project's model, also when the name carries a context suffix such as [1m]."""
    return (model or '').split('[')[0] == MODEL

RESUME_PROMPT = ('You were stopped part-way through /trace6845 (by the usage limit, an error, or '
                 'the runner being interrupted). Carry on from exactly where you stopped, following '
                 'the /trace6845 instructions and notes/workflow.md to the letter. Check the state '
                 'of the claim, the branch and main before acting. If you had already finished, '
                 'say so and stop.')


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
        self.last = ''
        self.stalled = False
        self.interrupted = False

    def command(self):
        cmd = [self.a.claude, '-p', self.prompt, '--model', MODEL,
               '--output-format', 'stream-json', '--verbose',
               '--permission-mode', self.a.permission_mode]
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
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use':
                    self.tool_calls += 1
                    self.last = describe_tool(b)
                    self.note(agent + self.last)
                elif b.get('type') == 'text' and b.get('text', '').strip():
                    self.note('%ssays: %s' % (agent, short(b['text'], 200)))
        elif kind == 'result':
            self.result = ev
            if ev.get('session_id'):
                self.session = ev['session_id']
            text = str(ev.get('result') or '')
            if ev.get('is_error') and LIMIT_RE.search(text):
                self.limit_text = short(text, 200)
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
        with open(stderr_path, 'a') as err:
            proc = subprocess.Popen(self.command(), cwd=self.a.repo, stdout=subprocess.PIPE,
                                    stderr=err, text=True, bufsize=1)
        lines = queue.Queue()

        def reader():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        last_event = time.monotonic()
        last_status = 0.0
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
        if r.get('result'):
            with open(self.report, 'w') as f:
                f.write(str(r['result']) + '\n')
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

    def cost(self):
        return float((self.result or {}).get('total_cost_usd') or 0)


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


# ---------------------------------------------------------------- the loop

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
                   help='stop a run that prints nothing for this long, and resume it '
                        '(default: %(default)s)')
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
    a = p.parse_args()
    a.repo = os.path.abspath(os.path.expanduser(a.repo))
    return a


def main():
    a = parse_args()
    os.makedirs(RUNS, exist_ok=True)
    out = Out(a.notify)

    problems = check_setup(a)
    st = load_state()
    if a.check:
        print('log:     tail -f %s' % LOG)
        print('status:  cat %s' % STATUS)
        print('claude:  %s' % a.claude)
        print('repo:    %s' % a.repo)
        print('state:   %s' % json.dumps(st))
        resume = st.get('session') and st.get('status') in ('running', 'interrupted')
        print('next:    %s' % ('resume session %s' % st['session'] if resume
                                else 'new session: /trace6845' + (' ' + a.package if a.package else '')))
        print('problems: ' + ('; '.join(problems) if problems else 'none'))
        return 1 if problems else 0
    if problems:
        for pr in problems:
            print('runner: ' + pr, file=sys.stderr)
        return 2

    lock = open(LOCK, 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.seek(0)
        print('runner: another runner is already going (pid %s; %s is locked)'
              % (lock.read().strip() or '?', LOCK), file=sys.stderr)
        return 2
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, on_sigterm)
    if shutil.which('caffeinate'):          # keep the Mac awake while the runner is going
        subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])

    print('Follow the log with:\n\n  tail -f %s\n' % LOG, flush=True)
    out.log('START', 'pid %d; repo %s; model %s; on quota: %s; permission mode: %s'
            % (os.getpid(), a.repo, MODEL, a.on_quota, a.permission_mode))
    who = git(a.repo, 'config', 'user.name') or ''
    package = a.package

    while True:
        if take_file(STOP):
            out.log('STOPPED', 'stop file', notify=True)
            out.status('STOPPED', 'stop file')
            return 0

        resume = bool(st.get('session')) and st.get('status') in ('running', 'interrupted')
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
        summary = 'run %d: %d min, $%.2f (all runs $%.2f)' % (st['run'], mins, run.cost(),
                                                             st['total_cost_usd'])

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
