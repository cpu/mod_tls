FROM ubuntu:noble

ENV DEBIAN_FRONTEND=noninteractive

# Install system packages
RUN apt-get update; apt-get upgrade -y
RUN apt-get install -y \
    libtool autoconf automake pkgconf cmake apache2 apache2-dev openssl \
    curl nghttp2-client libssl-dev \
    git python3-pip python3-pytest python3-venv

# Install rust stable toolchain
RUN curl -sSf "https://sh.rustup.rs" | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN rustup default stable

# Set up a virtual env for Python requirements
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY . /abetterinternet/mod_tls/

# Install Python requirements
WORKDIR /abetterinternet/mod_tls
RUN pip3 install -r test/requirements.txt

RUN chmod +x docker/ubuntu-noble/bin/update.sh

CMD ["/bin/bash", "-c", "/abetterinternet/mod_tls/docker/ubuntu-noble/bin/update.sh"]
