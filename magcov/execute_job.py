#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


MARKER_HIT = "__DIRECTED_TEST_PROBE_HIT__"
MARKER_VALUE = "__DIRECTED_TEST_PROBE_VALUE__"


class JobError(RuntimeError):
    pass


def log(message):
    print(
        "[{0}] [execute_job] {1}".format(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            message,
        ),
        flush=True,
    )


def load_json(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise JobError("Expected one JSON object in {0}".format(path))
    return value


def save_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def get_job_kind(job):
    raw = str(job.get("job_kind") or job.get("kind") or "coverage")
    normalized = raw.strip().lower().replace("-", "_")
    if normalized in ("coverage", "execution", "execute"):
        return "coverage"
    if normalized in ("runtime_probe", "probe", "gdb_probe"):
        return "runtime_probe"
    raise JobError("Unsupported job_kind: {0}".format(raw))


def require_executable(value, field):
    text = str(value or "").strip()
    if not text:
        raise JobError("{0} is required".format(field))
    candidate = Path(text)
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return candidate.resolve()
    resolved = shutil.which(text)
    if resolved:
        return Path(resolved).resolve()
    raise JobError("{0} is not executable: {1}".format(field, text))


def wait_for_file(value, field, timeout_seconds=10.0):
    path = Path(str(value or ""))
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if path.is_file():
            return path.resolve()
        time.sleep(0.1)
    raise JobError("{0} was not visible in time: {1}".format(field, path))


def safe_component(value):
    text = str(value or "").strip()
    normalized = "".join(
        character
        if character.isalnum() or character in ("-", "_", ".")
        else "_"
        for character in text
    ).strip("._")
    return normalized or "unknown"


def shell_join(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


def render_command(parts, replacements):
    rendered = []
    for part in parts:
        text = str(part)
        for key, value in replacements.items():
            text = text.replace("{" + key + "}", str(value))
        rendered.append(text)
    return rendered


def relative_output_path(root, value, field):
    root = Path(root).resolve()
    raw = str(value or "").strip()
    if not raw:
        raise JobError("{0} is required".format(field))
    if Path(raw).is_absolute():
        raise JobError("{0} must be relative: {1}".format(field, raw))
    candidate = (root / raw).resolve()
    if candidate == root or root not in candidate.parents:
        raise JobError("{0} escapes its work directory: {1}".format(field, raw))
    return candidate


def regular_files(root):
    root = Path(root)
    if not root.exists():
        return set()
    return set(path.resolve() for path in root.rglob("*") if path.is_file())


def build_program_command(binary, execution, input_file):
    input_mode = str(execution.get("input_mode") or "file").strip().lower()
    arg_template = execution.get("arg_template")
    if isinstance(arg_template, str) and arg_template.strip():
        rendered = arg_template.replace("@@", str(input_file))
        return [str(binary)] + shlex.split(rendered), None
    if input_mode == "file":
        return [str(binary), str(input_file)], None
    if input_mode == "stdin":
        return [str(binary)], input_file.read_bytes()
    raise JobError("Unsupported execution.input_mode: {0}".format(input_mode))


def policy_value(job, name, legacy_value=None, default="never"):
    retention = job.get("retention")
    value = retention.get(name) if isinstance(retention, dict) else None
    if value is None and legacy_value is not None:
        value = "always" if bool_value(legacy_value) else "never"
    if isinstance(value, bool):
        return "always" if value else "never"
    normalized = str(value or default).strip().lower()
    if normalized not in ("always", "on_failure", "never"):
        raise JobError(
            "retention.{0} must be always, on_failure, or never".format(name)
        )
    return normalized


def retain_file(source, destination, policy, failed):
    source = Path(source)
    destination = Path(destination)
    should_keep = policy == "always" or (policy == "on_failure" and failed)
    if should_keep and source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(destination))
        return str(destination)
    return None


class JobContext(object):
    def __init__(self, job, job_file):
        self.job = job
        self.job_file = Path(job_file).resolve()
        self.job_id = str(job.get("job_id") or "").strip()
        if not self.job_id:
            raise JobError("job.json is missing job_id")
        raw_job_dir = str(job.get("job_dir") or "").strip()
        if not raw_job_dir:
            raise JobError("job.json is missing job_dir")
        self.job_dir = Path(raw_job_dir).resolve()
        self.output_dir = self.job_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.job_dir / ".work" / safe_component(self.job_id)
        if self.work_dir.exists():
            shutil.rmtree(str(self.work_dir), ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.execution_path = self.output_dir / "execution.json"
        self.coverage_path = self.output_dir / "coverage.json"
        self.runtime_path = self.output_dir / "runtime_observations.json"
        self.generated_input_dir = self.output_dir / "generated_input"
        self.kind = get_job_kind(job)

        for stale in (
            self.execution_path,
            self.coverage_path,
            self.runtime_path,
        ):
            try:
                stale.unlink()
            except OSError:
                pass

    def cleanup(self):
        keep = bool_value(
            (self.job.get("retention") or {}).get("container_work_files")
            if isinstance(self.job.get("retention"), dict)
            else None,
            False,
        )
        if not keep:
            shutil.rmtree(str(self.work_dir), ignore_errors=True)


def validate_job(job):
    if not isinstance(job.get("execution"), dict):
        raise JobError("job.json requires an execution object")
    kind = get_job_kind(job)
    if kind == "coverage" and not isinstance(job.get("coverage"), dict):
        raise JobError("Coverage jobs require a coverage object")
    if kind == "runtime_probe" and not isinstance(job.get("runtime_probe"), dict):
        raise JobError("Runtime-probe jobs require a runtime_probe object")


def materialize_input(ctx):
    input_generation = ctx.job.get("input_generation")
    if not isinstance(input_generation, dict):
        return wait_for_file(ctx.job.get("input_file"), "input_file")

    mode = str(input_generation.get("mode") or "direct_input").strip().lower()
    if mode == "direct_input":
        return wait_for_file(ctx.job.get("input_file"), "input_file")
    if mode != "generator_script":
        raise JobError("Unsupported input_generation.mode: {0}".format(mode))

    generator = input_generation.get("generator_script")
    if not isinstance(generator, dict):
        raise JobError("input_generation.generator_script must be an object")
    script = wait_for_file(
        generator.get("script_file"),
        "input_generation.generator_script.script_file",
    )
    runner = generator.get("runner") or ["python3", "{script_path}"]
    if not isinstance(runner, list) or not runner:
        raise JobError("generator_script.runner must be a non-empty list")

    generator_dir = ctx.work_dir / "generator"
    generator_dir.mkdir()
    local_script = generator_dir / script.name
    shutil.copy2(str(script), str(local_script))
    before = regular_files(generator_dir)
    expected = relative_output_path(
        generator_dir,
        generator.get("output_filename"),
        "generator_script.output_filename",
    )
    command = render_command(
        runner,
        {
            "script_path": local_script,
            "work_dir": generator_dir,
        },
    )
    stdout_path = ctx.work_dir / "generator_stdout.txt"
    stderr_path = ctx.work_dir / "generator_stderr.txt"
    log("Running generator: {0}".format(shell_join(command)))
    with stdout_path.open("wb") as stdout_handle, stderr_path.open(
        "wb"
    ) as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=str(generator_dir),
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=float(generator.get("timeout_seconds", 30)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            retain_file(
                stdout_path,
                ctx.output_dir / "generator_stdout.txt",
                "on_failure",
                True,
            )
            retain_file(
                stderr_path,
                ctx.output_dir / "generator_stderr.txt",
                "on_failure",
                True,
            )
            raise JobError("Generator script timed out")

    if completed.returncode != 0:
        retain_file(
            stdout_path,
            ctx.output_dir / "generator_stdout.txt",
            "on_failure",
            True,
        )
        retain_file(
            stderr_path,
            ctx.output_dir / "generator_stderr.txt",
            "on_failure",
            True,
        )
        raise JobError(
            "Generator script exited with code {0}".format(completed.returncode)
        )

    ignored = set(
        path
        for path in regular_files(generator_dir) - before
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo")
    )
    produced = sorted((regular_files(generator_dir) - before) - ignored)
    if expected.resolve() not in produced:
        raise JobError(
            "Generator did not create output_filename: {0}".format(expected)
        )
    if len(produced) != 1:
        raise JobError(
            "Generator created {0} output files; expected exactly one: {1}".format(
                len(produced),
                ", ".join(str(path) for path in produced),
            )
        )

    relative = expected.relative_to(generator_dir.resolve())
    preserved = relative_output_path(
        ctx.generated_input_dir,
        str(relative),
        "generated input path",
    )
    preserved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(expected), str(preserved))
    return preserved


def run_process(command, cwd, env, timeout_seconds, stdin_bytes, stdout_path, stderr_path):
    start = time.time()
    timed_out = False
    exit_code = None
    with Path(stdout_path).open("wb") as stdout_handle, Path(stderr_path).open(
        "wb"
    ) as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                input=stdin_bytes,
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=float(timeout_seconds),
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    return {
        "timed_out": timed_out,
        "exit_code": exit_code,
        "duration_seconds": round(time.time() - start, 6),
    }


def resolve_coverage_processor(coverage):
    configured = str(
        coverage.get("processor_script")
        or coverage.get("process_coverage_py")
        or "process_coverage.py"
    ).strip()
    if not configured:
        raise JobError("coverage.processor_script is required")

    processor = Path(configured)
    if not processor.is_absolute():
        processor = Path(__file__).resolve().parent / processor
    processor = processor.resolve()
    if not processor.is_file():
        raise JobError(
            "coverage.processor_script does not exist: {0}".format(processor)
        )
    return processor


def run_coverage_processor(
    processor_script,
    profjson_path,
    output_dir,
    processed_format,
):
    command = [
        sys.executable,
        str(processor_script),
        "--input-file",
        str(profjson_path),
        "--output-dir",
        str(output_dir),
        "--format",
        str(processed_format),
    ]
    log("Processing coverage: {0}".format(shell_join(command)))
    subprocess.check_call(command)

    line_path = Path(output_dir) / "line.{0}".format(processed_format)
    branch_path = Path(output_dir) / "branch.{0}".format(processed_format)
    if not line_path.is_file():
        raise JobError(
            "Coverage processor did not produce line coverage: {0}".format(
                line_path
            )
        )
    if not branch_path.is_file():
        raise JobError(
            "Coverage processor did not produce branch coverage: {0}".format(
                branch_path
            )
        )
    return line_path, branch_path


def run_coverage_job(ctx, input_file):
    execution = ctx.job["execution"]
    coverage = ctx.job["coverage"]
    binary = require_executable(
        coverage.get("cov_bin") or execution.get("binary"),
        "coverage.cov_bin",
    )
    llvm_profdata = require_executable(
        coverage.get("llvm_profdata_bin") or "llvm-profdata",
        "coverage.llvm_profdata_bin",
    )
    llvm_cov = require_executable(
        coverage.get("llvm_cov_bin") or "llvm-cov",
        "coverage.llvm_cov_bin",
    )
    processor_script = resolve_coverage_processor(coverage)
    processed_format = str(coverage.get("processed_format") or "csv")
    command, stdin_bytes = build_program_command(binary, execution, input_file)
    cwd = Path(str(execution.get("work_dir") or binary.parent)).resolve()
    if not cwd.is_dir():
        raise JobError("execution.work_dir is not a directory: {0}".format(cwd))

    profiles_dir = ctx.work_dir / "profiles"
    profiles_dir.mkdir()
    stdout_temp = ctx.work_dir / "program_stdout.txt"
    stderr_temp = ctx.work_dir / "program_stderr.txt"
    environment = os.environ.copy()
    environment["LLVM_PROFILE_FILE"] = str(profiles_dir / "coverage-%p.profraw")
    log("Running coverage target: {0}".format(shell_join(command)))
    run = run_process(
        command=command,
        cwd=cwd,
        env=environment,
        timeout_seconds=execution.get("timeout_seconds", 30),
        stdin_bytes=stdin_bytes,
        stdout_path=stdout_temp,
        stderr_path=stderr_temp,
    )
    failed_execution = run["timed_out"] or (
        run["exit_code"] is not None and run["exit_code"] < 0
    )
    stdout_kept = retain_file(
        stdout_temp,
        ctx.output_dir / "stdout.txt",
        policy_value(
            ctx.job,
            "stdout",
            legacy_value=coverage.get("keep_stdout"),
            default="on_failure",
        ),
        failed_execution,
    )
    stderr_kept = retain_file(
        stderr_temp,
        ctx.output_dir / "stderr.txt",
        policy_value(
            ctx.job,
            "stderr",
            legacy_value=coverage.get("keep_stderr"),
            default="on_failure",
        ),
        failed_execution,
    )
    execution_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "job_kind": "coverage",
        "status": "timeout" if run["timed_out"] else "finished",
        "program_exit_code": run["exit_code"],
        "timed_out": run["timed_out"],
        "crashed": run["exit_code"] is not None and run["exit_code"] < 0,
        "duration_seconds": run["duration_seconds"],
        "input_file": str(input_file),
        "input_mode": str(execution.get("input_mode") or "file"),
        "command": command,
        "stdout_path": stdout_kept,
        "stderr_path": stderr_kept,
    }
    if run["timed_out"]:
        coverage_doc = {
            "schema_version": 1,
            "job_id": ctx.job_id,
            "status": "timeout",
            "coverage_collected": False,
            "processed_format": processed_format,
            "line_coverage_path": None,
            "branch_coverage_path": None,
            "note": "Target execution timed out",
        }
        save_json_atomic(ctx.execution_path, execution_doc)
        save_json_atomic(ctx.coverage_path, coverage_doc)
        return 124

    profiles = sorted(profiles_dir.glob("*.profraw"))
    profiles = [path for path in profiles if path.stat().st_size > 0]
    if not profiles:
        execution_doc["status"] = "finished_no_profile"
        coverage_doc = {
            "schema_version": 1,
            "job_id": ctx.job_id,
            "status": "no_profile",
            "coverage_collected": False,
            "processed_format": processed_format,
            "line_coverage_path": None,
            "branch_coverage_path": None,
            "note": "LLVM_PROFILE_FILE was not produced",
        }
        save_json_atomic(ctx.execution_path, execution_doc)
        save_json_atomic(ctx.coverage_path, coverage_doc)
        return 10

    profdata = ctx.work_dir / "coverage.profdata"
    profjson = ctx.work_dir / "coverage.prof.json"
    merge_command = [
        str(llvm_profdata),
        "merge",
        "-sparse",
    ] + [str(path) for path in profiles] + ["-o", str(profdata)]
    log("Merging {0} LLVM profile(s)".format(len(profiles)))
    subprocess.check_call(merge_command)
    with profjson.open("wb") as profjson_handle:
        subprocess.check_call(
            [
                str(llvm_cov),
                "export",
                str(binary),
                "-format=text",
                "-instr-profile={0}".format(profdata),
            ],
            stdout=profjson_handle,
        )

    processed_dir = ctx.work_dir / "processed"
    processed_dir.mkdir()
    line_source, branch_source = run_coverage_processor(
        processor_script=processor_script,
        profjson_path=profjson,
        output_dir=processed_dir,
        processed_format=processed_format,
    )

    coverage_dir = ctx.output_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    line_destination = coverage_dir / "line.{0}".format(processed_format)
    shutil.copy2(str(line_source), str(line_destination))

    keep_branch = bool_value(
        (ctx.job.get("retention") or {}).get("branch_coverage")
        if isinstance(ctx.job.get("retention"), dict)
        else coverage.get("keep_branch_coverage"),
        False,
    )
    branch_destination = None
    if keep_branch and branch_source is not None:
        branch_destination = coverage_dir / "branch.{0}".format(processed_format)
        shutil.copy2(str(branch_source), str(branch_destination))

    coverage_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "status": "finished",
        "coverage_collected": True,
        "processed_format": processed_format,
        "line_coverage_path": str(line_destination),
        "branch_coverage_path": (
            str(branch_destination) if branch_destination is not None else None
        ),
        "note": None,
    }
    save_json_atomic(ctx.execution_path, execution_doc)
    save_json_atomic(ctx.coverage_path, coverage_doc)
    return 0


