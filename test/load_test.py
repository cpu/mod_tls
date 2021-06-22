import argparse
import logging
import multiprocessing
import os
import re
import sys
import time
from datetime import timedelta, datetime
from threading import Thread
from tqdm import tqdm  # type: ignore
from typing import Dict, Iterable, List, Tuple, Optional

from test_cert import TlsTestCA, CertificateSpec
from test_conf import TlsTestConf
from test_env import TlsTestEnv, ExecResult

log = logging.getLogger(__name__)


class LoadTestException(Exception):
    pass


class H2LoadLogSummary:

    @staticmethod
    def from_file(fpath: str, title: str, duration: timedelta) -> 'H2LoadLogSummary':
        with open(fpath) as fd:
            return H2LoadLogSummary.from_lines(fd.readlines(), title=title, duration=duration)

    @staticmethod
    def from_lines(lines: Iterable[str], title: str, duration: timedelta) -> 'H2LoadLogSummary':
        stati = {}
        count = 0
        all_durations = timedelta(milliseconds=0)
        for line in lines:
            parts = re.split(r'\s+', line)  # start(us), status(int), duration(ms), tbd.
            if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
                count += 1
                status = int(parts[1])
                if status in stati:
                    stati[status] += 1
                else:
                    stati[status] = 1
                all_durations += timedelta(microseconds=int(parts[2]))
            else:
                sys.stderr.write("unrecognize log line: {0}".format(line))
        return H2LoadLogSummary(title=title, total=count, stati=stati,
                                duration=duration, all_durations=all_durations)

    def __init__(self, title: str, total: int, stati: Dict[int, int],
                 duration: timedelta, all_durations: timedelta):
        self._title = title
        self._total = total
        self._stati = stati
        self._duration = duration
        self._all_durations = all_durations
        self._transfered_mb = 0.0
        self._exec_result = None
        self._expected_responses = 0

    @property
    def title(self) -> str:
        return self._title

    @property
    def response_count(self) -> int:
        return self._total

    @property
    def duration(self) -> timedelta:
        return self._duration

    @property
    def response_durations(self) -> timedelta:
        return self._all_durations

    @property
    def response_stati(self) -> Dict[int, int]:
        return self._stati

    @property
    def expected_responses(self) -> int:
        return self._expected_responses

    @property
    def execution(self) -> ExecResult:
        return self._exec_result

    def all_200(self) -> bool:
        non_200s = [n for n in self._stati.keys() if n != 200]
        return len(non_200s) == 0

    @property
    def throughput_mb(self) -> float:
        if self._transfered_mb > 0.0:
            return self._transfered_mb / self.duration.total_seconds()
        return 0.0

    def set_transfered_mb(self, mb: float) -> None:
        self._transfered_mb = mb

    def set_exec_result(self, result: ExecResult):
        self._exec_result = result

    def set_expected_responses(self, n: int):
        self._expected_responses = n

    def get_footnote(self) -> Optional[str]:
        notes = []
        note = ""
        if self.expected_responses > 0 and \
                self.response_count != self.expected_responses:
            note += "{0}/{1} missing".format(
                self.expected_responses - self.response_count,
                self.expected_responses
            )
        if not self.all_200():
            note += ", non 200s:"
            for status in [n for n in self.response_stati.keys() if n != 200]:
                note += " {0}={1}".format(status, self.response_stati[status])
        return note if len(note) else None


class H2LoadMonitor:

    def __init__(self, fpath: str, expected: int, title: str):
        self._fpath = fpath
        self._expected = expected
        self._title = title
        self._tqdm = tqdm(desc=title, total=expected, unit="request", leave=False)
        self._running = False
        self._lines = ()
        self._tail = None

    def start(self):
        self._tail = Thread(target=self._collect, kwargs={'self': self})
        self._running = True
        self._tail.start()

    def get_summary(self, duration: timedelta) -> H2LoadLogSummary:
        self._running = False
        self._tail.join()
        return H2LoadLogSummary.from_file(self._fpath, title=self._title, duration=duration)

    def stop(self):
        self._running = False

    @staticmethod
    def _collect(self) -> None:
        first_call = True
        while self._running:
            try:
                with open(self._fpath) as fd:
                    if first_call:
                        fd.seek(0, 2)
                        first_call = False
                    latest_data = fd.read()
                    while self._running:
                        if '\n' not in latest_data:
                            latest_data += fd.read()
                            if '\n' not in latest_data:
                                if not os.path.isfile(self._fpath):
                                    break
                                time.sleep(0.1)
                                continue
                        lines = latest_data.split('\n')
                        if lines[-1] != '\n':
                            latest_data = lines[-1]
                            lines = lines[:-1]
                        else:
                            latest_data = None
                        self._tqdm.update(n=len(lines))
                        if latest_data is None:
                            latest_data = fd.read()
            except IOError:
                time.sleep(0.1)
        self._tqdm.close()


