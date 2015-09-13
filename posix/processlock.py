__author__ = 'qiaolei'

import errno
import shutil
import os
import tempfile
import time
import threading
import logging
import functools
import weakref

from eventlet import semaphore


LOG = logging.getLogger(__name__)


def ensure_tree(path):
    """Create a directory (and any ancestor directories required)

    :param path: Directory to create
    """
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            if not os.path.isdir(path):
                raise
        else:
            raise


class WeakLocal(threading.local):
    def __getattribute__(self, attr):
        rval = super(WeakLocal, self).__getattribute__(attr)
        if rval:
            # NOTE(mikal): this bit is confusing. What is stored is a weak
            # reference, not the value itself. We therefore need to lookup
            # the weak reference and return the inner value here.
            rval = rval()
        return rval

    def __setattr__(self, attr, value):
        value = weakref.ref(value)
        return super(WeakLocal, self).__setattr__(attr, value)


# NOTE(mikal): the name "store" should be deprecated in the future
store = WeakLocal()

# A "weak" store uses weak references and allows an object to fall out of scope
# when it falls out of scope in the code that uses the thread local storage. A
# "strong" store will hold a reference to the object so that it never falls out
# of scope.
weak_store = WeakLocal()
strong_store = threading.local()


class _InterProcessLock(object):
    """Lock implementation which allows multiple locks, working around
    issues like bugs.debian.org/cgi-bin/bugreport.cgi?bug=632857 and does
    not require any cleanup. Since the lock is always held on a file
    descriptor rather than outside of the process, the lock gets dropped
    automatically if the process crashes, even if __exit__ is not executed.

    There are no guarantees regarding usage by multiple green threads in a
    single process here. This lock works only between processes. Exclusive
    access between local threads should be achieved using the semaphores
    in the @synchronized decorator.

    Note these locks are released when the descriptor is closed, so it's not
    safe to close the file descriptor while another green thread holds the
    lock. Just opening and closing the lock file can break synchronisation,
    so lock files must be accessed only using this abstraction.
    """

    def __init__(self, name):
        self.lockfile = None
        self.fname = name

    def __enter__(self):
        self.lockfile = open(self.fname, 'w')

        while True:
            try:
                # Using non-blocking locks since green threads are not
                # patched to deal with blocking locking calls.
                # Also upon reading the MSDN docs for locking(), it seems
                # to have a laughable 10 attempts "blocking" mechanism.
                self.trylock()
                return self
            except IOError as e:
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    # external locks synchronise things like iptables
                    # updates - give it some time to prevent busy spinning
                    time.sleep(0.01)
                else:
                    raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.unlock()
            self.lockfile.close()
        except IOError:
            LOG.exception(_("Could not release the acquired lock `%s`"),
                          self.fname)

    def trylock(self):
        raise NotImplementedError()

    def unlock(self):
        raise NotImplementedError()


class _PosixLock(_InterProcessLock):
    """
    LOCK_UN - unlock
    LOCK_SH - acquire a shared lock
    LOCK_EX -acquire an exclusive lock
    When operation is LOCK_SH or LOCK_EX, it can also be bitwise ORed with LOCK_NB
    to avoid blocking on lock acquisition. If LOCK_NB is used and the lock cannot be acquired,
    an IOError will be raised and the exception will have an errno attribute set to
    EACCES or EAGAIN (depending on the operating system; for portability, check for both values).
    On at least some systems, LOCK_EX can only be used if the file descriptor refers
    to a file opened for writing
    """

    def trylock(self):
        fcntl.fcntl(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(self):
        fcntl.fcntl(self.lockfile, fcntl.LOCK_UN)


import fcntl
InterProcessLock = _PosixLock

_semaphores = weakref.WeakValueDictionary


def synchronized(name, lock_file_prefix, external=False, lock_path=None):
    """Synchronization decorator.

    Decorating a method like so::

        @synchronized('mylock')
        def foo(self, *args):
           ...

    ensures that only one thread will execute the foo method at a time.

    Different methods can share the same lock::

        @synchronized('mylock')
        def foo(self, *args):
           ...

        @synchronized('mylock')
        def bar(self, *args):
           ...

    This way only one of either foo or bar can be executing at a time.

    :param lock_file_prefix: The lock_file_prefix argument is used to provide
    lock files on disk with a meaningful prefix. The prefix should end with a
    hyphen ('-') if specified.

    :param external: The external keyword argument denotes whether this lock
    should work across multiple processes. This means that if two different
    workers both run a a method decorated with @synchronized('mylock',
    external=True), only one of them will execute at a time.

    :param lock_path: The lock_path keyword argument is used to specify a
    special location for external lock files to live. If nothing is set, then
    CONF.lock_path is used as a default.
    """

    def wrap(f):
        @functools.wraps(f)
        def inner(*args, **kwargs):
            # NOTE(soren): If we ever go natively threaded, this will be racy.
            #              See http://stackoverflow.com/questions/5390569/dyn
            #              amically-allocating-and-destroying-mutexes
            sem = _semaphores.get(name, semaphore.Semaphore())
            if name not in _semaphores:
                # this check is not racy - we're already holding ref locally
                # so GC won't remove the item and there was no IO switch
                # (only valid in greenthreads)
                _semaphores[name] = sem

            with sem:
                LOG.debug(_('Got semaphore "%(lock)s" for method '
                            '"%(method)s"...'), {'lock': name,
                                                 'method': f.__name__})

                # NOTE(mikal): I know this looks odd
                if not hasattr(strong_store, 'locks_held'):
                    strong_store.locks_held = []
                strong_store.locks_held.append(name)

                try:
                    if external:
                        LOG.debug(_('Attempting to grab file lock "%(lock)s" '
                                    'for method "%(method)s"...'),
                                  {'lock': name, 'method': f.__name__})
                        cleanup_dir = False

                        # We need a copy of lock_path because it is non-local
                        local_lock_path = lock_path
                        if not local_lock_path:
                            local_lock_path = '/tmp/'

                        if not local_lock_path:
                            cleanup_dir = True
                            local_lock_path = tempfile.mkdtemp()

                        if not os.path.exists(local_lock_path):
                            ensure_tree(local_lock_path)

                        # NOTE(mikal): the lock name cannot contain directory
                        # separators
                        safe_name = name.replace(os.sep, '_')
                        lock_file_name = '%s%s' % (lock_file_prefix, safe_name)
                        lock_file_path = os.path.join(local_lock_path,
                                                      lock_file_name)

                        try:
                            lock = InterProcessLock(lock_file_path)
                            with lock:
                                LOG.debug(_('Got file lock "%(lock)s" at '
                                            '%(path)s for method '
                                            '"%(method)s"...'),
                                          {'lock': name,
                                           'path': lock_file_path,
                                           'method': f.__name__})
                                retval = f(*args, **kwargs)
                        finally:
                            LOG.debug(_('Released file lock "%(lock)s" at '
                                        '%(path)s for method "%(method)s"...'),
                                      {'lock': name,
                                       'path': lock_file_path,
                                       'method': f.__name__})
                            # NOTE(vish): This removes the tempdir if we needed
                            #             to create one. This is used to
                            #             cleanup the locks left behind by unit
                            #             tests.
                            if cleanup_dir:
                                shutil.rmtree(local_lock_path)
                    else:
                        retval = f(*args, **kwargs)

                finally:
                    strong_store.locks_held.remove(name)

            return retval
        return inner
    return wrap