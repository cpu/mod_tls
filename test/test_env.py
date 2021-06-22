import inspect
import json
import logging
import os
import re
import subprocess
import sys
import time

from configparser import ConfigParser
from datetime import timedelta, datetime
from http.client import HTTPConnection
from typing import List, Optional, Dict, Tuple, Union
from urllib.parse import urlparse

from test_cert import TlsTestCA, CertificateSpec, Credentials

log = logging.getLogger(__name__)


class ExecResult:

    def __init__(self, exit_code: int, stdout: str, stderr: str = None, duration: timedelta = None):
        self._exit_code = exit_code
        self._stdout = stdout if stdout is not None else ""
        self._stderr = stderr if stderr is not None else ""
        self._duration = duration if duration is not None else timedelta()
        # noinspection PyBroadException
        try:
            self._json_out = json.loads(self._stdout)
        except:
            self._json_out = None

    @property
    def exit_code(self) -> int:
        return self._exit_code

    @property
    def stdout(self) -> str:
        return self._stdout

    @property
    def json(self) -> Optional[Dict]:
        """Output as JSON dictionary or None if not parseable."""
        return self._json_out

    @property
    def stderr(self) -> str:
        return self._stderr

    @property
    def duration(self) -> timedelta:
        return self._duration


class TlsCipher:

    def __init__(self, id: int, name: str, flavour: str,
                 min_version: float, max_version: float = None,
                 openssl: str = None):
        self.id = id
        self.name = name
        self.flavour = flavour
        self.min_version = min_version
        self.max_version = max_version if max_version is not None else self.min_version
        if openssl is None:
            if name.startswith('TLS13_'):
                openssl = re.sub(r'^TLS13_', 'TLS_', name)
            else:
                openssl = re.sub(r'^TLS_', '', name)
                openssl = re.sub(r'_WITH_([^_]+)_', r'_\1_', openssl)
                openssl = re.sub(r'_AES_(\d+)', r'_AES\1', openssl)
                openssl = re.sub(r'(_POLY1305)_\S+$', r'\1', openssl)
                openssl = re.sub(r'_', '-', openssl)
        self.openssl_name = openssl
        self.id_name = "TLS_CIPHER_0x{0:04x}".format(self.id)

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.name


