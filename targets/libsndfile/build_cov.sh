#!/bin/bash
set -e

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
# - env OUT_COV: path to directory where artifacts are stored
# - env CC, CXX, FLAGS, LIBS, etc...
##

if [ ! -d "$TARGET_COV/repo" ]; then
    echo "fetch.sh must be executed first."
    exit 1
fi

cd "$TARGET_COV/repo"
./autogen.sh
./configure --disable-shared --enable-ossfuzzers
make -j$(nproc) clean
make -j$(nproc) ossfuzz/sndfile_fuzzer

cp -v ossfuzz/sndfile_fuzzer $OUT_COV/
