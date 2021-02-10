import os
from typing import List, Union, Dict

from test_env import TlsTestEnv


class TlsTestConf:

    def __init__(self, env: TlsTestEnv, name: str = "test.conf"):
        self.env = env
        self.name = name
        self._content = ["LogLevel tls:trace8"]

    def add(self, text: Union[List[str], str]) -> None:
        if isinstance(text, List):
            self._content.extend(text)
        else:
            self._content.append(text)

    def write(self) -> None:
        with open(os.path.join(self.env.server_conf_dir, self.name), "w") as fd:
            fd.write("\n".join(self._content))

    def add_vhosts(self, domains: List[str], extras: Dict[str, str] = None):
        extras = extras if extras is not None else {}
        self.add("""
TLSListen {https}
{extras}
        """.format(
            https=self.env.https_port,
            extras=extras['base'] if 'base' in extras else "",
        ))
        for domain in domains:
            self.add("""
    <VirtualHost *:{https}>
      ServerName {domain}
      DocumentRoot htdocs/{domain}
      TLSCertificate {domain}.cert.pem {domain}.pkey.pem
      {extras}
    </VirtualHost>
            """.format(
                https=self.env.https_port,
                domain=domain,
                extras=extras[domain] if domain in extras else ""
            ))

