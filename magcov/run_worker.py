#!/usr/bin/env python3
import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


LOG = logging.getLogger("directed_input_generation.run_worker")


class WorkerError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def queue_key(namespace):
    return "{0}:job_queue".format(namespace)


def processing_key(namespace):
    return "{0}:processing".format(namespace)


def job_key(namespace, job_id):
    return "{0}:job:{1}".format(namespace, job_id)


def done_channel(namespace):
    return "{0}:job_done".format(namespace)


def event_channel(namespace):
    return "{0}:job_events".format(namespace)


def decode(value):
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def decode_hash(raw):
    return {
        decode(key): decode(value)
        for key, value in (raw or {}).items()
    }


def load_json(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise WorkerError("Expected one JSON object in {0}".format(path))
    return value


def get_job_kind(job):
    raw = str(job.get("job_kind") or job.get("kind") or "coverage")
    normalized = raw.strip().lower().replace("-", "_")
    if normalized in ("coverage", "execution", "execute"):
        return "coverage"
    if normalized in ("runtime_probe", "probe", "gdb_probe"):
        return "runtime_probe"
    raise WorkerError("Unsupported job_kind: {0}".format(raw))


def connect_redis(redis_url):
    try:
        import redis
    except ImportError:
        raise WorkerError("The redis package is required by run_worker.py")
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=False)
        client.ping()
        return client
    except Exception as error:
        raise WorkerError(
            "Could not connect to Redis at {0}: {1}".format(redis_url, error)
        )


def publish(client, channel, payload):
    client.publish(channel, json.dumps(payload, ensure_ascii=False))


def set_status(client, namespace, job_id, status, **details):
    fields = {"status": status}
    fields.update(details)
    encoded = {
        str(key): "" if value is None else str(value)
        for key, value in fields.items()
    }
    client.hset(job_key(namespace, job_id), mapping=encoded)
    publish(
        client,
        event_channel(namespace),
        dict({"event": status, "job_id": job_id}, **encoded),
    )


def publish_terminal(client, namespace, job_id, status, details):
    set_status(client, namespace, job_id, status, **details)
    publish(
        client,
        done_channel(namespace),
        dict({"job_id": job_id, "status": status}, **details),
    )


def claim_job(client, namespace, block_seconds):
    value = client.brpoplpush(
        queue_key(namespace),
        processing_key(namespace),
        timeout=int(block_seconds),
    )
    return decode(value) if value is not None else None


def finish_processing(client, namespace, job_id):
    client.lrem(processing_key(namespace), 1, job_id)


def resolve_job_file(client, namespace, job_id, timeout_seconds=20.0):
    state = decode_hash(client.hgetall(job_key(namespace, job_id)))
    raw_path = state.get("job_file")
    if not raw_path:
        raise WorkerError(
            "Redis state is missing job_file for job_id={0}".format(job_id)
        )
    path = Path(raw_path)
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if path.is_file():
            return path.resolve()
        time.sleep(0.1)
    raise WorkerError(
        "job_file did not become visible for job_id={0}: {1}".format(
            job_id,
            path,
        )
    )


def capabilities(accepted_kinds=None):
    accepted = set(accepted_kinds or ("coverage", "runtime_probe"))
    return {
        "schema_version": 1,
        "worker_id": "{0}:{1}".format(socket.gethostname(), os.getpid()),
        "python_version": ".".join(
            str(value) for value in sys.version_info[:3]
        ),
        "accepted_job_kinds": sorted(accepted),
        "tools_visible_on_path": {
            "gdb": shutil.which("gdb"),
            "llvm_cov": shutil.which("llvm-cov"),
            "llvm_profdata": shutil.which("llvm-profdata"),
        },
    }


def output_paths(job):
    output_dir = Path(str(job["job_dir"])).resolve() / "output"
    return {
        "execution": output_dir / "execution.json",
        "coverage": output_dir / "coverage.json",
        "runtime": output_dir / "runtime_observations.json",
    }