def normalize_breakpoint(value):
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise JobError("Runtime breakpoint must be one filename:line value")
    if ":" not in text:
        raise JobError(
            "Runtime breakpoint must use filename:line, got: {0}".format(text)
        )
    source_file, line_text = text.rsplit(":", 1)
    source_file = source_file.strip()
    try:
        line = int(line_text.strip())
    except ValueError:
        raise JobError(
            "Runtime breakpoint line is not an integer: {0}".format(text)
        )
    if not source_file or not Path(source_file).name or line < 1:
        raise JobError(
            "Runtime breakpoint must use filename:positive-line, got: {0}".format(
                text
            )
        )
    return "{0}:{1}".format(source_file, line)


def normalize_expression(value):
    text = str(value or "").strip()
    if not text:
        raise JobError("Runtime probe expression is required")
    if "\n" in text or "\r" in text:
        raise JobError("Runtime probe expression must be one GDB expression")
    return text


def normalize_runtime_probes(job):
    runtime = job["runtime_probe"]
    probes = runtime.get("probes")
    if probes is None:
        probes = runtime.get("runtime_probes")
    if not isinstance(probes, list) or not probes:
        raise JobError("runtime_probe.probes must be a non-empty list")

    normalized = []
    for index, raw_probe in enumerate(probes, 1):
        if not isinstance(raw_probe, dict):
            raise JobError(
                "runtime_probe.probes[{0}] must be an object".format(index - 1)
            )
        expressions = raw_probe.get("expressions")
        if expressions is not None:
            if not isinstance(expressions, list) or len(expressions) != 1:
                raise JobError(
                    "Each runtime probe must contain exactly one expression"
                )
            expression = expressions[0]
        else:
            expression = raw_probe.get("expression")
        probe_id = safe_component(
            raw_probe.get("id") or "probe_{0:02d}".format(index)
        )
        normalized.append(
            {
                "id": probe_id,
                "breakpoint": normalize_breakpoint(
                    raw_probe.get("breakpoint") or raw_probe.get("location")
                ),
                "expression": normalize_expression(expression),
                "request": dict(raw_probe),
            }
        )
    return normalized


