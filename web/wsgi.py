__author__ = 'qiaolei'

import errno
import logging
import os
import random
import signal
import socket
import ssl
import sys
import time

try:
    # Importing just the symbol here because the io module does not
    # exist in Python 2.6.
    from io import UnsupportedOperation  # noqa
except ImportError:
    # Python 2.6
    UnsupportedOperation = None

import eventlet.wsgi
eventlet.patcher.monkey_patch(all=False, socket=True, thread=True)
from eventlet import event
from oslo_config import cfg
from paste import deploy

from threadgroup import ThreadGroup

LOG = logging.getLogger(__name__)


socket_opts = [
    cfg.IntOpt('backlog',
               default=4096,
               help=_("Number of backlog requests to configure "
                      "the socket with")),
    cfg.IntOpt('tcp_keepidle',
               default=600,
               help=_("Sets the value of TCP_KEEPIDLE in seconds for each "
                      "server socket. Not supported on OS X.")),
    cfg.IntOpt('retry_until_window',
               default=30,
               help=_("Number of seconds to keep retrying to listen")),
    cfg.IntOpt('max_header_line',
               default=16384,
               help=_("Max header line to accommodate large tokens")),
    cfg.BoolOpt('use_ssl',
                default=False,
                help=_('Enable SSL on the API server')),
    cfg.StrOpt('ssl_ca_file',
               help=_("CA certificate file to use to verify "
                      "connecting clients")),
    cfg.StrOpt('ssl_cert_file',
               help=_("Certificate file to use when starting "
                      "the server securely")),
    cfg.StrOpt('ssl_key_file',
               help=_("Private key file to use when starting "
                      "the server securely")),
    cfg.BoolOpt('wsgi_keep_alive',
                default=True,
                help=_("Determines if connections are allowed to be held "
                     "open by clients after a request is fulfilled. A value "
                     "of False will ensure that the socket connection will "
                     "be explicitly closed once a response has been sent to "
                     "the client.")),
    cfg.IntOpt('client_socket_timeout', default=900,
               help=_("Timeout for client connections socket operations. "
                    "If an incoming connection is idle for this number of "
                    "seconds it will be closed. A value of '0' means "
                    "wait forever.")),
]

service_opts = [
    cfg.IntOpt('api_workers',
               default=0,
               help=_('Number of separate API worker processes for service')),
]
CONF = cfg.CONF
CONF.register_opts(service_opts)
CONF.register_opts(socket_opts)


def _sighup_supported():
    return hasattr(signal, 'SIGHUP')


def _is_daemon():
    # The process group for a foreground process will match the
    # process group of the controlling terminal. If those values do
    # not match, or ioctl() fails on the stdout file handle, we assume
    # the process is running in the background as a daemon.
    # http://www.gnu.org/software/bash/manual/bashref.html#Job-Control-Basics
    try:
        is_daemon = os.getpgrp() != os.tcgetpgrp(sys.stdout.fileno())
    except OSError as err:
        if err.errno == errno.ENOTTY:
            # Assume we are a daemon because there is no terminal.
            is_daemon = True
        else:
            raise
    except UnsupportedOperation:
        # Could not get the fileno for stdout, so we must be a daemon.
        is_daemon = True
    return is_daemon


def _is_sighup_and_daemon(signo):
    if not (_sighup_supported() and signo == signal.SIGHUP):
        # Avoid checking if we are a daemon, because the signal isn't
        # SIGHUP.
        return False
    return _is_daemon()


def _signo_to_signame(signo):
    signals = {signal.SIGTERM: 'SIGTERM',
               signal.SIGINT: 'SIGINT'}
    if _sighup_supported():
        signals[signal.SIGHUP] = 'SIGHUP'
    return signals[signo]


def _set_signals_handler(handler):
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    if _sighup_supported():
        signal.signal(signal.SIGHUP, handler)


class WritableLogger(object):
    """A thin wrapper that responds to `write` and logs."""

    def __init__(self, logger, level=logging.INFO):
        self.logger = logger
        self.level = level

    def write(self, msg):
        self.logger.log(self.level, msg.rstrip())


class WorkerService(object):
    """Wraps a worker to be handled by ProcessLauncher"""
    def __init__(self, service, application):
        self._service = service
        self._application = application
        self._server = None

    def start(self):
        self._server = self._service.pool.spawn(self._service._run,
                                                self._application,
                                                self._service._socket)

    def wait(self):
        if isinstance(self._server, eventlet.greenthread.GreenThread):
            self._server.wait()

    def stop(self):
        if isinstance(self._server, eventlet.greenthread.GreenThread):
            self._server.kill()
            self._server = None


