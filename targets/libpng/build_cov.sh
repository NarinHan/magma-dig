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

# build the libpng library
cd "$TARGET_COV/repo"
autoreconf -f -i
./configure --with-libpng-prefix=MAGMA_ --disable-shared
make -j$(nproc) clean
make -j$(nproc) libpng16.la

cp .libs/libpng16.a "$OUT_COV/"

# build libpng_read_fuzzer.
$CXX $CXXFLAGS -std=c++11 -I. \
     contrib/oss-fuzz/libpng_read_fuzzer.cc \
     -o $OUT_COV/libpng_read_fuzzer \
     $LDFLAGS .libs/libpng16.a $LIBS -lz
