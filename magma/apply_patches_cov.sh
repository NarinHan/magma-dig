#!/bin/bash
set -e

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
##

# TODO filter patches by target config.yaml
find "$TARGET_COV/patches/setup" "$TARGET_COV/patches/bugs" -name "*.patch" | \
while read patch; do
    echo "Applying $patch"
    name=${patch##*/}
    name=${name%.patch}
    sed "s/%MAGMA_BUG%/$name/g" "$patch" | patch -p1 -d "$TARGET_COV/repo"
done