class Server(object):
    """Server class to manage multiple WSGI sockets and applications."""
    def __init__(self, name, threads=1000):
        eventlet.wsgi.MAX_HEADER_LINE = CONF.max_header_line
        self.pool = eventlet.GreenPool(threads)
        self.name = name
        self._server = None
        self.client_socket_timeout = CONF.client_socket_timeout

    def _get_socket(self, host, port, backlog):
        bind_addr = (host, port)
        try:
            info = socket.getaddrinfo(bind_addr[0],
                                      backlog[1],
                                      socket.AF_UNSPEC,
                                      socket.SOCK_STREAM)[0]
            family = info[0]
            bind_addr = info[-1]
        except Exception:
            LOG.exception(_("Unable to listen on %(host)s:%(port)s"),
                          {'host': host, 'port': port})
            sys.exit(1)

        if CONF.use_ssl:
            if not os.path.exists(CONF.ssl_cert_file):
                raise RuntimeError(_("Unable to find ssl cert file "
                                   ": %s") % CONF.ssl_cert_file)
            if CONF.ssl_key_file and not os.path.exists(CONF.ssl_key_file):
                raise RuntimeError(_("Unable to find ssl key file "
                                   ": %s") % CONF.ssl_key_file)
            if CONF.ssl_ca_file and not os.path.exists(CONF.ssl_ca_file):
                raise RuntimeError(_("Unable to find ssl ca file "
                                   ": %s") % CONF.ssl_ca_file)

        def wrap_ssl(sock):
            ssl_kwargs = {
                'server_side': True,
                'certfile': CONF.ssl_cert_file,
                'keyfile': CONF.ssl_key_file,
                'cert_reqs': ssl.CERT_NONE
            }

            if CONF.ssl_ca_file:
                ssl_kwargs['ca_certs'] = CONF.ssl_ca_file
                ssl_kwargs['cert_reqs'] = ssl.CERT_REQUIRED

            return ssl.wrap_socket(sock, **ssl_kwargs)

        sock = None
        retry_until = time.time() + CONF.retry_until_window
        while not sock and time.time() < retry_until:
            try:
                sock = eventlet.listen(bind_addr,
                                       backlog=backlog,
                                       family=family)
                if CONF.use_ssl:
                    sock = wrap_ssl(sock)
            except socket.error as err:
                if err.errno == errno.EADDRINUSE:
                    eventlet.sleep(0.1)
        if not sock:
            raise RuntimeError(_("Could not bind to %(host)s:%(port)s "
                               "after trying for %(time)d seconds") %
                               {'host': host,
                                'port': port,
                                'time': CONF.retry_until_window})
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP,
                            socket.TCP_KEEPIDLE,
                            CONF.tcp_keepidle)
            return sock

    def start(self, application, port, host='0.0.0.0', workers=2):
        self._host = host
        self._port = port
        backlog = CONF.backlog
        self._socket = self._get_socket(self._host,
                                        self._port,
                                        backlog)
        self._launch(application, workers)

    def _launch(self, application, workers=2):
        service = WorkerService(self, application)
        if workers < 1:
            self._server = service
            service.start()
            notify_once()
        else:
            self._server = ProcessLauncher(wait_interval=0.1)
            self._server.launch_service(service, workers=workers)

    @property
    def host(self):
        return self._socket.getsockname()[0] if self._socket else self._host

    @property
    def port(self):
        return self._socket.getsockname()[1] if self._socket else self._port

    def stop(self):
        self._server.stop()

    def wait(self):
        try:
            self._server.wait()
        except KeyboardInterrupt:
            pass

    def _run(self, application, socket):
        eventlet.wsgi.Server(socket, application, custom_pool=self.pool,
                             log=WritableLogger(LOG), keepalive=CONF.wsgi_keep_alive,
                             socket_timeout=self.client_socket_timeout)


class ServiceWrapper(object):

    def __init__(self, service, workers):
        self.service = service
        self.workers = workers
        self.children = set()
        self.forktimes = []


class SignalExit(SystemExit):

    def __init__(self, signo, exccode=1):
        super(SignalExit, self).__init__(exccode)
        self.signo = signo


