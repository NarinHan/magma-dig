#!/bin/bash

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
##

curl "https://www.sqlite.org/src/tarball/sqlite.tar.gz?r=8c432642572c8c4b" \
  -o "$OUT_COV/sqlite.tar.gz" && \
mkdir -p "$TARGET_COV/repo" && \
tar -C "$TARGET_COV/repo" --strip-components=1 -xzf "$OUT_COV/sqlite.tar.gz"