class TlsTestEnv:

    DOMAIN_A = "a.mod-tls.test"
    DOMAIN_B = "b.mod-tls.test"

    # current rustls supported ciphers in their order of preference
    # used to test cipher selection, see test_06_ciphers.py
    RUSTLS_CIPHERS = [
        TlsCipher(0x1303, "TLS13_CHACHA20_POLY1305_SHA256", "CHACHA", 1.3),
        TlsCipher(0x1302, "TLS13_AES_256_GCM_SHA384", "AES", 1.3),
        TlsCipher(0x1301, "TLS13_AES_128_GCM_SHA256", "AES", 1.3),
        TlsCipher(0xcca9, "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256", "ECDSA", 1.2),
        TlsCipher(0xcca8, "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256", "RSA", 1.2),
        TlsCipher(0xc02c, "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384", "ECDSA", 1.2),
        TlsCipher(0xc02b, "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256", "ECDSA", 1.2),
        TlsCipher(0xc030, "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384", "RSA", 1.2),
        TlsCipher(0xc02f, "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256", "RSA", 1.2),
    ]

    def __init__(self):
        our_dir = os.path.dirname(inspect.getfile(TlsTestEnv))
        config = ConfigParser()
        config.read(os.path.join(our_dir, 'test.ini'))
        self._prefix = config.get('global', 'prefix')
        self._gen_dir = os.path.join(our_dir, 'gen')
        self._ca = None
        self._server_dir = os.path.join(self._gen_dir, 'apache')
        self._server_conf_dir = os.path.join(self._server_dir, "conf")
        self._server_docs_dir = os.path.join(self._server_dir, "htdocs")
        self._server_error_log = os.path.join(self._server_dir, "logs", "error_log")
        self._mpm_type = os.environ['MPM'] if 'MPM' in os.environ else 'event'

        self._apachectl = os.path.join(self._prefix, 'bin', 'apachectl')
        self._apxs = os.path.join(self._prefix, 'bin', 'apxs')
        self._httpd = os.path.join(self._prefix, 'bin', 'httpd')
        self._http_port = int(config.get('global', 'http_port'))
        self._https_port = int(config.get('global', 'https_port'))

        self._http_base = "http://127.0.0.1:{port}".format(port=self._http_port)
        self._httpd_check_url = self._http_base
        self._https_base = "https://127.0.0.1:{port}".format(port=self._https_port)

        self._curl = config.get('global', 'curl_bin')
        if self._curl is None or len(self._curl) == 0:
            self._curl = "curl"
        self._openssl = config.get('global', 'openssl_bin')
        self.apache_error_log_clear()

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def mpm_type(self) -> str:
        return self._mpm_type

    @property
    def http_port(self) -> int:
        return self._http_port

    @property
    def https_port(self) -> int:
        return self._https_port

    @property
    def http_base_url(self) -> str:
        return self._http_base

    @property
    def https_base_url(self) -> str:
        return self._https_base

    @property
    def gen_dir(self) -> str:
        return self._gen_dir

    @property
    def server_dir(self) -> str:
        return self._server_dir

    @property
    def server_conf_dir(self) -> str:
        return self._server_conf_dir

    @property
    def server_docs_dir(self) -> str:
        return self._server_docs_dir

    @property
    def domain_a(self) -> str:
        return self.DOMAIN_A

    @property
    def domain_b(self) -> str:
        return self.DOMAIN_B

    @property
    def ca(self) -> Credentials:
        return self._ca

    def set_ca(self, ca: Credentials):
        self._ca = ca

    @staticmethod
    def run(args: List[str]) -> ExecResult:
        log.debug("run: {0}", " ".join(args))
        start = datetime.now()
        p = subprocess.run(args, capture_output=True, text=True)
        # noinspection PyBroadException
        return ExecResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr,
                          duration=datetime.now() - start)

    # --------- HTTP ---------

    def is_live(self, url: str = None, timeout: timedelta = None):
        url = url if url else self._httpd_check_url
        server = urlparse(url)
        timeout = timeout if timeout is not None else timedelta(seconds=20)
        try_until = time.time() + timeout.total_seconds()
        log.debug("checking is reachable: {url}".format(url=url))
        while time.time() < try_until:
            # noinspection PyBroadException
            try:
                c = HTTPConnection(server.hostname, server.port, timeout=timeout.total_seconds())
                c.request('HEAD', server.path)
                _resp = c.getresponse()
                c.close()
                return True
            except ConnectionRefusedError:
                log.debug("connection refused")
                time.sleep(.1)
            except:
                log.warning(f"Unexpected error: {sys.exc_info()[0]}", )
                time.sleep(.1)
        log.warning("Unable to contact server after {timeout} sec".format(timeout=timeout))
        return False

    def is_dead(self, url: str = None, timeout: timedelta = None):
        url = url if url else self._httpd_check_url
        server = urlparse(url)
        timeout = timeout if timeout is not None else timedelta(seconds=20)
        try_until = time.time() + timeout.total_seconds()
        log.debug("checking is unreachable: {url}".format(url=url))
        while time.time() < try_until:
            # noinspection PyBroadException
            try:
                c = HTTPConnection(server.hostname, server.port, timeout=timeout.total_seconds())
                c.request('HEAD', server.path)
                _resp = c.getresponse()
                c.close()
                time.sleep(.1)
            except IOError:
                return True
            except:
                return True
        log.warning(f"Server still responding after {timeout} sec".format(timeout=timeout))
        return False

    def get_httpd_version(self):
        p = subprocess.run([self._apxs, "-q", "HTTPD_VERSION"], capture_output=True, text=True)
        if p.returncode != 0:
            return "unknown"
        return p.stdout.strip()

    # --------- control apache ---------

    def httpd(self):
        args = [self._httpd, "-d", self._server_dir]
        p = subprocess.run(args, capture_output=True, text=True)
        rv = p.returncode
        return rv

    def apachectl(self, cmd, check_live=True):
        args = [self._apachectl, "-d", self._server_dir, "-k", cmd]
        p = subprocess.run(args, capture_output=True, text=True)
        rv = p.returncode
        if rv == 0:
            timeout = timedelta(seconds=10)
            if check_live:
                rv = 0 if self.is_live(timeout=timeout) else -1
                if rv != 0:
                    log.warning("apache did not start: {0}".format(p.stderr))
            else:
                rv = 0 if self.is_dead(timeout=timeout) else -1
                log.debug("waited for a apache.is_dead, rv=%d" % rv)
        else:
            log.warning("exit %d, stderr: %s" % (rv, p.stderr))
        return rv

    def apache_restart(self):
        return self.apachectl("graceful")

    def apache_start(self):
        return self.apachectl("start")

    def apache_stop(self):
        return self.apachectl("stop", check_live=False)

    def apache_fail(self):
        rv = self.apachectl("graceful", check_live=False)
        if rv != 0:
            log.warning("graceful restart returned: {0}".format(rv))
            return 0 if self.is_dead(timeout=timedelta(seconds=5)) else -1
        return rv

    def apache_try_start(self):
        args = [self._apachectl, "-d", self._server_dir, "-k", "start"]
        return self.run(args)

    def apache_error_log_clear(self):
        if os.path.isfile(self._server_error_log):
            os.remove(self._server_error_log)

    RE_MD_RESET = re.compile(r'.*\[tls:info].*initializing\.\.\.')
    RE_MD_ERROR = re.compile(r'.*\[tls:error].*')
    RE_MD_WARN = re.compile(r'.*\[tls:warn].*')

    def apache_error_log_count(self):
        ecount = 0
        wcount = 0

        if os.path.isfile(self._server_error_log):
            fin = open(self._server_error_log)
            for line in fin:
                m = self.RE_MD_ERROR.match(line)
                if m:
                    ecount += 1
                    continue
                m = self.RE_MD_WARN.match(line)
                if m:
                    wcount += 1
                    continue
                m = self.RE_MD_RESET.match(line)
                if m:
                    ecount = 0
                    wcount = 0
        return ecount, wcount

    def curl(self, args: List[str]) -> ExecResult:
        return self.run([self._curl] + args)

    def https_get(self, domain, paths: Union[str, List[str]], extra_args: List[str] = None) -> ExecResult:
        args = []
        if extra_args:
            args.extend(extra_args)
        args.extend(["--cacert", self.ca.cert_file, "--resolve", "{domain}:{port}:127.0.0.1".format(
            domain=domain, port=self.https_port
        )])
        if isinstance(paths, str):
            paths = [paths]
        for path in paths:
            args.append("https://{domain}:{port}{path}".format(
                domain=domain, port=self.https_port, path=path))
        return self.curl(args)

    def https_get_json(self, domain, path, extra_args: List[str] = None):
        r = self.https_get(domain=domain, paths=path, extra_args=extra_args)
        assert r.exit_code == 0, r.stderr
        return r.json

    def run_diff(self, fleft: str, fright: str) -> ExecResult:
        return self.run(['diff', '-u', fleft, fright])

    def openssl(self, args: List[str]) -> ExecResult:
        return self.run([self._openssl] + args)

    def openssl_client(self, domain, extra_args: List[str] = None) -> ExecResult:
        args = ["s_client", "-CAfile", self.ca.cert_file, "-servername", domain,
                "-connect", "localhost:{port}".format(
                    port=self.https_port
                )]
        if extra_args:
            args.extend(extra_args)
        args.extend([])
        return self.openssl(args)

    CURL_SUPPORTS_TLS_1_3 = None

    def curl_supports_tls_1_3(self) -> bool:
        if self.CURL_SUPPORTS_TLS_1_3 is None:
            r = self.https_get(self.domain_a, "/index.json", extra_args=["--tlsv1.3"])
            self.CURL_SUPPORTS_TLS_1_3 = r.exit_code == 0
        return self.CURL_SUPPORTS_TLS_1_3

    OPENSSL_SUPPORTED_PROTOCOLS = None

    @staticmethod
    def openssl_supports_tls_1_3() -> bool:
        if TlsTestEnv.OPENSSL_SUPPORTED_PROTOCOLS is None:
            env = TlsTestEnv()
            r = env.openssl(args=["ciphers", "-v"])
            protos = set()
            ciphers = set()
            for line in r.stdout.splitlines():
                m = re.match(r'^(\S+)\s+(\S+)\s+(.*)$', line)
                if m:
                    ciphers.add(m.group(1))
                    protos.add(m.group(2))
            TlsTestEnv.OPENSSL_SUPPORTED_PROTOCOLS = protos
            TlsTestEnv.OPENSSL_SUPPORTED_CIPHERS = ciphers
        return "TLSv1.3" in TlsTestEnv.OPENSSL_SUPPORTED_PROTOCOLS
