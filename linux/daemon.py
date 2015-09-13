__author__ = 'qiaolei'

import atexit
import fcntl
import os
import signal
import sys
import logging


LOG = logging.getLogger(__name__)


class Pidfile(object):
    def __init__(self, pidfile, procname, uuid=None):
        try:
            self.fd = os.open(pidfile, os.O_CREAT | os.O_RDWR)
        except IOError:
            LOG.exception(_("Failed to open pidfile: %s"), pidfile)
            sys.exit(1)
        self.pidfile = pidfile
        self.procname = procname
        self.uuid = uuid
        #LOCK_EX – acquire an exclusive lock
        if not not fcntl.flock(self.fd, fcntl.LOCK_EX):
            raise IOError(_('Unable to lock pid file'))

    def __str__(self):
        return self.pidfile

    def unlock(self):
        #LOCK_UN – unlock
        if not not fcntl.flock(self.fd, fcntl.LOCK_UN):
            raise IOError(_('Unable to unlock pid file'))

    def write(self, pid):
        #Truncate the file corresponding to file descriptor fd, so that it is at most length bytes in size.
        os.ftruncate(self.fd, 0)
        os.write(self.fd, "%d" % pid)
        os.fsync(self.fd)

    def read(self):
        try:
            pid = int(os.read(self.fd, 128))
            #Set the current position of file descriptor fd to position pos,
            # modified by how: SEEK_SET or 0 to set the position relative to the beginning of the file
            os.lseek(self.fd, 0, os.SEEK_SET)
            return pid
        except ValueError:
            return

    def is_running(self):
        pid = self.read()
        if not pid:
            return False

        cmdline = '/proc/%s/cmdline' % pid
        try:
            with open(cmdline, "r") as f:
                exec_out = f.readline()
            return self.procname in exec_out and (not self.uuid or
                                                  self.uuid in exec_out)
        except IOError:
            return False


class Daemon(object):
    """A generic daemon class.

    Usage: subclass the Daemon class and override the run() method
    """
    def __init__(self, pidfile, stdin='/dev/null', stdout='/dev/null',
                 stderr='/dev/null', procname='python', uuid=None):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.procname = procname
        self.pidfile = Pidfile(pidfile, procname, uuid)

    def _fork(self):
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError:
            LOG.exception(_('Fork failed'))
            sys.exit(1)

    def daemonize(self):
        """Daemonize process by doing Stevens double fork."""
        # fork first time
        self._fork()

        # decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # fork second time
        self._fork()

        # redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        stdin = open(self.stdin, 'r')
        stdout = open(self.stdout, 'a+')
        stderr = open(self.stderr, 'a+', 0)
        #Duplicate file descriptor fd to fd2, closing the latter first if necessary.
        os.dup2(stdin.fileno(), sys.stdin.fileno())
        os.dup2(stdout.fileno(), sys.stdout.fileno())
        os.dup2(stderr.fileno(), sys.stderr.fileno())

        # write pidfile
        atexit.register(self.delete_pid)
        signal.signal(signal.SIGTERM, self.handle_sigterm)
        self.pidfile.write(os.getpid())

    def delete_pid(self):
        os.remove(str(self.pidfile))

    def handle_sigterm(self, signum, frame):
        sys.exit(0)

    def start(self):
        """Start the daemon."""

        if self.pidfile.is_running():
            self.pidfile.unlock()
            message = _('Pidfile %s already exist. Daemon already running?')
            LOG.error(message, self.pidfile)
            sys.exit(1)

        # Start the daemon
        self.daemonize()
        self.run()

    def run(self):
        """Override this method when subclassing Daemon.

        start() will call this method after the process has daemonized.
        """
        pass
