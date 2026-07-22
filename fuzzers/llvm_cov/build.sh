#!/bin/bash
set -e

##
# Pre-requirements:
# - env FUZZER_COV: path to fuzzer work dir
##

export CC="clang"
export CXX="clang++"

# compile standalone driver
$CC $CFLAGS -c "$FUZZER_COV/src/StandaloneFuzzTargetMain.c" -fPIC \
    -o "$OUT_COV/StandaloneFuzzTargetMain.o"
