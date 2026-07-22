#!/bin/bash

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
##

git clone --no-checkout https://github.com/libsndfile/libsndfile.git \
    "$TARGET_COV/repo"
git -C "$TARGET_COV/repo" checkout 86c9f9eb7022d186ad4d0689487e7d4f04ce2b29
