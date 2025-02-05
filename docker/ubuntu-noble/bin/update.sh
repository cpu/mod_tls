#!/bin/bash

TOP=/abetterinternet
DATADIR=$TOP/data
RUSTLS_VERSION=0.14.0

fail() {
    exit 1
}

source /opt/venv/bin/activate

mkdir -p $DATADIR
mkdir -p "$TOP/mod_tls"

cd "$TOP/mod_tls" || fail
cp -r /abetterinternet/mod_tls/* .

cd $DATADIR
rm -rf rustls-ffi
git clone https://github.com/rustls/rustls-ffi.git rustls-ffi
cd rustls-ffi
git fetch origin
git checkout "tags/v$RUSTLS_VERSION"

make CFLAGS="" DESTDIR=$TOP/rustls-ffi/build/rust CRYPTO_PROVIDER=ring install || fail

cd "$TOP/mod_tls" ||fail
autoreconf -fi || fail
./configure --with-rustls=$TOP/rustls-ffi/build/rust || fail
make V=1 || fail

pytest -v
