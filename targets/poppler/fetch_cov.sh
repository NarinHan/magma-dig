#!/bin/bash

##
# Pre-requirements:
# - env TARGET_COV: path to target work dir
##

git clone --no-checkout https://gitlab.freedesktop.org/poppler/poppler.git \
    "$TARGET_COV/repo"
git -C "$TARGET_COV/repo" checkout 1d23101ccebe14261c6afc024ea14f29d209e760

git clone --no-checkout https://gitlab.freedesktop.org/freetype/freetype.git \
	"$TARGET_COV/freetype2"
git -C "$TARGET_COV/freetype2" checkout 50d0033f7ee600c5f5831b28877353769d1f7d48
