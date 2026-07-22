#!/usr/bin/env python3
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import redis

from manage_redis_jobs import (
    decode_redis_hash,
    job_key,
    processing_key,
    publish_done,
    publish_event,
    queue_key,
    read_json,
    utc_now_iso,
)


LOG = logging.getLogger("process_execution_jobs")


class WorkerError(Exception):
    pass


def setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def make_redis(url):
    client = redis.Redis.from_url(url, decode_responses=False)
    client.ping()
    return client


def decode_item(item):
    if item is None:
        return None
    if isinstance(item, tuple) and len(item) == 2:
        _, value = item
    else:
        value = item

    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def claim_job(client, namespace, block_seconds):
    LOG.debug("Waiting for Redis job notification on queue=%s", queue_key(namespace))
    item = client.brpoplpush(
        queue_key(namespace),
        processing_key(namespace),
        timeout=block_seconds,
    )
    job_id = decode_item(item)
    if job_id:
        LOG.info("Received Redis job notification: job_id=%s", job_id)
    return job_id


def load_job_file_from_redis(client, namespace, job_id, wait_seconds=20.0, poll_interval=0.1):
    state = decode_redis_hash(client.hgetall(job_key(namespace, job_id)))
    job_file = state.get("job_file")
    if not job_file:
        raise WorkerError("Redis state missing job_file for job_id={0}".format(job_id))

    path = Path(job_file)
    parent = path.parent
    grandparent = parent.parent

    deadline = time.time() + wait_seconds
    last_error = None

    while time.time() < deadline:
        try:
            if path.is_file():
                resolved = path.resolve()
                LOG.info("Resolved job file from Redis: job_id=%s job_file=%s", job_id, resolved,)
                return resolved
        except Exception as e:
            last_error = e

        time.sleep(poll_interval)

    def safe_listdir(p: Path):
        try:
            if p.is_dir():
                return sorted(os.listdir(p))[:20]
            return None
        except Exception as e:
            return ["<listdir failed: {0}: {1}>".format(type(e).__name__, e)]

    extra = {
        "path_exists": path.exists(),
        "path_is_file": path.is_file(),
        "parent": str(parent),
        "parent_exists": parent.exists(),
        "parent_is_dir": parent.is_dir(),
        "parent_listdir": safe_listdir(parent),
        "grandparent": str(grandparent),
        "grandparent_exists": grandparent.exists(),
        "grandparent_is_dir": grandparent.is_dir(),
        "grandparent_listdir": safe_listdir(grandparent),
    }
    if last_error is not None:
        extra["last_error"] = "{0}: {1}".format(type(last_error).__name__, last_error)

    raise WorkerError(
        "job_file not visible after waiting for job_id={0}: {1} ({2})".format(
            job_id, path, extra,
        )
    )


def set_status(client, namespace, job_id, status, **extra):
    mapping = {"status": status}
    mapping.update(extra)

    normalized = {}
    for key_name, value in mapping.items():
        normalized[str(key_name)] = "" if value is None else str(value)

    client.hset(job_key(namespace, job_id), mapping=normalized)
    publish_event(client, namespace, {"event": status, "job_id": job_id, **normalized})
    LOG.info("Published worker status: job_id=%s status=%s", job_id, status)


def finish_processing_entry(client, namespace, job_id):
    client.lrem(processing_key(namespace), 1, job_id)
    LOG.debug("Removed job_id=%s from processing list", job_id)


def infer_output_paths(job):
    job_dir = Path(str(job["job_dir"])).resolve()
    output_dir = job_dir / "output"
    return (
        output_dir / "execution.json",
        output_dir / "coverage.json",
        output_dir / "runtime_observations.json",
    )


def get_job_kind(job):
    raw_kind = str(job.get("job_kind") or job.get("kind") or "coverage").strip().lower()
    normalized = raw_kind.replace("-", "_")
    if normalized in {"runtime_probe", "probe", "gdb_probe"}:
        return "runtime_probe"
    return "coverage"


