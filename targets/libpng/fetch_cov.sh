#!/bin/bash

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
##

git clone --no-checkout https://github.com/glennrp/libpng.git \
    "$TARGET_COV/repo"
git -C "$TARGET_COV/repo" checkout a37d4836519517bdce6cb9d956092321eca3e73b