def gdb_literal(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("\n", "\\n")
    )


def build_gdb_script(path, binary, run_arguments, probe, max_hits):
    commands = [
        "set pagination off",
        "set confirm off",
        "set print pretty off",
        "set print elements 128",
        "set print repeats 16",
        "set breakpoint pending on",
        "file {0}".format(shlex.quote(str(binary))),
        "set $directed_input_generation_hits = 0",
        "break {0}".format(probe["breakpoint"]),
        "commands",
        "silent",
        "set $directed_input_generation_hits = $directed_input_generation_hits + 1",
        'printf "{0}|{1}|%d\\n", $directed_input_generation_hits'.format(
            MARKER_HIT,
            gdb_literal(probe["id"]),
        ),
        'printf "{0}|{1}|%d|", $directed_input_generation_hits'.format(
            MARKER_VALUE,
            gdb_literal(probe["id"]),
        ),
        "output {0}".format(probe["expression"]),
        'printf "\\n"',
    ]
    if max_hits > 0:
        commands.extend(
            [
                "if $directed_input_generation_hits >= {0}".format(max_hits),
                "quit",
                "end",
            ]
        )
    commands.extend(["continue", "end"])
    commands.append(
        "run {0}".format(shell_join(run_arguments))
        if run_arguments
        else "run"
    )
    commands.append("quit")
    Path(path).write_text("\n".join(commands) + "\n", encoding="utf-8")