def finalize_status_from_outputs(client, namespace, job_id, job, execution_json, coverage_json, runtime_observations_json):
    execution_doc = read_json(execution_json) if execution_json.is_file() else {}
    coverage_doc = read_json(coverage_json) if coverage_json.is_file() else {}
    runtime_doc = read_json(runtime_observations_json) if runtime_observations_json.is_file() else {}

    execution_status = str(execution_doc.get("status", ""))
    coverage_status = str(coverage_doc.get("status", ""))
    runtime_status = str(runtime_doc.get("status", ""))
    job_kind = get_job_kind(job)

    if job_kind == "runtime_probe":
        if execution_status == "timeout" or runtime_status == "timeout":
            status = "timeout"
        elif execution_json.is_file() and runtime_observations_json.is_file():
            status = "finished"
        else:
            status = "failed"
    else:
        if execution_status == "timeout" or coverage_status == "timeout":
            status = "timeout"
        elif execution_json.is_file() and coverage_json.is_file():
            status = "finished"
        else:
            status = "failed"

    result = {
        "finished_at": utc_now_iso(),
        "job_kind": job_kind,
        "program_exit_code": execution_doc.get("program_exit_code"),
        "timed_out": execution_doc.get("timed_out"),
        "crashed": execution_doc.get("crashed"),
        "duration_seconds": execution_doc.get("duration_seconds"),
        "execution_json": str(execution_json) if execution_json.is_file() else "",
        "coverage_json": str(coverage_json) if coverage_json.is_file() else "",
        "runtime_observations_json": str(runtime_observations_json) if runtime_observations_json.is_file() else "",
        "target_reached": coverage_doc.get("target_reached"),
        "coverage_collected": coverage_doc.get("coverage_collected"),
        "runtime_probe_observation_count": len(runtime_doc.get("observations", [])) if isinstance(runtime_doc.get("observations"), list) else "",
    }

    LOG.info(
        "Finalizing job result: job_id=%s kind=%s status=%s exit_code=%s coverage_collected=%s runtime_observations=%s",
        job_id,
        job_kind,
        status,
        result.get("program_exit_code"),
        result.get("coverage_collected"),
        result.get("runtime_probe_observation_count"),
    )

    set_status(client, namespace, job_id, status, **result)
    publish_done(client, namespace, {"job_id": job_id, "status": status, **result})

    LOG.info(
        "Emitted final result: job_id=%s status=%s execution_json=%s coverage_json=%s runtime_observations_json=%s",
        job_id,
        status,
        result.get("execution_json"),
        result.get("coverage_json"),
        result.get("runtime_observations_json"),
    )

    return {"status": status, **result}


def stream_process_output(proc, job_id):
    """
    Stream child stdout/stderr in real time to the worker logs.

    Uses line buffering so output appears while the child is running.
    """
    stdout_open = proc.stdout is not None
    stderr_open = proc.stderr is not None

    while stdout_open or stderr_open:
        made_progress = False

        if stdout_open:
            line = proc.stdout.readline()
            if line:
                LOG.info("[job_id=%s] [runner stdout] %s", job_id, line.rstrip("\n"))
                made_progress = True
            elif proc.poll() is not None:
                stdout_open = False

        if stderr_open:
            line = proc.stderr.readline()
            if line:
                LOG.warning("[job_id=%s] [runner stderr] %s", job_id, line.rstrip("\n"))
                made_progress = True
            elif proc.poll() is not None:
                stderr_open = False

        if not made_progress:
            if proc.poll() is not None:
                # Process has exited; drain any remaining buffered lines.
                if stdout_open and proc.stdout is not None:
                    remainder = proc.stdout.read()
                    if remainder:
                        for line in remainder.splitlines():
                            LOG.info("[job_id=%s] [runner stdout] %s", job_id, line)
                if stderr_open and proc.stderr is not None:
                    remainder = proc.stderr.read()
                    if remainder:
                        for line in remainder.splitlines():
                            LOG.warning("[job_id=%s] [runner stderr] %s", job_id, line)
                break

            time.sleep(0.05)

    return proc.wait()


