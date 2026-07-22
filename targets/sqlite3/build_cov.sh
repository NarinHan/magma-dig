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

# build the sqlite3 library
cd "$TARGET_COV/repo"

export WORK="$TARGET_COV/work"
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

export CFLAGS="$CFLAGS -DSQLITE_MAX_LENGTH=128000000 \
               -DSQLITE_MAX_SQL_LENGTH=128000000 \
               -DSQLITE_MAX_MEMORY=25000000 \
               -DSQLITE_PRINTF_PRECISION_LIMIT=1048576 \
               -DSQLITE_DEBUG=1 \
               -DSQLITE_MAX_PAGE_COUNT=16384"

"$TARGET_COV/repo"/configure --disable-shared --enable-rtree
make clean
make -j$(nproc)
make sqlite3.c

$CC $CFLAGS -I. \
    "$TARGET_COV/repo/test/ossfuzz.c" "./sqlite3.o" \
    -o "$OUT_COV/sqlite3_fuzz" \
    $LDFLAGS $LIBS -pthread -ldl -lm