def mk_text_file(fpath: str, lines: int):
    t110 = ""
    for _ in range(11):
        t110 += "0123456789"
    with open(fpath, "w") as fd:
        for i in range(lines):
            fd.write("{0:015d}: ".format(i))  # total 128 bytes per line
            fd.write(t110)
            fd.write("\n")


class LoadTestCase:

    @staticmethod
    def from_scenario(scenario: Dict, env: TlsTestEnv) -> 'SingleFileLoadTest':
        raise NotImplemented

    def run(self) -> H2LoadLogSummary:
        raise NotImplemented

    def format_result(self, summary: H2LoadLogSummary) -> str:
        raise NotImplemented

    def setup_base_conf(self, env: TlsTestEnv, worker_count: int = 5000) -> TlsTestConf:
        conf = TlsTestConf(env=env)
        # ylavic's formula
        process_count = int(max(10, min(100, int(worker_count / 100))))
        thread_count = int(max(25, worker_count / process_count))
        conf.add(f"""
        StartServers             1
        ServerLimit              {int(process_count * 2.5)}
        ThreadLimit              {thread_count}
        ThreadsPerChild          {thread_count}
        MinSpareThreads          {thread_count}
        MaxSpareThreads          {int(worker_count / 2)}
        MaxRequestWorkers        {worker_count}
        MaxConnectionsPerChild   0
        KeepAliveTimeout         60
        MaxKeepAliveRequests     0
                """)
        return conf

    def start_server(self, cd: timedelta = None):
        if self.env.apache_stop() == 0 and cd:
            with tqdm(desc="connection cooldown", total=int(cd.total_seconds()), unit="s", leave=False) as t:
                end = datetime.now() + cd
                while datetime.now() < end:
                    time.sleep(1)
                    t.update()
        assert self.env.apache_start() == 0


