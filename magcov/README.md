# Container Pipeline

The container side has one Redis worker, one job executor, and one coverage
processor:

- `run_worker.py` claims jobs, tracks Redis status, and publishes completion.
- `execute_job.py` materializes one input and dispatches by `job_kind`.
- `process_coverage.py` converts one `llvm-cov export` into line and branch
  coverage.

All three scripts use Python 3.6 syntax and avoid the external `dataclasses`
package.

The former container scripts map to these files as follows:

| Existing responsibility | Integrated location |
| --- | --- |
| Redis queue consumption and status publication from `process_execution_jobs.py` and `manage_redis_jobs.py` | `run_worker.py` |
| Generator and target execution from `execute_job.py` | `execute_job.py` |
| LLVM export processing from `process_coverage.py` | `process_coverage.py` |
| GDB probe collection from `run_gdb.py` and `collect_runtime_observations.py` | `execute_job.py` |

## Job Kinds

### Coverage

```json
{
  "schema_version": 2,
  "job_id": "run-target-turn-q01",
  "job_kind": "coverage",
  "job_dir": "/magma_shared/jobs/run-target-turn-q01",
  "project": "libsndfile",
  "target_key": "fn48012_t982__p01",
  "turn": 0,
  "candidate": "q01",
  "model": "gpt-5.4",
  "input_generation": {
    "mode": "generator_script",
    "generator_script": {
      "script_file": "/magma_shared/jobs/run-target-turn-q01/input/generator.py",
      "runner": ["python3", "{script_path}"],
      "timeout_seconds": 30,
      "output_filename": "input.bin"
    }
  },
  "execution": {
    "binary": "/magma_out_cov/sndfile_fuzzer",
    "input_mode": "file",
    "arg_template": null,
    "timeout_seconds": 30,
    "work_dir": "."
  },
  "coverage": {
    "cov_bin": "/magma_out_cov/sndfile_fuzzer",
    "llvm_profdata_bin": "llvm-profdata-9",
    "llvm_cov_bin": "llvm-cov-9",
    "processor_script": "process_coverage.py",
    "processed_format": "csv"
  },
  "retention": {
    "stdout": "on_failure",
    "stderr": "on_failure",
    "branch_coverage": false,
    "container_work_files": false
  }
}
```

The generator must create exactly the relative `output_filename` and no other
regular output file.

### Runtime Probe

A runtime job normally reuses the generated input selected as the seed:

```json
{
  "schema_version": 2,
  "job_id": "run-target-turn-runtime",
  "job_kind": "runtime_probe",
  "job_dir": "/magma_shared/jobs/run-target-turn-runtime",
  "target_key": "fn48012_t982__p01",
  "input_file": "/magma_shared/jobs/seed/input.bin",
  "execution": {
    "binary": "/magma_out/afl/sndfile_fuzzer",
    "input_mode": "file",
    "arg_template": null,
    "timeout_seconds": 60,
    "work_dir": "."
  },
  "runtime_probe": {
    "gdb_bin": "gdb",
    "max_hits_per_probe": 200,
    "timeout_seconds": 60,
    "reasoning_summary": "...",
    "probes": [
      {
        "breakpoint": "ogg_opus.c:981",
        "expression": "odata->pkt_indx == odata->pkt_len",
        "expected_value": "true",
        "confidence": 96,
        "why_needed": "..."
      }
    ]
  }
}
```

Each probe must contain exactly one expression and a `filename:line`
breakpoint. Probes run independently so every requested breakpoint gets a
chance to execute. All values observed across repeated hits are recorded up to
`max_hits_per_probe`.

## Canonical Outputs

| Job kind | Required outputs |
| --- | --- |
| `coverage` | `output/execution.json`, `output/coverage.json`, `output/coverage/line.csv` |
| `runtime_probe` | `output/execution.json`, `output/runtime_observations.json` |

Generated inputs are preserved under `output/generated_input/`. GDB command
files and GDB stdout/stderr stay in temporary work storage and are removed by
default. LLVM raw profiles and exported JSON are also temporary.

`execute_job.py` invokes `process_coverage.py` with explicit input and output
paths. The processor has no dependency on `COV_DIR` and can also be run
independently. A relative `processor_script` path is resolved from the
directory containing `execute_job.py`.

```bash
python3 /magma/magcov/process_coverage.py \
  --input-file /tmp/coverage.prof.json \
  --output-dir /tmp/processed \
  --format csv \
  --verbose
```

## Running

Inspect tools visible in the container:

```bash
python3 /magma/magcov/run_worker.py --capabilities
```

Run a worker accepting both job kinds:

```bash
python3 /magma/magcov/run_worker.py \
  --redis-url redis://127.0.0.1:6379/0 \
  --namespace libsndfile-directed-test
```

During migration, a manually started container can be restricted:

```bash
python3 /magma/magcov/run_worker.py \
  --namespace libsndfile-directed-test \
  --accept-kind coverage
```

Run one already-visible job without Redis:

```bash
python3 /magma/magcov/execute_job.py \
  --job-file /magma_shared/jobs/example/job.json
```