class Services(object):

    def __init__(self):
        self.services = []
        self.tg = ThreadGroup()
        # signal that the service is done shutting itself down:
        self.done = event.Event()

    def add(self, service):
        self.services.append(service)
        self.tg.add_thread(self.run_service, service, self.done)

    def stop(self):
        for service in self.services:
            service.stop()
            service.wait()

        if not self.done.ready():
            self.done.send()

        self.tg.stop()

    def wait(self):
        self.tg.wait()

    def restart(self):
        self.stop()
        self.done = event.Event()
        for restart_service in self.services:
            restart_service.reset()
            self.tg.add_thread(self.run_service, restart_service, self.done)

    @staticmethod
    def run_service(service, done):
        """Service start wrapper.
        :param service: service to run
        :param done: event to wait on until a shutdown is triggered
        :returns: None

        """
        service.start()
        done.wait()


class Launcher(object):
    """Launch one or more services and wait for them to complete."""
    def __init__(self):
        """Initialize the service launcher.

        :returns: None

        """
        self.services = Services()

    def launch_service(self, service):
        """Load and start the given service.
        :param service: The service you would like to start.
        :returns: None

        """
        self.services.add(service)

    def stop(self):
        """Stop all services which are currently running.

        :returns: None

        """
        self.services.stop()

    def wait(self):
        """Waits until all services have been stopped, and then returns.

        :returns: None

        """
        self.services.wait()

    def restart(self):
        """Reload config files and restart service.

        :returns: None

        """
        cfg.CONF.reload_config_files()
        self.services.restart()


class ProcessLauncher(object):

    def __init__(self, wait_interval=0.01):
        self.children = {}
        self.sigcaught = None
        self.running = True
        self.wait_interval = wait_interval
        rfd, self.writepipe = os.pipe()
        self.readpipe = eventlet.greenio.GreenPipe(rfd, 'r')
        self.handle_signal()

    def handle_signal(self):
        _set_signals_handler(self._handle_signal)

    def _handle_signal(self, signo, frame):
        self.sigcaught = signo
        self.running = False
        _set_signals_handler(signal.SIG_DFL)

    def _pipe_watcher(self):
        self.readpipe.read()
        LOG.info(_("Parent process has died unexpectedly, exiting"))
        sys.exit(1)

    def _child_process_handle_signal(self):

        def _sigterm(*args):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            raise SignalExit(signal.SIGTERM)

        def _sighup(*args):
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            raise SignalExit(signal.SIGTERM)

        signal.signal(signal.SIGTERM, _sigterm)
        if _sighup_supported():
            signal.signal(signal.SIGHUP, _sighup)

        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def _child_wait_for_exit_or_signal(self, launcher):
        status = 0
        signo = 0
        try:
            launcher.wait()
        except SignalExit as exc:
            signame = _signo_to_signame(exc.signo)
            LOG.info(_("Child caught %s, exiting"), signame)
            status = exc.code
            signo = exc.signo
        except SystemExit as exc:
            status = exc.code
        except BaseException:
            LOG.exception(_("Unhandled exception"))
        finally:
            launcher.stop()
        return status, signo

    def _child_process(self, service):
        self._child_process_handle_signal()

        # Reopen the eventlet hub to make sure we don't share an epoll
        # fd with parent and/or siblings, which would be bad
        eventlet.hubs.use_hub()

        # Close write to ensure only parent has it open
        os.close(self.writepipe)
        eventlet.spawn_n(self._pipe_watcher)

        random.seed()

        launcher = Launcher()
        launcher.launch_service(service)
        return launcher

    def _start_child(self, wrap):
        if len(wrap.forktimes) > wrap.workers:
            if time.time() - wrap.forktimes[0] < wrap.workers:
                LOG.info(_("Foking too fast, sleeping"))
                time.sleep(1)

            wrap.forktimes.pop(0)

        wrap.forktimes.append(time.time())

        pid = os.fork()
        if pid == 0:
            launcher = self._child_process(wrap.service)
            while True:
                self._child_process_handle_signal()
                status, signo = self._child_wait_for_exit_or_signal(launcher)
                if not _is_sighup_and_daemon(signo):
                    break
                launcher.restart()

            os._exit(status)

        LOG.info(_("Started child %s"), pid)

        wrap.children.add(pid)
        self.children[pid] = wrap

        return pid

    def launch_service(self, service, workers=2):
        wrap = ServiceWrapper(service, workers)

        LOG.info(_("Starting %d workers"), wrap.workers)
        while self.running and len(wrap.workers) < workers:
            self._start_child(wrap)

    def _wait_child(self):
        try:
            pid, status = os.waitpid(0, os.WNOHANG)
            if not pid:
                return None
        except OSError as exc:
            if exc.errno not in (errno.EINTR, errno.ECHILD):
                raise
            return None

        if os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            LOG.info(_("Child %(pid)d killed by signal %(sig)d"),
                     dict(pid=pid, sig=sig))
        else:
            code = os.WEXITSTATUS(status)
            LOG.info(_("Child %(pid)s exited with status %(code)d"),
                     dict(pid=pid, code=code))

        if pid not in self.children:
            LOG.warning(_("pid %d not in child list"), pid)

        wrap = self.children.pop(pid)
        wrap.children.remove(pid)
        return wrap

    def _respawn_children(self):
        while self.running:
            wrap = self._wait_child()
            if not wrap:
                eventlet.greenlet.sleep(self.wait_interval)
                continue
            while self.running and len(wrap.children) < wrap.workers:
                self._start_child(wrap)

    def wait(self):
        notify_once()
        try:
            while True:
                self.handle_signal()
                self._respawn_children()
                if not self.sigcaught:
                    return

                signame = _signo_to_signame(self.sigcaught)
                LOG.info(_("Caught %s, stopping children"), signame)
                if not _is_sighup_and_daemon(self.sigcaught):
                    break

                for pid in self.children:
                    os.kill(pid, signal.SIGHUP)
                self.running = True
                self.sigcaught = None
        except eventlet.greenlet.GreenletExit:
            LOG.info(_("Wait called after thread killed. Cleaning up."))

        self.stop()

    def stop(self):
        self.running = False
        for pid in self.children:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                #errno.ESRCH, No such process
                if exc.errno != errno.ESRCH:
                    raise

        if self.children:
            LOG.info(_("Waiting on %d children to exit"), len(self.children))
            while self.children:
                self._wait_child()