def parse_gdb_output(stdout_text, probe_id):
    hit_count = 0
    observed = []
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if line.startswith(MARKER_HIT + "|"):
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[1] == probe_id:
                parsed = _integer_or_none(parts[2])
                if parsed is not None:
                    hit_count = max(hit_count, parsed)
        elif line.startswith(MARKER_VALUE + "|"):
            parts = line.split("|", 3)
            if len(parts) == 4 and parts[1] == probe_id:
                parsed = _integer_or_none(parts[2])
                observed.append(
                    {
                        "hit_index": (
                            parsed if parsed is not None else len(observed) + 1
                        ),
                        "value": parts[3].strip(),
                    }
                )
    return max(hit_count, len(observed)), observed


def _integer_or_none(value):
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def unique_values(observed):
    values = []
    seen = set()
    for item in observed:
        value = str(item.get("value") or "")
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def runtime_binary(job):
    runtime = job["runtime_probe"]
    value = (
        runtime.get("binary")
        or job["execution"].get("runtime_binary")
    )
    if not value:
        out_dir = os.environ.get("OUT")
        program = os.environ.get("PROGRAM")
        if out_dir and program:
            value = str(Path(out_dir) / "afl" / program)
    if not value:
        value = job["execution"].get("binary")
    return require_executable(value, "runtime_probe.binary")


