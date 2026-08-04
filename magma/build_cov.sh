#!/bin/bash
set -e

##
# Pre-requirements:
# - env MAGMA: path to Magma support files
# - env OUT_COV: path to directory where artifacts are stored
# - env SHARED: path to directory shared with host (to store results)
##

MAGMA_STORAGE="$SHARED/canaries.raw"

$CC $CFLAGS -D"MAGMA_STORAGE=\"$MAGMA_STORAGE\"" -c "$MAGMA/src/canary.c" \
    -fPIC -I "$MAGMA/src/" -o "$OUT_COV/canary.o" $LDFLAGS

$CC $CFLAGS -D"MAGMA_STORAGE=\"$MAGMA_STORAGE\"" -c "$MAGMA/src/storage.c" \
    -fPIC -I "$MAGMA/src/" -o "$OUT_COV/storage.o" $LDFLAGS

$LD -r "$OUT_COV/canary.o" "$OUT_COV/storage.o" -o "$OUT_COV/magma.o"
rm "$OUT_COV/canary.o" "$OUT_COV/storage.o"
