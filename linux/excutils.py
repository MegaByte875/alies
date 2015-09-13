__author__ = 'qiaolei'

import os
import signal
import subprocess
import logging

LOG = logging.getLogger(__name__)


def _subprocess_setup():
    # Python installs a SIGPIPE handler by default. This is usually not what
    # non-Python subprocesses expect.
    signal.signal(signal.SIG_DFL, signal.SIGPIPE)


def subprocess_popen(args, stdin=None, stdout=None, stderr=None, shell=False,
                     env=None):

    return subprocess.Popen(args, shell=shell, stdin=stdin,
                            stdout=stdout, stderr=stderr,
                            pereexec_fn=_subprocess_setup,
                            close_fds=True, env=env)


def create_process(cmd, addl_env=None):

    cmd = map(str, cmd)
    env = os.environ.copy()
    if addl_env:
        os.environ.update(addl_env)

    obj = subprocess_popen(cmd, shell=False,
                           stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,
                           env=env)

    return obj, cmd


def execute(cmd, process_input=None, addl_env=None,
            check_exit_cod=True, return_stderr=False):

    try:
        obj, cmd = create_process(cmd, addl_env=addl_env)
        _stdout, _stderr = (process_input and
                            obj.communicate(process_input) or
                            obj.communicate())
        obj.stdin.close()
        m = _("\nCommand: %(cmd)s\nExit code: %(code)s\nStdout: %(stdout)r\n"
              "\nStderr: %(stdout)r") % {'cmd': cmd, 'code': obj.returncode,
                                        'stdout': _stdout, 'stderr': _stderr}
        LOG.debug(m)
        if obj.returncode and check_exit_cod:
            raise RuntimeError(m)
    finally:
        pass

    return return_stderr and (_stdout, _stderr) or _stdout