class SingleFileLoadTest(LoadTestCase):

    def __init__(self, env: TlsTestEnv, server: str,
                 clients: int, requests: int, resource_kb: int,
                 ssl_module: str = 'mod_tls', protocol: str = 'h2',
                 threads: int = None):
        self.env = env
        self.domain_a = self.env.domain_a
        self._server = server
        self._clients = clients
        self._requests = requests
        self._resource_kb = resource_kb
        self._ssl_module = ssl_module
        self._protocol = protocol
        self._threads = threads if threads is not None else min(multiprocessing.cpu_count()/2, self._clients)

    @staticmethod
    def from_scenario(scenario: Dict, env: TlsTestEnv) -> 'SingleFileLoadTest':
        return SingleFileLoadTest(
            env=env,
            server=scenario['server'],
            clients=scenario['clients'], requests=scenario['requests'],
            ssl_module=scenario['module'], resource_kb=scenario['rsize'],
            protocol=scenario['protocol'] if 'protocol' in scenario else 'h2'
        )

    def _setup(self) -> str:
        conf = self.setup_base_conf(env=self.env)
        extras = {
            'base': self._server
        }
        if 'mod_tls' == self._ssl_module:
            conf.add_vhosts(domains=[self.domain_a], extras=extras)
        elif 'mod_ssl' == self._ssl_module:
            conf.add_ssl_vhosts(domains=[self.domain_a], extras=extras)
        else:
            raise LoadTestException("tests for module: {0}".format(self._ssl_module))
        conf.write()
        docs_a = os.path.join(self.env.server_docs_dir, self.domain_a)
        fname = "{0}k.txt".format(self._resource_kb)
        mk_text_file(os.path.join(docs_a, fname), 8 * self._resource_kb)
        self.start_server()
        return "/{0}".format(fname)

    def _teardown(self):
        if self.env.is_live(timeout=timedelta(milliseconds=100)):
            assert self.env.apache_stop() == 0

    def run_test(self, mode: str, path: str) -> H2LoadLogSummary:
        monitor = None
        try:
            log_file = "{gen_dir}/h2load.log".format(gen_dir=self.env.gen_dir)
            if os.path.isfile(log_file):
                os.remove(log_file)
            monitor = H2LoadMonitor(log_file, expected=self._requests,
                                    title=f"{self._ssl_module}/{self._protocol}/"\
                                          f"{self._clients}c/{self._resource_kb / 1024}MB[{mode}]")
            monitor.start()
            args = [
                'h2load',
                '--clients={0}'.format(self._clients),
                '--threads={0}'.format(self._threads),
                '--requests={0}'.format(self._requests),
                '--log-file={0}'.format(log_file),
                '--connect-to=localhost:{0}'.format(self.env.https_port)
            ]
            if self._protocol == 'h1' or self._protocol == 'http/1.1':
                args.append('--h1')
            elif self._protocol == 'h2':
                args.extend(['-m', "6"])
            else:
                raise Exception(f"unknown protocol: {self._protocol}");
            r = self.env.run(args + [
                'https://{0}:{1}{2}'.format(self.domain_a, self.env.https_port, path)
            ])
            if r.exit_code != 0:
                raise LoadTestException("h2load returned {0}: {1}".format(r.exit_code, r.stderr))
            summary = monitor.get_summary(duration=r.duration)
            summary.set_expected_responses(self._requests)
            summary.set_exec_result(r)
            summary.set_transfered_mb(self._requests * self._resource_kb / 1024)
            return summary
        finally:
            if monitor is not None:
                monitor.stop()

    def run(self) -> H2LoadLogSummary:
        path = self._setup()
        try:
            self.run_test(mode="warmup", path=path)
            return self.run_test(mode="measure", path=path)
        finally:
            self._teardown()

    def format_result(self, summary: H2LoadLogSummary) -> Tuple[str, List[str]]:
        return "{0:.1f}".format(summary.throughput_mb), summary.get_footnote()


