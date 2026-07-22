## How To Use

Replace `--redis-url` with the correct redis url.
Replace `--namespace` with the corresponding namespace from config file.

Run `./run.sh`

## Script Descriptions 

The container-side call order is:

```text
process_execution_jobs.py
  -> manage_redis_jobs.py
    -> execute_job.py
         -> process_coverage.py
```

| Script | Work |
| --- | --- |
| `process_execution_jobs.py` | Redis worker loop; claims jobs and starts the per-job executor. |
| `manage_redis_jobs.py`      | Redis key and status helpers imported by the worker. |
| `execute_job.py`            | Runs the generator, target binary, coverage tools, or an integrated `runtime_probe` job. |
| `process_coverage.py`       | Processes coverage inside the container. |


## Script for Runtime Feedback

| Script | Work |
| --- | --- |
| `run_gdb.py` | Reads probe requests and prior generated inputs, runs GDB, and writes runtime observations. |

`execute_job.py` also has an integrated `runtime_probe` mode. 
The current manual workflow uses `run_gdb.py`; 
these are two alternative mechanisms, not two steps that must both run.
