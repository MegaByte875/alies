__author__ = 'qiaolei'

# To ignore a signal, register SIG_IGN as the handler. This script replaces the default
# handler for SIGINT with SIG_IGN and registers a handler for SIGUSR1 . Then it uses
# signal.pause() to wait for a signal to be received.

import signal
import os
import time


def do_exit(sig, stack):
    raise SystemExit('Exiting')

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGUSR1, do_exit)

print 'My PID:', os.getpid()

signal.pause()