def run_one_probe(ctx, input_file, binary, gdb, probe, timeout_seconds, max_hits):
    execution = ctx.job["execution"]
    command, stdin_bytes = build_program_command(binary, execution, input_file)
    if stdin_bytes is not None:
        raise JobError("Runtime probes do not support stdin input mode")

    probe_dir = ctx.work_dir / "gdb" / probe["id"]
    probe_dir.mkdir(parents=True)
    script_path = probe_dir / "commands.gdb"
    stdout_path = probe_dir / "stdout.txt"
    stderr_path = probe_dir / "stderr.txt"
    build_gdb_script(
        path=script_path,
        binary=binary,
        run_arguments=command[1:],
        probe=probe,
        max_hits=max_hits,
    )
    gdb_command = [str(gdb), "--batch", "-x", str(script_path)]
    run = run_process(
        command=gdb_command,
        cwd=Path(str(execution.get("work_dir") or binary.parent)).resolve(),
        env=os.environ.copy(),
        timeout_seconds=timeout_seconds,
        stdin_bytes=None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    stdout_text = stdout_path.read_text(
        encoding="utf-8",
        errors="replace",
    )
    stderr_text = stderr_path.read_text(
        encoding="utf-8",
        errors="replace",
    )
    hit_count, observed = parse_gdb_output(stdout_text, probe["id"])
    distinct = unique_values(observed)
    available = bool(observed)
    notes = []
    if hit_count:
        notes.append("breakpoint hit {0} time(s)".format(hit_count))
    else:
        notes.append("breakpoint was not reached")
    if observed:
        notes.append("captured {0} observed value(s)".format(len(observed)))
    if max_hits > 0 and hit_count >= max_hits:
        notes.append("maximum recorded hit count was reached")
    if run["timed_out"]:
        notes.append("GDB probe timed out")
    stderr_excerpt = "\n".join(stderr_text.strip().splitlines()[:8]).strip()
    if stderr_excerpt:
        notes.append("GDB reported: {0}".format(stderr_excerpt[:1000]))

    observation = dict(probe["request"])
    observation.update(
        {
            "breakpoint": probe["breakpoint"],
            "expression": probe["expression"],
            "available": available,
            "observed_value": observed[0]["value"] if observed else "",
            "observed_values": observed,
            "distinct_observed_values": distinct,
            "hit_count": hit_count,
            "observed_value_count": len(observed),
            "hit_limit_reached": max_hits > 0 and hit_count >= max_hits,
            "timed_out": run["timed_out"],
            "gdb_exit_code": run["exit_code"],
            "duration_seconds": run["duration_seconds"],
            "notes": "; ".join(notes),
        }
    )
    return observation


def run_runtime_probe_job(ctx, input_file):
    runtime = ctx.job["runtime_probe"]
    probes = normalize_runtime_probes(ctx.job)
    binary = runtime_binary(ctx.job)
    gdb = require_executable(
        runtime.get("gdb_bin") or "gdb",
        "runtime_probe.gdb_bin",
    )
    timeout_seconds = float(
        runtime.get(
            "timeout_seconds",
            ctx.job["execution"].get("timeout_seconds", 60),
        )
    )
    max_hits = int(runtime.get("max_hits_per_probe", 200))
    if max_hits < 0:
        raise JobError("runtime_probe.max_hits_per_probe cannot be negative")

    started = time.time()
    observations = []
    for probe in probes:
        log(
            "Running probe {0} at {1}".format(
                probe["id"],
                probe["breakpoint"],
            )
        )
        observations.append(
            run_one_probe(
                ctx=ctx,
                input_file=input_file,
                binary=binary,
                gdb=gdb,
                probe=probe,
                timeout_seconds=timeout_seconds,
                max_hits=max_hits,
            )
        )

    duration = round(time.time() - started, 6)
    timed_out_count = sum(
        1 for observation in observations if observation["timed_out"]
    )
    runtime_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "target_key": ctx.job.get("target_key"),
        "base_target_key": (
            ctx.job.get("base_target_key") or ctx.job.get("target_key")
        ),
        "job_kind": "runtime_probe",
        "status": "finished",
        "input_file": str(input_file),
        "binary": str(binary),
        "gdb_bin": str(gdb),
        "max_hits_per_probe": max_hits,
        "observation_count": len(observations),
        "available_observation_count": sum(
            1 for observation in observations if observation["available"]
        ),
        "timed_out_observation_count": timed_out_count,
        "duration_seconds": duration,
        "reasoning_summary": runtime.get("reasoning_summary"),
        "observations": observations,
    }
    execution_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "job_kind": "runtime_probe",
        "status": "finished",
        "program_exit_code": None,
        "timed_out": timed_out_count > 0,
        "crashed": False,
        "duration_seconds": duration,
        "input_file": str(input_file),
        "probe_count": len(observations),
    }
    save_json_atomic(ctx.runtime_path, runtime_doc)
    save_json_atomic(ctx.execution_path, execution_doc)
    return 0


