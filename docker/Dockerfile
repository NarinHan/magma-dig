FROM ubuntu:18.04

# TODO remove sudo for user "magma" to avoid unwanted priv escalation from
# other attack vectors.

RUN apt-get update && \
	apt-get install -y sudo vim python3 python3-pip gdb && \
	rm -rf /var/lib/apt/lists/*

# Install redis
RUN pip3 install redis
RUN pip3 install dataclasses

## Magma directory hierarchy
# magma_root is relative to the docker-build's working directory
# The Docker image must be built in the root of the magma directory
ARG magma_root=./

## Path variables inside the container
ENV MAGMA_R /magma
ENV OUT		/magma_out
ENV OUT_COV /magma_out_cov
ENV SHARED 	/magma_shared

ENV CC  /usr/bin/gcc
ENV CXX /usr/bin/g++
ENV LD /usr/bin/ld
ENV AR /usr/bin/ar
ENV AS /usr/bin/as
ENV NM /usr/bin/nm
ENV RANLIB /usr/bin/ranlib

ARG USER_ID=1000
ARG GROUP_ID=1000
RUN mkdir -p /home && \
	groupadd -g ${GROUP_ID} magma && \
	useradd -l -u ${USER_ID} -K UMASK=0000 -d /home -g magma magma && \
	chown magma:magma /home
RUN	echo "magma:amgam" | chpasswd && usermod -a -G sudo magma

RUN mkdir -p ${SHARED} ${OUT} ${OUT_COV} && \
	chown magma:magma ${SHARED} ${OUT} ${OUT_COV} && \
	chmod 744 ${SHARED} ${OUT} ${OUT_COV}

ENV MAGCOV  ${MAGMA_R}/magcov
USER root:root
RUN mkdir -p ${MAGCOV} && chown magma:magma ${MAGCOV}
COPY --chown=magma:magma ${magma_root}/magcov ${MAGCOV}/

ARG magma_path=magma
ENV MAGMA 	${MAGMA_R}/${magma_path}
USER root:root
RUN mkdir -p ${MAGMA} && chown magma:magma ${MAGMA}
COPY --chown=magma:magma ${magma_root}/${magma_path} ${MAGMA}/
RUN ${MAGMA}/preinstall.sh
USER magma:magma
RUN ${MAGMA}/prebuild.sh

ARG fuzzer_name
ARG fuzzer_path=fuzzers/${fuzzer_name}
ENV FUZZER 	${MAGMA_R}/${fuzzer_path}
USER root:root
RUN mkdir -p ${FUZZER} && chown magma:magma ${FUZZER}
COPY --chown=magma:magma ${magma_root}/${fuzzer_path} ${FUZZER}/
RUN ${FUZZER}/preinstall.sh
USER magma:magma
RUN ${FUZZER}/fetch.sh
RUN ${FUZZER}/build.sh

ARG target_name
ARG target_path=targets/${target_name}
ENV TARGET 	${MAGMA_R}/${target_path}
USER root:root
RUN mkdir -p ${TARGET} && chown magma:magma ${TARGET}
COPY --chown=magma:magma ${magma_root}/${target_path} ${TARGET}/
RUN ${TARGET}/preinstall.sh
USER magma:magma
RUN ${TARGET}/fetch.sh
RUN ${MAGMA}/apply_patches.sh

## Configuration parameters
ARG isan
ARG harden
ARG canaries
ARG fixes

ARG ISAN_FLAG=${isan:+-DMAGMA_FATAL_CANARIES}
ARG HARDEN_FLAG=${harden:+-DMAGMA_HARDEN_CANARIES}
ARG CANARIES_FLAG=${canaries:+-DMAGMA_ENABLE_CANARIES}
ARG FIXES_FLAG=${fixes:+-DMAGMA_ENABLE_FIXES}
ARG BUILD_FLAGS="-include ${MAGMA}/src/canary.h ${CANARIES_FLAG} ${FIXES_FLAG} ${ISAN_FLAG} ${HARDEN_FLAG} -g -O0"

ENV CFLAGS ${BUILD_FLAGS}
ENV CXXFLAGS ${BUILD_FLAGS}
ENV LIBS -l:magma.o -lrt
ENV LDFLAGS -L"${OUT}" -L"${OUT_COV}"  -g

RUN ${FUZZER}/instrument.sh

## Use llvm_cov to measure coverage
ENV FUZZER_COV ${MAGMA_R}/fuzzers/llvm_cov
USER root:root
RUN mkdir -p ${FUZZER_COV} && chown magma:magma ${FUZZER_COV}
COPY --chown=magma:magma ${magma_root}/fuzzers/llvm_cov ${FUZZER_COV}/
RUN ${FUZZER_COV}/preinstall.sh
USER magma:magma
RUN ${FUZZER_COV}/fetch.sh

## Build target program for coverage measurement
ENV TARGET_COV ${MAGMA_R}/${target_path}_cov
USER root:root
RUN mkdir -p ${TARGET_COV} && chown magma:magma ${TARGET_COV}
COPY --chown=magma:magma ${magma_root}/${target_path} ${TARGET_COV}/
RUN ${TARGET_COV}/preinstall.sh
USER magma:magma
RUN ${TARGET_COV}/fetch_cov.sh
RUN ${MAGMA}/apply_patches_cov.sh

ENTRYPOINT "${MAGMA}/run.sh"