class WsgiService(object):
    """Base class for WSGI based services."""
    def __init__(self, app_name):
        self.app_name = app_name
        self.wsgi_app = None

    def start(self):
        self.wsgi_app = _run_wsgi(self.app_name)

    def wait(self):
        self.wsgi_app.wait()


class PlutoApiService(WsgiService):
    """Class for api service."""
    @classmethod
    def create(cls, app_name='pluto'):
        service = cls(app_name)
        return service


def serve_wsgi(cls):
    try:
        service = cls.create()
        service.start()
    except Exception:
        LOG.exception(_("Unrecoverable error: please check log for details"))
    return service


def _run_wsgi(app_name):
    app = load_paste_app(app_name)
    if not app:
        LOG.exception(_("No known API applications configured."))
        return
    server = Server("Pluto")
    server.start(app, cfg.CONF.bind_port, cfg.CONF.bind_host,
                 workers=cfg.CON.api_workers)
    LOG.info(_("Pluto service started, listening on %(host)s:%(port)s"),
            {'host': cfg.CONF.bind_host,
             'port': cfg.CONF.bind_port})
    return server


def load_paste_app(app_name):
    """Builds and returns a WSGI app from a paste config file.

    :param app_name: Name of the application to load
    :raises ConfigFilesNotFoundError when config file cannot be located
    :raises RuntimeError when application cannot be loaded from config file
    """

    config_path = cfg.CONF.find_file(cfg.CONF.api_paste_config)
    if not config_path:
        raise cfg.ConfigFilesNotFoundError(
            config_files=[cfg.CONF.api_paste_config])
    config_path = os.path.abspath(config_path)
    LOG.info(_("Config paste file: %s"), config_path)

    try:
        app = deploy.loadapp("config:%s" % config_path, name=app_name)
    except (LookupError, ImportError):
        msg = (_("Unable to load %(app_name)s from "
                 "configuration file %(config_path)s.") %
               {'app_name': app_name,
                'config_path': config_path})
        LOG.exception(msg)
        raise RuntimeError(msg)
    return app


def main():
    try:
        pool = eventlet.GreenPool()

        pluto_api = serve_wsgi(PlutoApiService)
        api_thread = pool.spawn(pluto_api.wait)
        pool.waitall()
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        sys.exit(_("ERROR: %s") % e)


if __name__ == "__main__":
    main()