def failure_documents(ctx, message, runner_exit_code):
    stdout_path = retain_file(
        ctx.work_dir / "program_stdout.txt",
        ctx.output_dir / "stdout.txt",
        policy_value(
            ctx.job,
            "stdout",
            legacy_value=(ctx.job.get("coverage") or {}).get("keep_stdout"),
            default="on_failure",
        ),
        True,
    )
    stderr_path = retain_file(
        ctx.work_dir / "program_stderr.txt",
        ctx.output_dir / "stderr.txt",
        policy_value(
            ctx.job,
            "stderr",
            legacy_value=(ctx.job.get("coverage") or {}).get("keep_stderr"),
            default="on_failure",
        ),
        True,
    )
    execution = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "job_kind": ctx.kind,
        "status": "failed",
        "program_exit_code": None,
        "timed_out": False,
        "crashed": False,
        "duration_seconds": None,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "error": message,
        "runner_exit_code": runner_exit_code,
    }
    save_json_atomic(ctx.execution_path, execution)
    if ctx.kind == "coverage":
        save_json_atomic(
            ctx.coverage_path,
            {
                "schema_version": 1,
                "job_id": ctx.job_id,
                "status": "failed",
                "coverage_collected": False,
                "processed_format": (
                    ctx.job.get("coverage", {}).get("processed_format")
                    or "csv"
                ),
                "line_coverage_path": None,
                "branch_coverage_path": None,
                "note": message,
            },
        )
    else:
        save_json_atomic(
            ctx.runtime_path,
            {
                "schema_version": 1,
                "job_id": ctx.job_id,
                "target_key": ctx.job.get("target_key"),
                "job_kind": "runtime_probe",
                "status": "failed",
                "observations": [],
                "error": message,
            },
        )


def run_job(job, job_file):
    validate_job(job)
    ctx = JobContext(job, job_file)
    try:
        input_file = materialize_input(ctx)
        if ctx.kind == "coverage":
            return run_coverage_job(ctx, input_file)
        return run_runtime_probe_job(ctx, input_file)
    except Exception as error:
        failure_documents(
            ctx,
            "{0}: {1}".format(type(error).__name__, error),
            2,
        )
        raise
    finally:
        ctx.cleanup()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Execute one directed test worker job"
    )
    parser.add_argument("--job-file", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    job_file = Path(args.job_file).resolve()
    try:
        job = load_json(job_file)
        return run_job(job, job_file)
    except JobError as error:
        log("ERROR: {0}".format(error))
        return 2
    except subprocess.CalledProcessError as error:
        log(
            "ERROR: subprocess exited with {0}: {1}".format(
                error.returncode,
                error.cmd,
            )
        )
        return 3
    except Exception as error:
        log("ERROR: {0}: {1}".format(type(error).__name__, error))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