class MultiFileLoadTest(LoadTestCase):

    SETUP_DONE = False

    def __init__(self, env: TlsTestEnv, server: str,
                 clients: int, requests: int, file_count: int,
                 file_sizes: List[int],
                 ssl_module: str = 'mod_tls', protocol: str = 'h2',
                 threads: int = None, ):
        self.env = env
        self.domain_a = self.env.domain_a
        self._server = server
        self._clients = clients
        self._requests = requests
        self._file_count = file_count
        self._file_sizes = file_sizes
        self._ssl_module = ssl_module
        self._protocol = protocol
        self._threads = threads if threads is not None else \
            min(multiprocessing.cpu_count()/2, self._clients)
        self._url_file = "{gen_dir}/h2load-urls.txt".format(gen_dir=self.env.gen_dir)

    @staticmethod
    def from_scenario(scenario: Dict, env: TlsTestEnv) -> 'MultiFileLoadTest':
        return MultiFileLoadTest(
            env=env,
            server=scenario['server'],
            clients=scenario['clients'], requests=scenario['requests'],
            file_sizes=scenario['file_sizes'], file_count=scenario['file_count'],
            ssl_module=scenario['module'], protocol=scenario['protocol']
        )

    def _setup(self, cls):
        conf = self.setup_base_conf(env=self.env)
        extras = {
            'base': self._server
        }
        if 'mod_tls' == self._ssl_module:
            conf.add_vhosts(domains=[self.domain_a], extras=extras)
        elif 'mod_ssl' == self._ssl_module:
            conf.add_ssl_vhosts(domains=[self.domain_a], extras=extras)
        else:
            raise LoadTestException("tests for module: {0}".format(self._ssl_module))
        conf.write()
        if not cls.SETUP_DONE:
            with tqdm(desc="setup resources", total=self._file_count, unit="file", leave=False) as t:
                docs_a = os.path.join(self.env.server_docs_dir, self.domain_a)
                uris = []
                for i in range(self._file_count):
                    fsize = self._file_sizes[i % len(self._file_sizes)]
                    if fsize is None:
                        raise Exception("file sizes?: {0} {1}".format(i, fsize))
                    fname = "{0}-{1}k.txt".format(i, fsize)
                    mk_text_file(os.path.join(docs_a, fname), 8 * fsize)
                    uris.append(f"/{fname}")
                    t.update()
                with open(self._url_file, 'w') as fd:
                    fd.write("\n".join(uris))
                    fd.write("\n")
            cls.SETUP_DONE = True
        self.start_server()

    def _teardown(self):
        if self.env.is_live(timeout=timedelta(milliseconds=100)):
            assert self.env.apache_stop() == 0

    def run_test(self, mode: str, path: str) -> H2LoadLogSummary:
        monitor = None
        try:
            log_file = "{gen_dir}/h2load.log".format(gen_dir=self.env.gen_dir)
            if os.path.isfile(log_file):
                os.remove(log_file)
            monitor = H2LoadMonitor(log_file, expected=self._requests,
                                    title=f"{self._ssl_module}/{self._protocol}/"\
                                          f"{self._file_count / 1024}f/{self._clients}c[{mode}]")
            monitor.start()
            args = [
                'h2load',
                '--clients={0}'.format(self._clients),
                '--requests={0}'.format(self._requests),
                '--input-file={0}'.format(self._url_file),
                '--log-file={0}'.format(log_file),
                '--connect-to=localhost:{0}'.format(self.env.https_port)
            ]
            if self._protocol == 'h1' or self._protocol == 'http/1.1':
                args.append('--h1')
            elif self._protocol == 'h2':
                args.extend(['-m', "6"])
            else:
                raise Exception(f"unknown protocol: {self._protocol}");
            r = self.env.run(args + [
                '--base-uri=https://{0}:{1}/'.format(
                    self.domain_a, self.env.https_port)
            ])
            if r.exit_code != 0:
                raise LoadTestException("h2load returned {0}: {1}".format(r.exit_code, r.stderr))
            summary = monitor.get_summary(duration=r.duration)
            summary.set_expected_responses(self._requests)
            summary.set_exec_result(r)
            return summary
        finally:
            if monitor is not None:
                monitor.stop()

    def run(self) -> H2LoadLogSummary:
        path = self._setup(self.__class__)
        try:
            # self.run_test(mode="warmup", path=path)
            return self.run_test(mode="measure", path=path)
        finally:
            self._teardown()

    def format_result(self, summary: H2LoadLogSummary) -> str:
        return "{0:.1f}".format(
            summary.response_count / summary.duration.total_seconds()
        ), summary.get_footnote()