def terminal_result(job, runner_return_code):
    kind = get_job_kind(job)
    paths = output_paths(job)
    execution = (
        load_json(paths["execution"]) if paths["execution"].is_file() else {}
    )
    execution_status = str(execution.get("status") or "")
    details = {
        "finished_at": utc_now(),
        "job_kind": kind,
        "runner_return_code": runner_return_code,
        "program_exit_code": execution.get("program_exit_code"),
        "timed_out": execution.get("timed_out"),
        "crashed": execution.get("crashed"),
        "duration_seconds": execution.get("duration_seconds"),
        "execution_json": (
            str(paths["execution"]) if paths["execution"].is_file() else ""
        ),
        "coverage_json": "",
        "runtime_observations_json": "",
    }

    if execution_status == "timeout":
        return "timeout", details
    if execution_status == "failed":
        details["error"] = execution.get("error") or "executor failed"
        return "failed", details

    if kind == "coverage":
        if not paths["coverage"].is_file():
            details["error"] = "coverage.json was not produced"
            return "failed", details
        coverage = load_json(paths["coverage"])
        details["coverage_json"] = str(paths["coverage"])
        details["coverage_collected"] = coverage.get("coverage_collected")
        details["line_coverage_path"] = coverage.get("line_coverage_path")
        if str(coverage.get("status") or "") == "timeout":
            return "timeout", details
        if not bool(coverage.get("coverage_collected")):
            details["error"] = (
                coverage.get("note") or "coverage was not collected"
            )
            return "failed", details
        if runner_return_code != 0:
            details["error"] = "executor returned {0}".format(
                runner_return_code
            )
            return "failed", details
        return "finished", details

    if not paths["runtime"].is_file():
        details["error"] = "runtime_observations.json was not produced"
        return "failed", details
    runtime = load_json(paths["runtime"])
    details["runtime_observations_json"] = str(paths["runtime"])
    details["runtime_probe_observation_count"] = runtime.get(
        "observation_count",
        len(runtime.get("observations", []))
        if isinstance(runtime.get("observations"), list)
        else 0,
    )
    details["runtime_probe_available_count"] = runtime.get(
        "available_observation_count",
        0,
    )
    if str(runtime.get("status") or "") == "failed":
        details["error"] = runtime.get("error") or "runtime collection failed"
        return "failed", details
    if runner_return_code != 0:
        details["error"] = "executor returned {0}".format(runner_return_code)
        return "failed", details
    return "finished", details


def process_job(
    client,
    namespace,
    job_id,
    execute_script,
    accepted_kinds,
):
    worker_id = "{0}:{1}".format(socket.gethostname(), os.getpid())
    set_status(
        client,
        namespace,
        job_id,
        "claimed",
        claimed_at=utc_now(),
        worker_id=worker_id,
    )
    job_file = resolve_job_file(client, namespace, job_id)
    job = load_json(job_file)
    document_job_id = str(job.get("job_id") or "")
    if document_job_id != job_id:
        raise WorkerError(
            "Redis job ID {0} does not match job.json ID {1}".format(
                job_id,
                document_job_id,
            )
        )
    kind = get_job_kind(job)
    if kind not in accepted_kinds:
        raise WorkerError(
            "This worker does not accept job_kind={0}; accepted: {1}".format(
                kind,
                ", ".join(sorted(accepted_kinds)),
            )
        )

    set_status(
        client,
        namespace,
        job_id,
        "running",
        started_at=utc_now(),
        worker_id=worker_id,
        job_kind=kind,
    )
    command = [
        sys.executable,
        str(execute_script),
        "--job-file",
        str(job_file),
    ]
    LOG.info("Executing job_id=%s kind=%s", job_id, kind)
    runner_return_code = subprocess.call(command)
    status, details = terminal_result(job, runner_return_code)
    publish_terminal(client, namespace, job_id, status, details)
    LOG.info("Completed job_id=%s status=%s", job_id, status)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Process directed test jobs from Redis"
    )
    parser.add_argument(
        "--redis-url",
        default="redis://127.0.0.1:6379/0",
    )
    parser.add_argument("--namespace", default="directed-test")
    parser.add_argument(
        "--execute-script",
        default=str(Path(__file__).resolve().parent / "execute_job.py"),
    )
    parser.add_argument("--block-seconds", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--accept-kind",
        action="append",
        choices=("coverage", "runtime_probe"),
        default=None,
    )
    parser.add_argument("--capabilities", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def configure_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)
    accepted_kinds = set(
        args.accept_kind or ("coverage", "runtime_probe")
    )
    if args.capabilities:
        print(
            json.dumps(
                capabilities(accepted_kinds),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    execute_script = Path(args.execute_script).resolve()
    if not execute_script.is_file():
        LOG.error("execute_job.py was not found: %s", execute_script)
        return 2
    try:
        client = connect_redis(args.redis_url)
    except WorkerError as error:
        LOG.error("%s", error)
        return 3

    LOG.info(
        "Worker ready namespace=%s accepted=%s capabilities=%s",
        args.namespace,
        ",".join(sorted(accepted_kinds)),
        json.dumps(capabilities(accepted_kinds), ensure_ascii=False),
    )
    processed = 0
    while True:
        job_id = None
        try:
            job_id = claim_job(
                client,
                args.namespace,
                args.block_seconds,
            )
            if job_id is None:
                if args.once:
                    return 0
                continue
            try:
                process_job(
                    client=client,
                    namespace=args.namespace,
                    job_id=job_id,
                    execute_script=execute_script,
                    accepted_kinds=accepted_kinds,
                )
            except Exception as error:
                LOG.exception("Job failed job_id=%s", job_id)
                publish_terminal(
                    client,
                    args.namespace,
                    job_id,
                    "failed",
                    {
                        "finished_at": utc_now(),
                        "error": "{0}: {1}".format(
                            type(error).__name__,
                            error,
                        ),
                    },
                )
            finally:
                finish_processing(client, args.namespace, job_id)

            processed += 1
            if args.once and processed >= 1:
                return 0
        except KeyboardInterrupt:
            LOG.info("Worker interrupted")
            return 0
        except Exception as error:
            LOG.error("Worker loop error: %s", error)
            if args.once:
                return 4
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
