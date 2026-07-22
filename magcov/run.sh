#!/usr/bin/env bash

python3 process_execution_jobs.py \
--redis-url redis://172.17.0.1:6379/0 \
--namespace namespace-of-the-project \
--execute-script ./execute_job.py