class ConnectionLoadTest(LoadTestCase):

    SETUP_DONE = False

    def __init__(self, env: TlsTestEnv, server: str,
                 clients: int, requests: int, duration: timedelta,
                 file_count: int, file_sizes: List[int], cooldown: timedelta,
                 ssl_module: str = 'mod_tls', protocol: str = 'h2'):
        self.env = env
        self.domain_a = self.env.domain_a
        self._server = server
        self._clients = clients
        self._requests = requests
        self._duration = duration
        self._file_count = file_count
        self._file_sizes = file_sizes
        self._ssl_module = ssl_module
        self._protocol = protocol
        self._url_file = "{gen_dir}/h2load-urls.txt".format(gen_dir=self.env.gen_dir)
        self._cd = cooldown

    @staticmethod
    def from_scenario(scenario: Dict, env: TlsTestEnv) -> 'MultiFileLoadTest':
        return ConnectionLoadTest(
            env=env,
            server=scenario['server'],
            clients=scenario['clients'], requests=scenario['requests'],
            duration=scenario['duration'], cooldown=scenario['cooldown'],
            file_sizes=scenario['file_sizes'], file_count=scenario['file_count'],
            ssl_module=scenario['module'], protocol=scenario['protocol']
        )

    def _setup(self):
        conf = self.setup_base_conf(env=self.env)
        extras = {
            'base': self._server
        }
        if 'mod_tls' == self._ssl_module:
            conf.add_vhosts(domains=[self.domain_a], extras=extras)
        elif 'mod_ssl' == self._ssl_module:
            conf.add_ssl_vhosts(domains=[self.domain_a], extras=extras)
        else:
            raise LoadTestException("tests for module: {0}".format(self._ssl_module))
        conf.write()
        if not ConnectionLoadTest.SETUP_DONE:
            with tqdm(desc="setup resources", total=self._file_count, unit="file", leave=False) as t:
                docs_a = os.path.join(self.env.server_docs_dir, self.domain_a)
                uris = []
                for i in range(self._file_count):
                    fsize = self._file_sizes[i % len(self._file_sizes)]
                    if fsize is None:
                        raise Exception("file sizes?: {0} {1}".format(i, fsize))
                    fname = "{0}-{1}k.txt".format(i, fsize)
                    mk_text_file(os.path.join(docs_a, fname), 8 * fsize)
                    uris.append(f"/{fname}")
                    t.update()
                with open(self._url_file, 'w') as fd:
                    fd.write("\n".join(uris))
                    fd.write("\n")
            ConnectionLoadTest.SETUP_DONE = True
        self.start_server(cd=self._cd)

    def _teardown(self):
        if self.env.is_live(timeout=timedelta(milliseconds=100)):
            assert self.env.apache_stop() == 0

    def run_test(self, mode: str, path: str) -> H2LoadLogSummary:
        monitor = None
        try:
            log_file = "{gen_dir}/h2load.log".format(gen_dir=self.env.gen_dir)
            if os.path.isfile(log_file):
                os.remove(log_file)
            monitor = H2LoadMonitor(log_file, expected=0,
                                    title=f"{self._ssl_module}/{self._protocol}/"\
                                          f"{self._clients}c/{self._duration.total_seconds()}s")
            monitor.start()
            args = [
                'h2load',
                '--clients={0}'.format(self._clients),
                '--requests={0}'.format(self._requests * self._clients),
                '--input-file={0}'.format(self._url_file),
                '--log-file={0}'.format(log_file),
                '--connect-to=localhost:{0}'.format(self.env.https_port)
            ]
            if self._protocol == 'h1' or self._protocol == 'http/1.1':
                args.append('--h1')
            elif self._protocol == 'h2':
                args.extend(['-m', "6"])
            else:
                raise Exception(f"unknown protocol: {self._protocol}")
            args += [
                '--base-uri=https://{0}:{1}/'.format(
                    self.domain_a, self.env.https_port)
            ]
            end = datetime.now() + self._duration
            while datetime.now() < end:
                r = self.env.run(args)
                if r.exit_code != 0:
                    raise LoadTestException("h2load returned {0}: {1}".format(r.exit_code, r.stderr))
            summary = monitor.get_summary(duration=self._duration)
            summary.set_exec_result(r)
            return summary
        finally:
            if monitor is not None:
                monitor.stop()

    def run(self) -> H2LoadLogSummary:
        path = self._setup()
        try:
            return self.run_test(mode="measure", path=path)
        finally:
            self._teardown()

    def format_result(self, summary: H2LoadLogSummary) -> str:
        return "{0:.1f}".format(
            summary.response_count / summary.duration.total_seconds() / self._requests
        ), summary.get_footnote()


