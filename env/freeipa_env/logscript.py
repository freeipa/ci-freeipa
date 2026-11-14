"""`logs --script`: replay the commands a test run executed, as a shell
transcript (``[host]# cmd`` followed by its output) from the console log."""
import ast
import re
import shlex

_CMD = re.compile(r'^\[ipatests\.\S*?Host\.(?P<host>[\w-]+)\.cmd\d+\] '
                  r'(?P<msg>.*)$')
_HEADER = re.compile(r'^(?P<id>\S+\.py::\S+)')
_NOISE = re.compile(r'^(Exit code: \d+$|Warning: Permanently added |'
                    r'bash: line \d+: (cd: |/root/ipatests/env\.sh))')


def _command(msg):
    """The command text of a `RUN ...` message: argv lists become a quoted
    shell line, a plain string (shell=True) is kept as is."""
    cmd = msg[4:]
    if cmd.startswith('['):
        try:
            return shlex.join(ast.literal_eval(cmd))
        except (ValueError, SyntaxError):
            pass
    return cmd


def transcript(lines, test=None, host=None):
    """Yield transcript lines for the tests whose node id contains `test`
    (all when None) and, when `host` is given, only that host."""
    wanted = test is None
    cur = None
    for line in lines:
        line = line.rstrip('\n')
        h = _HEADER.match(line)
        if h:
            wanted = test is None or test in h['id']
            if wanted:
                yield f'# == {h["id"]}'
        m = _CMD.match(line)
        if not (wanted and m) or (host and m['host'] != host):
            continue
        msg = m['msg']
        if msg.startswith('RUN '):
            cmd = _command(msg)
            if cmd.startswith('-o ') or cmd == 'true' or '/root/ipatests' in cmd:
                cur = None  # ssh keepalive / framework bookkeeping, not a test step
                continue
            cur = m['host']
            yield f'[{cur}]# {cmd}'
        elif cur and not _NOISE.match(msg):
            yield msg


def per_test(lines):
    """{node id: transcript lines} for every test in the console log."""
    out, cur = {}, None
    for line in transcript(lines):
        if line.startswith('# == '):
            cur = out.setdefault(line[5:], [])
        elif cur is not None:
            cur.append(line)
    return out