def execute_job(job_file, execute_script, job_id):
    cmd = [sys.executable, str(execute_script), "--job-file", str(job_file)]
    LOG.info("Starting execution runner: job_id=%s cmd=%s", job_id, " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        bufsize=1,
    )

    returncode = stream_process_output(proc, job_id)

    LOG.info("Execution runner exited: job_id=%s returncode=%s", job_id, returncode)
    return returncode


def process_one_job(client, namespace, job_id, execute_script):
    LOG.info("Claimed job_id=%s", job_id)
    set_status(client, namespace, job_id, "claimed", claimed_at=utc_now_iso())

    job_file = load_job_file_from_redis(client, namespace, job_id)
    job = read_json(job_file)

    LOG.info(
        "Loaded job metadata: job_id=%s project=%s target_key=%s function_index=%s attempt=%s input_file=%s job_dir=%s",
        job_id,
        job.get("project"),
        job.get("target_key"),
        job.get("function_index"),
        job.get("response_count"),
        job.get("input_file"),
        job.get("job_dir"),
    )

    set_status(
        client,
        namespace,
        job_id,
        "running",
        started_at=utc_now_iso(),
        worker_pid=os.getpid(),
    )

    runner_returncode = execute_job(job_file, execute_script, job_id)
    execution_json, coverage_json, runtime_observations_json = infer_output_paths(job)

    LOG.info(
        "Runner finished: job_id=%s returncode=%s execution_json=%s coverage_json=%s runtime_observations_json=%s",
        job_id,
        runner_returncode,
        execution_json,
        coverage_json,
        runtime_observations_json,
    )

    if runner_returncode not in (0, 10, 124) and not execution_json.is_file():
        LOG.error(
            "Runner failed before writing execution.json: job_id=%s returncode=%s",
            job_id,
            runner_returncode,
        )
        set_status(
            client,
            namespace,
            job_id,
            "failed",
            finished_at=utc_now_iso(),
            runner_return_code=runner_returncode,
            error="runner_failed_before_writing_execution_json",
        )
        publish_done(
            client,
            namespace,
            {
                "job_id": job_id,
                "status": "failed",
                "runner_return_code": runner_returncode,
            },
        )
        return

    finalize_status_from_outputs(
        client,
        namespace,
        job_id,
        job,
        execution_json,
        coverage_json,
        runtime_observations_json,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Process execution jobs from Redis")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0", help="Redis URL")
    parser.add_argument("--namespace", default="llm-exec", help="Redis key namespace")
    parser.add_argument(
        "--execute-script",
        default=str(Path(__file__).resolve().parent / "execute_job.py"),
        help="Path to run_coverage_job.py",
    )
    parser.add_argument("--block-seconds", type=int, default=5, help="Blocking pop timeout")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    execute_script = Path(args.execute_script).resolve()
    if not execute_script.is_file():
        LOG.error("execute script not found: %s", execute_script)
        return 2

    try:
        client = make_redis(args.redis_url)
        LOG.info("Connected to Redis: %s", args.redis_url)
    except redis.RedisError as e:
        LOG.error("Redis connection failed: %s", e)
        return 3

    processed = 0
    while True:
        try:
            job_id = claim_job(client, args.namespace, args.block_seconds)
            if not job_id:
                if args.once:
                    LOG.info("No job received in once mode, exiting")
                    return 0
                continue

            try:
                process_one_job(client, args.namespace, job_id, execute_script)
            except Exception as e:
                LOG.exception("Job failed job_id=%s", job_id)
                set_status(
                    client,
                    args.namespace,
                    job_id,
                    "failed",
                    finished_at=utc_now_iso(),
                    error="{0}: {1}".format(type(e).__name__, e),
                )
                publish_done(
                    client,
                    args.namespace,
                    {
                        "job_id": job_id,
                        "status": "failed",
                        "error": "{0}: {1}".format(type(e).__name__, e),
                    },
                )
            finally:
                finish_processing_entry(client, args.namespace, job_id)

            processed += 1
            if args.once and processed >= 1:
                LOG.info("Processed one job in once mode, exiting")
                return 0

        except redis.RedisError as e:
            LOG.error("Redis error in worker loop: %s", e)
            time.sleep(1.0)
        except KeyboardInterrupt:
            LOG.info("Worker interrupted, exiting")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