class LoadTest:

    @staticmethod
    def print_table(table: List[List[str]], foot_notes: List[str] = None):
        col_widths = []
        col_sep = "   "
        for row in table[1:]:
            for idx, cell in enumerate(row):
                if idx >= len(col_widths):
                    col_widths.append(len(cell))
                else:
                    col_widths[idx] = max(len(cell), col_widths[idx])
        row_len = sum(col_widths) + (len(col_widths) * len(col_sep))
        print(f"{' '.join(table[0]):^{row_len}}")
        for row in table[1:]:
            line = ""
            for idx, cell in enumerate(row):
                line += f"{col_sep if idx > 0 else ''}{cell:>{col_widths[idx]}}"
            print(line)
        if foot_notes is not None:
            for idx, note in enumerate(foot_notes):
                print("{0:3d}) {1}".format(idx+1, note))

    @staticmethod
    def scenario_with(base: Dict, updates: Dict) -> Dict:
        scenario = base.copy()
        scenario.update(updates)
        return scenario

    @classmethod
    def main(cls):
        parser = argparse.ArgumentParser(prog='load_h1', description="""
            Run a range of load tests against the test Apache setup.
            """)
        parser.add_argument("-m", "--module", type=str, default=None,
                            help="which module to test, defaults to all")
        parser.add_argument("-p", "--protocol", type=str, default=None,
                            help="which protocols to test, defaults to all")
        parser.add_argument("-v", "--verbose", action='count', default=0,
                            help="log more output on stderr")
        parser.add_argument("names", nargs='*', help="Name(s) of scenarios to run")
        args = parser.parse_args()

        if args.verbose > 0:
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
            logging.getLogger('').addHandler(console)

        try:
            log.debug("starting tests")

            server_config = """
        LogLevel tls:warn
        LogLevel ssl:warn
        Protocols h2 http/1.1
                """

            scenario_sf = {
                "title": "sizes and throughput (MB/s)",
                "class": SingleFileLoadTest,
                "server": server_config,
                "clients": 0,
                "row0_title": "module protocol",
                "row_title": "{module} {protocol}",
                "rows": [
                    {"module": "mod_ssl", "protocol": 'h1'},
                    {"module": "mod_tls", "protocol": 'h1'},
                    {"module": "mod_ssl", "protocol": 'h2'},
                    {"module": "mod_tls", "protocol": 'h2'},
                ],
                "col_title": "{rsize}KB",
                "columns": [],
            }
            scenario_mf = {
                "title": "connections and throughput (MB/s)",
                "class": MultiFileLoadTest,
                "server": server_config,
                "file_count": 1024,
                "file_sizes": [1, 2, 3, 4, 5, 10, 20, 30, 40, 50, 100, 10000],
                "requests": 10000,
                "row0_title": "module protocol",
                "row_title": "{module} {protocol}",
                "rows": [
                    {"module": "mod_ssl", "protocol": 'h1'},
                    {"module": "mod_tls", "protocol": 'h1'},
                    {"module": "mod_ssl", "protocol": 'h2'},
                    {"module": "mod_tls", "protocol": 'h2'},
                ],
                "col_title": "{clients}c",
                "columns": [],
            }
            scenario_conn = {
                "title": "connections",
                "class": ConnectionLoadTest,
                "server": server_config,
                "duration": timedelta(seconds=10),
                "cooldown": timedelta(seconds=5),
                "file_count": 12,
                "file_sizes": [1, 2, 3, 4, 5, 10, 20, 30, 40, 50, 100, 10000],
                "requests": 1,
                "clients": 1,
                "row0_title": "module protocol",
                "row_title": "{module} {http}",
                "rows": [
                    {"module": "mod_ssl", "protocol": 'h1'},
                    {"module": "mod_tls", "protocol": 'h1'},
                    {"module": "mod_ssl", "protocol": 'h2'},
                    {"module": "mod_tls", "protocol": 'h2'},
                ],
                "col_title": "{clients}c",
                "columns": [],
            }

            scenarios = {
                "1c-throughput": cls.scenario_with(scenario_sf, {
                    "title": "1 conn, 1k-10k requests, *sizes, throughput (MB/s)",
                    "clients": 1,
                    "columns": [
                        {"requests": 1000, "rsize": 10 * 1024},
                        {"requests": 3000, "rsize": 1024},
                        {"requests": 6000, "rsize": 100},
                        {"requests": 10000, "rsize": 10},
                    ],
                }),
                "10c-throughput": cls.scenario_with(scenario_sf, {
                    "title": "10 conn, 5k-50k requests, *sizes, throughput (MB/s)",
                    "clients": 10,
                    "columns": [
                        {"requests": 5000, "rsize": 10 * 1024},
                        {"requests": 10000, "rsize": 1024},
                        {"requests": 25000, "rsize": 100},
                        {"requests": 50000, "rsize": 10},
                    ],
                }),
                "20c-throughput": cls.scenario_with(scenario_sf, {
                    "title": "20 conn, 5k-50k requests, *sizes, throughput (MB/s)",
                    "clients": 20,
                    "columns": [
                        {"requests": 5000, "rsize": 10 * 1024},
                        {"requests": 10000, "rsize": 1024},
                        {"requests": 25000, "rsize": 100},
                        {"requests": 50000, "rsize": 10},
                    ],
                }),
                "50c-throughput": cls.scenario_with(scenario_sf, {
                    "title": "50 conn, 10k-100k requests, *sizes, throughput (MB/s)",
                    "clients": 50,
                    "columns": [
                        {"requests": 5000, "rsize": 10 * 1024},
                        {"requests": 10000, "rsize": 1024},
                        {"requests": 50000, "rsize": 100},
                        {"requests": 100000, "rsize": 10},
                    ],
                }),
                "1k-files": cls.scenario_with(scenario_mf, {
                    "title": "1k files, 1k-10MB, *conn, 10k req, (req/s)",
                    "clients": 1,
                    "columns": [
                        {"clients": 1},
                        {"clients": 2},
                        {"clients": 4},
                        {"clients": 8},
                        {"clients": 16},
                        {"clients": 32},
                        {"clients": 64},
                    ],
                }),
                "1m-reqs": cls.scenario_with(scenario_mf, {
                    "title": "1m requests, 1k files, 1k-10MB, (req/s)",
                    "clients": 1,
                    "requests": 1000000,
                    "columns": [
                        {"clients": 1},
                        {"clients": 4},
                        {"clients": 16},
                        {"clients": 64},
                    ],
                }),
                "conn-scale": cls.scenario_with(scenario_conn, {
                    "title": "c parallel clients, 1 req/c (conn/s)",
                    "requests": 1,
                    "duration": timedelta(seconds=30),
                    "cooldown": timedelta(seconds=10),
                    "columns": [
                        {"clients": 1},
                        {"clients": 2},
                        {"clients": 4},
                        {"clients": 8},
                        {"clients": 16},
                        {"clients": 32},
                    ],
                }),
                "conn-limits": cls.scenario_with(scenario_conn, {
                    "title": "c parallel clients, 1 req/c (conn/s)",
                    "requests": 1,
                    "duration": timedelta(seconds=10),
                    "cooldown": timedelta(seconds=30),
                    "columns": [
                        {"clients": 64},
                        {"clients": 128},
                        {"clients": 256},
                        {"clients": 512},
                    ],
                }),
            }
            for name in args.names:
                if name not in scenarios:
                    raise LoadTestException(f"scenario unknown: '{name}'")
            names = args.names if len(args.names) else sorted(scenarios.keys())

            env = TlsTestEnv()
            cert_specs = [
                CertificateSpec(domains=[env.domain_a]),
                CertificateSpec(domains=[env.domain_b], key_type='secp256r1', single_file=True),
                CertificateSpec(domains=[env.domain_b], key_type='rsa4096'),
            ]
            ca = TlsTestCA.create_root(name="abetterinternet-mod_tls",
                                       store_dir=os.path.join(env.server_dir, 'ca'), key_type="rsa4096")
            ca.issue_certs(cert_specs)
            env.set_ca(ca)
            
            for name in names:
                scenario = scenarios[name]
                table = [
                    [scenario['title']],
                ]
                foot_notes = []
                headers = [scenario['row0_title']]
                for col in scenario['columns']:
                    headers.append(scenario['col_title'].format(**col))
                table.append(headers)
                cls.print_table(table)
                for row in scenario['rows']:
                    if args.module is not None and row['module'] != args.module:
                        continue
                    if args.protocol is not None and row['protocol'] != args.protocol:
                        continue
                    row_line = [scenario['row_title'].format(**row)]
                    table.append(row_line)
                    for col in scenario['columns']:
                        t = scenario.copy()
                        t.update(row)
                        t.update(col)
                        test = scenario['class'].from_scenario(t, env=env)
                        env.apache_error_log_clear()
                        summary = test.run()
                        result, fnote = test.format_result(summary)
                        if fnote:
                            foot_notes.append(fnote)
                        row_line.append("{0}{1}".format(result,
                            f"[{len(foot_notes)}]" if fnote else ""
                        ))
                        cls.print_table(table, foot_notes)
        except KeyboardInterrupt:
            sys.exit(1)
        except LoadTestException as ex:
            sys.stderr.write(f"ERROR: {str(ex)}\n")
            sys.exit(1)
        sys.exit(0)


if __name__ == "__main__":
    LoadTest.main()
