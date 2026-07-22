#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple


class JobError(Exception):
    pass


@dataclass
class JobPaths:
    job_file: Path
    job_dir: Path
    output_dir: Path
    coverage_dir: Path
    profraw_path: Path
    profdata_path: Path
    profjson_path: Path
    stdout_path: Path
    stderr_path: Path
    worker_log_path: Path
    execution_json_path: Path
    coverage_json_path: Path
    runtime_observations_path: Path
    gdb_commands_path: Path
    gdb_stdout_path: Path
    gdb_stderr_path: Path
    line_cov_path: Path
    branch_cov_path: Path
    generator_stdout_path: Path
    generator_stderr_path: Path
    generated_input_dir: Path


@dataclass
class ToolConfig:
    binary: Path
    cov_bin: Path
    llvm_profdata: Path
    llvm_cov: Path
    process_coverage_py: Path


@dataclass
class ExecutionConfig:
    input_mode: str
    arg_template: Optional[str]
    timeout_seconds: float
    work_dir: Path


@dataclass
class JobContext:
    job: Dict
    job_id: str
    paths: JobPaths
    tools: ToolConfig
    execution: ExecutionConfig
    coverage: Dict
    processed_format: str
    retention: Dict[str, bool]
    env: Dict[str, str]
    worker_log_lines: List[str]


@dataclass
class ProgramRunResult:
    argv: List[str]
    stdin_bytes: Optional[bytes]
    timed_out: bool
    exit_code: Optional[int]
    crashed: bool
    duration_seconds: float


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[{0}] [coverage_job] {1}".format(ts, msg), flush=True)


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise JobError("Expected JSON object in {0}".format(path))
    return data


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def wait_for_file(path_str, field, wait_seconds=5.0, poll_interval=0.1):
    path = Path(path_str)
    deadline = time.time() + wait_seconds
    last_exists = None
    last_is_file = None

    while time.time() < deadline:
        last_exists = path.exists()
        last_is_file = path.is_file()
        if last_is_file:
            return path
        time.sleep(poll_interval)

    raise JobError(
        "{0} not visible after waiting: {1} (exists={2} is_file={3})".format(
            field, path, last_exists, last_is_file
        )
    )


def require_file(path_str, field):
    path = Path(path_str)
    if not path.is_file():
        raise JobError("{0} does not exist or is not a file: {1}".format(field, path))
    return path


def require_executable(path_str, field):
    path = Path(path_str)
    if path.is_file() and os.access(str(path), os.X_OK):
        return path
    resolved = shutil.which(path_str)
    if resolved:
        return Path(resolved)
    raise JobError("{0} is not executable or not found in PATH: {1}".format(field, path_str))


def shell_join(parts):
    return " ".join(shlex.quote(str(p)) for p in parts)


def render_command_template(parts, replacements):
    rendered = []
    for part in parts:
        value = str(part)
        for key, replacement in replacements.items():
            value = value.replace("{" + key + "}", str(replacement))
        rendered.append(value)
    return rendered


def require_relative_output_path(base_dir, output_filename):
    candidate = (base_dir / str(output_filename)).resolve()
    base_resolved = base_dir.resolve()
    if candidate == base_resolved or base_resolved not in candidate.parents:
        raise JobError("Generated output must stay inside job input directory: {0}".format(output_filename))
    return candidate


def bool_config(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def build_command(binary, input_mode, input_file, arg_template):
    if arg_template:
        rendered = arg_template.replace("@@", str(input_file))
        argv = [str(binary)] + shlex.split(rendered)
        return argv, None

    if input_mode == "stdin":
        argv = [str(binary)]
        stdin_bytes = input_file.read_bytes()
        return argv, stdin_bytes

    if input_mode == "file":
        return [str(binary), str(input_file)], None

    raise JobError("Unsupported input_mode: {0}".format(input_mode))


def get_job_kind(job: Dict) -> str:
    raw_kind = str(job.get("job_kind") or job.get("kind") or "coverage").strip().lower()
    normalized = raw_kind.replace("-", "_")
    if normalized in {"coverage", "execution", "execute"}:
        return "coverage"
    if normalized in {"runtime_probe", "probe", "gdb_probe"}:
        return "runtime_probe"
    raise JobError("Unsupported job_kind: {0}".format(raw_kind))


def write_worker_log(worker_log_path, lines):
    worker_log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def safe_unlink(path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def collect_retention_policy(coverage_cfg):
    return {
        "keep_profraw": bool_config(coverage_cfg.get("keep_profraw"), False),
        "keep_profdata": bool_config(coverage_cfg.get("keep_profdata"), False),
        "keep_profjson": bool_config(coverage_cfg.get("keep_profjson"), False),
        "keep_stdout": bool_config(coverage_cfg.get("keep_stdout"), True),
        "keep_stderr": bool_config(coverage_cfg.get("keep_stderr"), True),
        "keep_worker_log": bool_config(coverage_cfg.get("keep_worker_log"), True),
        "keep_line_coverage": bool_config(coverage_cfg.get("keep_line_coverage"), True),
        "keep_branch_coverage": bool_config(coverage_cfg.get("keep_branch_coverage"), True),
    }


def apply_retention_policy(
    retention,
    profraw_path,
    profdata_path,
    profjson_path,
    stdout_path,
    stderr_path,
    worker_log_path,
    line_cov_path,
    branch_cov_path,
    coverage_doc,
):
    if not retention["keep_profraw"]:
        safe_unlink(profraw_path)

    if not retention["keep_profdata"]:
        safe_unlink(profdata_path)
        coverage_doc["profdata_path"] = None

    if not retention["keep_profjson"]:
        safe_unlink(profjson_path)
        coverage_doc["profjson_path"] = None

    if not retention["keep_stdout"]:
        safe_unlink(stdout_path)

    if not retention["keep_stderr"]:
        safe_unlink(stderr_path)

    if not retention["keep_worker_log"]:
        safe_unlink(worker_log_path)

    if not retention["keep_line_coverage"]:
        safe_unlink(line_cov_path)
        coverage_doc["line_coverage_path"] = None

    if not retention["keep_branch_coverage"]:
        safe_unlink(branch_cov_path)
        coverage_doc["branch_coverage_path"] = None


def move_processed_coverage_outputs(coverage_dir, processed_format):
    desired_line = coverage_dir / ("line." + processed_format)
    desired_branch = coverage_dir / ("branch." + processed_format)

    found_line = None
    found_branch = None

    for path in coverage_dir.glob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".line." + processed_format):
            found_line = path
        elif name.endswith(".branch." + processed_format):
            found_branch = path

    if found_line is None:
        raise JobError("Processed line coverage file was not produced in {0}".format(coverage_dir))
    if found_branch is None:
        raise JobError("Processed branch coverage file was not produced in {0}".format(coverage_dir))

    if desired_line.exists():
        desired_line.unlink()
    if desired_branch.exists():
        desired_branch.unlink()

    found_line.replace(desired_line)
    found_branch.replace(desired_branch)

    return desired_line, desired_branch


def make_worker_logger(worker_log_lines: List[str]) -> Callable[[str], None]:
    def wlog(message: str) -> None:
        line = "[{0}] {1}".format(time.strftime("%Y-%m-%d %H:%M:%S"), message)
        worker_log_lines.append(line)
        log(message)

    return wlog


def validate_top_level_job(job: Dict) -> None:
    required_top = ["job_id", "job_dir", "execution", "coverage"]
    for key in required_top:
        if key not in job:
            raise JobError("job.json missing required field: {0}".format(key))

    if not isinstance(job["execution"], dict) or not isinstance(job["coverage"], dict):
        raise JobError("execution and coverage must both be JSON objects")

    if get_job_kind(job) == "runtime_probe" and not isinstance(job.get("runtime_probe"), dict):
        raise JobError("runtime_probe jobs require a runtime_probe JSON object")


def build_job_paths(job_id: str, job_dir: Path, job_file: Path, processed_format: str) -> JobPaths:
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    coverage_dir = output_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)

    return JobPaths(
        job_file=job_file,
        job_dir=job_dir,
        output_dir=output_dir,
        coverage_dir=coverage_dir,
        profraw_path=output_dir / (job_id + ".profraw"),
        profdata_path=output_dir / (job_id + ".profdata"),
        profjson_path=output_dir / (job_id + ".prof.json"),
        stdout_path=output_dir / "stdout.txt",
        stderr_path=output_dir / "stderr.txt",
        worker_log_path=output_dir / "worker.log",
        execution_json_path=output_dir / "execution.json",
        coverage_json_path=output_dir / "coverage.json",
        runtime_observations_path=output_dir / "runtime_observations.json",
        gdb_commands_path=output_dir / "runtime_probe.gdb",
        gdb_stdout_path=output_dir / "gdb_stdout.txt",
        gdb_stderr_path=output_dir / "gdb_stderr.txt",
        line_cov_path=coverage_dir / ("line." + processed_format),
        branch_cov_path=coverage_dir / ("branch." + processed_format),
        generator_stdout_path=output_dir / "generator_stdout.txt",
        generator_stderr_path=output_dir / "generator_stderr.txt",
        generated_input_dir=job_dir / "generated_input",
    )


def build_tool_config(execution_cfg: Dict, coverage_cfg: Dict, job_kind: str) -> ToolConfig:
    binary = require_executable(
        str(execution_cfg.get("binary") or execution_cfg.get("program") or ""),
        "execution.binary",
    )
    if job_kind == "runtime_probe":
        return ToolConfig(
            binary=binary,
            cov_bin=Path(str(coverage_cfg.get("cov_bin", str(binary)))),
            llvm_profdata=Path(str(coverage_cfg.get("llvm_profdata_bin", "llvm-profdata"))),
            llvm_cov=Path(str(coverage_cfg.get("llvm_cov_bin", "llvm-cov"))),
            process_coverage_py=Path(str(coverage_cfg.get("process_coverage_py", ""))),
        )

    return ToolConfig(
        binary=binary,
        cov_bin=require_executable(
            str(coverage_cfg.get("cov_bin", str(binary))),
            "coverage.cov_bin",
        ),
        llvm_profdata=require_executable(
            str(coverage_cfg.get("llvm_profdata_bin", "llvm-profdata")),
            "coverage.llvm_profdata_bin",
        ),
        llvm_cov=require_executable(
            str(coverage_cfg.get("llvm_cov_bin", "llvm-cov")),
            "coverage.llvm_cov_bin",
        ),
        process_coverage_py=require_file(
            str(coverage_cfg.get("process_coverage_py")),
            "coverage.process_coverage_py",
        ),
    )


def build_execution_config(execution_cfg: Dict, binary: Path) -> ExecutionConfig:
    return ExecutionConfig(
        input_mode=str(execution_cfg.get("input_mode", "file")),
        arg_template=execution_cfg.get("arg_template") if isinstance(execution_cfg.get("arg_template"), str) else None,
        timeout_seconds=float(execution_cfg.get("timeout_seconds", 30)),
        work_dir=Path(str(execution_cfg.get("work_dir", binary.parent))).resolve(),
    )


def build_job_context(job: Dict, job_file: Path) -> JobContext:
    validate_top_level_job(job)

    job_id = str(job["job_id"])
    job_dir = Path(str(job["job_dir"])).resolve()
    coverage_cfg = job["coverage"]
    job_kind = get_job_kind(job)
    processed_format = str(coverage_cfg.get("processed_format", "csv"))
    paths = build_job_paths(job_id, job_dir, job_file.resolve(), processed_format)
    tools = build_tool_config(job["execution"], coverage_cfg, job_kind)
    execution = build_execution_config(job["execution"], tools.binary)

    env = os.environ.copy()
    env["LLVM_PROFILE_FILE"] = str(paths.profraw_path)
    env["COV_DIR"] = str(paths.output_dir)

    return JobContext(
        job=job,
        job_id=job_id,
        paths=paths,
        tools=tools,
        execution=execution,
        coverage=coverage_cfg,
        processed_format=processed_format,
        retention=collect_retention_policy(coverage_cfg),
        env=env,
        worker_log_lines=[],
    )


def sanitize_filename_component(value: object) -> str:
    text = str(value).strip()
    if not text:
        return "unknown"

    sanitized_chars = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            sanitized_chars.append(ch)
        else:
            sanitized_chars.append("_")

    sanitized = "".join(sanitized_chars).strip("._")
    return sanitized or "unknown"


def get_generator_workdir(ctx: Optional[JobContext] = None) -> Path:
    base_dir = Path.cwd() / "generator_workdir"
    if ctx is None:
        return base_dir.resolve()
    return (base_dir / sanitize_filename_component(ctx.job_id)).resolve()


def list_regular_files(base_dir: Path) -> Set[Path]:
    if not base_dir.exists():
        return set()
    return {path.resolve() for path in base_dir.rglob("*") if path.is_file()}


def build_generator_canonical_filename(ctx: JobContext) -> str:
    function_index = ctx.job.get("function_index", "unknown")
    target_line = ctx.job.get("target_line", "unknown")
    target_key = ctx.job.get("target_key")
    if not target_key:
        target_key = "fn{0}_t{1}".format(function_index, target_line)

    model = sanitize_filename_component(ctx.job.get("model", "unknown"))
    safe_target_key = sanitize_filename_component(target_key)
    return "id:{0},line:{1},target:{2},model:{3},mode:generator".format(
        function_index,
        target_line,
        safe_target_key,
        model,
    )


def discover_generated_file(work_dir: Path, script_path: Path, before_files: Set[Path]) -> Path:
    after_files = list_regular_files(work_dir)
    candidates = []
    ignored_suffixes = {".pyc", ".pyo", ".log", ".tmp", ".temp"}

    for path in sorted(after_files - before_files):
        if path == script_path.resolve():
            continue

        try:
            relative = path.relative_to(work_dir.resolve())
        except ValueError:
            continue

        relative_parts = set(relative.parts)
        if "__pycache__" in relative_parts:
            continue
        if path.suffix.lower() in ignored_suffixes:
            continue

        candidates.append(path)

    if not candidates:
        raise JobError("Generator script did not create a new regular file in generator_workdir")
    if len(candidates) != 1:
        raise JobError(
            "Generator script created {0} candidate files in generator_workdir; expected exactly one: {1}".format(
                len(candidates),
                ", ".join(str(path.name) for path in candidates),
            )
        )

    return candidates[0]


def resolve_generator_settings(ctx: JobContext) -> Optional[Dict[str, object]]:
    input_generation = ctx.job.get("input_generation")
    if not isinstance(input_generation, dict):
        return None

    mode = str(input_generation.get("mode", "direct_input")).strip().lower()
    if mode != "generator_script":
        return None

    generator_cfg = input_generation.get("generator_script")
    if not isinstance(generator_cfg, dict):
        raise JobError("input_generation.generator_script must be a JSON object")

    script_file = require_file(
        str(generator_cfg.get("script_file") or ""),
        "input_generation.generator_script.script_file",
    )

    runner = generator_cfg.get("runner", ["python3", "{script_path}"])
    if not isinstance(runner, list) or not runner:
        raise JobError("input_generation.generator_script.runner must be a non-empty list")

    return {
        "script_file": script_file,
        "runner": runner,
        "timeout_seconds": float(generator_cfg.get("timeout_seconds", 30)),
    }


def prepare_input_file(ctx: JobContext, wlog: Callable[[str], None]) -> Path:
    generator = resolve_generator_settings(ctx)
    if generator is None:
        if "input_file" not in ctx.job:
            raise JobError("job.json missing required field: input_file")
        return wait_for_file(str(ctx.job["input_file"]), "input_file")  

    script_file = generator["script_file"]
    runner = generator["runner"]
    timeout_seconds = generator["timeout_seconds"]

    work_dir = get_generator_workdir(ctx)
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    local_script_path = work_dir / script_file.name
    shutil.copy2(script_file, local_script_path)
    before_files = list_regular_files(work_dir)

    rendered_runner = render_command_template(
        runner,
        {
            "script_path": local_script_path,
            "work_dir": work_dir,
        },
    )

    wlog("Generating input inside container")
    wlog("generator_script={0}".format(script_file))
    wlog("generator_work_dir={0}".format(work_dir))
    wlog("generator_command={0}".format(shell_join(rendered_runner)))

    try:
        with ctx.paths.generator_stdout_path.open("wb") as out_f, ctx.paths.generator_stderr_path.open("wb") as err_f:
            try:
                proc = subprocess.run(
                    rendered_runner,
                    cwd=str(work_dir),
                    stdout=out_f,
                    stderr=err_f,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                raise JobError("Generator script timed out after {0} seconds".format(timeout_seconds)) from e

        if proc.returncode != 0:
            raise JobError("Generator script failed with exit code {0}".format(proc.returncode))

        generated_candidate = discover_generated_file(work_dir, local_script_path, before_files)
        canonical_name = build_generator_canonical_filename(ctx)
        ctx.paths.generated_input_dir.mkdir(parents=True, exist_ok=True)
        final_preserved_path = ctx.paths.generated_input_dir / canonical_name
        shutil.copy2(generated_candidate, final_preserved_path)

        ctx.job["input_file"] = str(final_preserved_path)
        wlog("Generator script completed successfully")
        wlog("generated_input_preserved={0}".format(final_preserved_path))
        return final_preserved_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def execute_target_program(ctx: JobContext, input_file: Path, wlog: Callable[[str], None]) -> ProgramRunResult:
    argv, stdin_bytes = build_command(
        ctx.tools.cov_bin,
        ctx.execution.input_mode,
        input_file,
        ctx.execution.arg_template,
    )

    wlog("job_id={0}".format(ctx.job_id))
    wlog("job_file={0}".format(ctx.paths.job_file))
    wlog("input_file={0}".format(input_file))
    wlog("work_dir={0}".format(ctx.execution.work_dir))
    wlog("command={0}".format(shell_join(argv)))
    wlog("retention={0}".format(json.dumps(ctx.retention, ensure_ascii=False, sort_keys=True)))

    start = time.time()
    timed_out = False
    exit_code = None

    wlog("Starting target program execution")
    with ctx.paths.stdout_path.open("wb") as out_f, ctx.paths.stderr_path.open("wb") as err_f:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(ctx.execution.work_dir),
                input=stdin_bytes,
                stdout=out_f,
                stderr=err_f,
                env=ctx.env,
                timeout=ctx.execution.timeout_seconds,
                check=False,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True

    duration = time.time() - start
    crashed = exit_code is not None and exit_code < 0
    wlog(
        "Target program finished: timed_out={0} exit_code={1} crashed={2} duration_seconds={3}".format(
            timed_out,
            exit_code,
            crashed,
            round(duration, 6),
        )
    )

    return ProgramRunResult(
        argv=argv,
        stdin_bytes=stdin_bytes,
        timed_out=timed_out,
        exit_code=exit_code,
        crashed=crashed,
        duration_seconds=round(duration, 6),
    )


def build_execution_doc(ctx: JobContext, run_result: ProgramRunResult) -> Dict:
    return {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "status": "timeout" if run_result.timed_out else "finished",
        "program_exit_code": run_result.exit_code,
        "timed_out": run_result.timed_out,
        "crashed": run_result.crashed,
        "duration_seconds": run_result.duration_seconds,
        "input_mode": ctx.execution.input_mode,
        "command": run_result.argv,
        "stdout_path": str(ctx.paths.stdout_path) if ctx.paths.stdout_path.exists() else None,
        "stderr_path": str(ctx.paths.stderr_path) if ctx.paths.stderr_path.exists() else None,
        "profraw_path": str(ctx.paths.profraw_path) if ctx.paths.profraw_path.exists() else None,
    }


def finalize_outputs(ctx: JobContext, execution_doc: Dict, coverage_doc: Dict) -> None:
    save_json(ctx.paths.execution_json_path, execution_doc)
    save_json(ctx.paths.coverage_json_path, coverage_doc)
    write_worker_log(ctx.paths.worker_log_path, ctx.worker_log_lines)

    apply_retention_policy(
        retention=ctx.retention,
        profraw_path=ctx.paths.profraw_path,
        profdata_path=ctx.paths.profdata_path,
        profjson_path=ctx.paths.profjson_path,
        stdout_path=ctx.paths.stdout_path,
        stderr_path=ctx.paths.stderr_path,
        worker_log_path=ctx.paths.worker_log_path,
        line_cov_path=ctx.paths.line_cov_path,
        branch_cov_path=ctx.paths.branch_cov_path,
        coverage_doc=coverage_doc,
    )

    execution_doc["stdout_path"] = str(ctx.paths.stdout_path) if ctx.paths.stdout_path.exists() else None
    execution_doc["stderr_path"] = str(ctx.paths.stderr_path) if ctx.paths.stderr_path.exists() else None
    execution_doc["profraw_path"] = str(ctx.paths.profraw_path) if ctx.paths.profraw_path.exists() else None

    save_json(ctx.paths.execution_json_path, execution_doc)
    save_json(ctx.paths.coverage_json_path, coverage_doc)


def handle_timeout(ctx: JobContext, execution_doc: Dict, wlog: Callable[[str], None]) -> int:
    wlog("Execution timed out after {0} seconds".format(ctx.execution.timeout_seconds))
    coverage_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "status": "timeout",
        "coverage_collected": False,
        "processed_format": ctx.processed_format,
        "line_coverage_path": None,
        "branch_coverage_path": None,
        "profdata_path": None,
        "profjson_path": None,
        "note": "Program timed out before coverage export",
    }
    finalize_outputs(ctx, execution_doc, coverage_doc)
    wlog("Emitted timeout execution and coverage results")
    return 124


def handle_no_profile(ctx: JobContext, execution_doc: Dict, wlog: Callable[[str], None]) -> int:
    execution_doc["status"] = "finished_no_profile"
    coverage_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "status": "no_profile",
        "coverage_collected": False,
        "processed_format": ctx.processed_format,
        "line_coverage_path": None,
        "branch_coverage_path": None,
        "profdata_path": None,
        "profjson_path": None,
        "note": "LLVM_PROFILE_FILE was not produced or was empty",
    }
    finalize_outputs(ctx, execution_doc, coverage_doc)
    wlog("Emitted no-profile execution and coverage results")
    return 10


def export_coverage_artifacts(ctx: JobContext, wlog: Callable[[str], None]) -> Tuple[Path, Path]:
    wlog("Merging profraw -> profdata")
    subprocess.run(
        [
            str(ctx.tools.llvm_profdata),
            "merge",
            "-sparse",
            str(ctx.paths.profraw_path),
            "-o",
            str(ctx.paths.profdata_path),
        ],
        check=True,
    )

    wlog("Exporting profdata -> profjson")
    with ctx.paths.profjson_path.open("wb") as profjson_f:
        subprocess.run(
            [
                str(ctx.tools.llvm_cov),
                "export",
                str(ctx.tools.cov_bin),
                "-format=text",
                "-instr-profile={0}".format(ctx.paths.profdata_path),
            ],
            stdout=profjson_f,
            check=True,
        )

    wlog("Starting processed coverage extraction")
    subprocess.run(
        [
            sys.executable,
            str(ctx.tools.process_coverage_py),
            "--format",
            ctx.processed_format,
            "--input-file",
            str(ctx.paths.profjson_path),
        ],
        cwd=str(ctx.paths.output_dir),
        env=ctx.env,
        check=True,
    )

    line_cov_path, branch_cov_path = move_processed_coverage_outputs(
        coverage_dir=ctx.paths.coverage_dir,
        processed_format=ctx.processed_format,
    )
    wlog("Coverage extraction finished: line={0} branch={1}".format(line_cov_path, branch_cov_path))
    return line_cov_path, branch_cov_path


def build_finished_coverage_doc(ctx: JobContext, line_cov_path: Path, branch_cov_path: Path) -> Dict:
    return {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "status": "finished",
        "coverage_collected": True,
        "processed_format": ctx.processed_format,
        "line_coverage_path": str(line_cov_path) if line_cov_path.exists() else None,
        "branch_coverage_path": str(branch_cov_path) if branch_cov_path.exists() else None,
        "profdata_path": str(ctx.paths.profdata_path) if ctx.paths.profdata_path.exists() else None,
        "profjson_path": str(ctx.paths.profjson_path) if ctx.paths.profjson_path.exists() else None,
        "note": None,
    }


def gdb_literal(text: object) -> str:
    value = str(text)
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n")


def normalize_probe_expressions(probe: Dict) -> List[str]:
    expressions = probe.get("expressions")
    if expressions is None and "expression" in probe:
        expressions = [probe.get("expression")]
    if not isinstance(expressions, list):
        raise JobError("runtime_probe.probes[].expressions must be a list, or expression must be a string")

    normalized = []
    for expression in expressions:
        text = str(expression).strip()
        if text:
            normalized.append(text)
    if not normalized:
        raise JobError("runtime_probe probe must contain at least one expression")
    return normalized


def normalize_probe_location(probe: Dict) -> str:
    location = probe.get("location") or probe.get("breakpoint")
    if location:
        return str(location).strip()

    source_file = probe.get("source_file") or probe.get("file") or probe.get("c_file")
    line = probe.get("line") or probe.get("breakpoint_line")
    function = probe.get("function") or probe.get("function_name")

    if source_file and line:
        return "{0}:{1}".format(source_file, line)
    if function and line:
        return "{0}:{1}".format(function, line)
    if function:
        return str(function).strip()
    if line:
        return str(line).strip()

    raise JobError("runtime_probe probe is missing location/breakpoint/function/line")


def normalize_runtime_probes(ctx: JobContext) -> List[Dict[str, object]]:
    runtime_probe = ctx.job.get("runtime_probe")
    if not isinstance(runtime_probe, dict):
        raise JobError("runtime_probe must be a JSON object")

    probes = runtime_probe.get("probes")
    if not isinstance(probes, list) or not probes:
        raise JobError("runtime_probe.probes must be a non-empty list")

    normalized = []
    for index, probe in enumerate(probes, start=1):
        if not isinstance(probe, dict):
            raise JobError("runtime_probe.probes[{0}] must be a JSON object".format(index - 1))
        probe_id = str(probe.get("id") or probe.get("name") or "probe_{0:02d}".format(index)).strip()
        normalized.append(
            {
                "id": sanitize_filename_component(probe_id),
                "label": probe_id,
                "location": normalize_probe_location(probe),
                "expressions": normalize_probe_expressions(probe),
                "condition": str(probe.get("condition")).strip() if probe.get("condition") else None,
            }
        )
    return normalized


def resolve_runtime_probe_binary(ctx: JobContext) -> Path:
    runtime_probe = ctx.job.get("runtime_probe") if isinstance(ctx.job.get("runtime_probe"), dict) else {}
    probe_binary = runtime_probe.get("binary") or ctx.job.get("execution", {}).get("runtime_binary")
    if probe_binary:
        return require_executable(str(probe_binary), "runtime_probe.binary")
    return ctx.tools.binary


def build_gdb_command_file(
    ctx: JobContext,
    input_file: Path,
    probes: List[Dict[str, object]],
    wlog: Callable[[str], None],
) -> Tuple[List[str], Path]:
    runtime_probe = ctx.job.get("runtime_probe") if isinstance(ctx.job.get("runtime_probe"), dict) else {}
    probe_binary = resolve_runtime_probe_binary(ctx)
    argv, stdin_bytes = build_command(
        probe_binary,
        ctx.execution.input_mode,
        input_file,
        ctx.execution.arg_template,
    )
    if stdin_bytes is not None:
        raise JobError("runtime_probe jobs currently require file/arg_template input mode; stdin is not supported")

    stop_after_first_hit = bool_config(runtime_probe.get("stop_after_first_hit"), True)
    run_args = shell_join(argv[1:])

    commands = [
        "set pagination off",
        "set confirm off",
        "set print pretty off",
        "set breakpoint pending on",
        "file {0}".format(shlex.quote(str(probe_binary))),
    ]

    for probe in probes:
        break_command = "break {0}".format(probe["location"])
        if probe.get("condition"):
            break_command = "{0} if {1}".format(break_command, probe["condition"])
        commands.append(break_command)
        commands.append("commands")
        commands.append("silent")
        commands.append('printf "__RUNTIME_PROBE_HIT__|{0}|{1}\\n"'.format(
            gdb_literal(probe["id"]),
            gdb_literal(probe["location"]),
        ))
        for expression in probe["expressions"]:
            commands.append('printf "__RUNTIME_EXPR__|{0}|{1}|"'.format(
                gdb_literal(probe["id"]),
                gdb_literal(expression),
            ))
            commands.append("print {0}".format(expression))
        if stop_after_first_hit:
            commands.append("quit")
        else:
            commands.append("continue")
        commands.append("end")

    commands.append("run {0}".format(run_args) if run_args else "run")
    commands.append("quit")
    ctx.paths.gdb_commands_path.write_text("\n".join(commands) + "\n", encoding="utf-8")

    gdb_bin = str(runtime_probe.get("gdb_bin", "gdb"))
    gdb_path = require_executable(gdb_bin, "runtime_probe.gdb_bin")
    gdb_argv = [str(gdb_path), "--batch", "-x", str(ctx.paths.gdb_commands_path)]
    wlog("runtime_probe_binary={0}".format(probe_binary))
    wlog("runtime_probe_gdb_command_file={0}".format(ctx.paths.gdb_commands_path))
    wlog("runtime_probe_command={0}".format(shell_join(gdb_argv)))
    return gdb_argv, probe_binary


def parse_gdb_probe_output(stdout_text: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    hits = []
    observations = []
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if line.startswith("__RUNTIME_PROBE_HIT__|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                hits.append({"probe_id": parts[1], "location": parts[2]})
        elif line.startswith("__RUNTIME_EXPR__|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                value = parts[3].strip()
                if value.startswith("$") and "=" in value:
                    value = value.split("=", 1)[1].strip()
                observations.append(
                    {
                        "probe_id": parts[1],
                        "expression": parts[2],
                        "value": value,
                        "raw": parts[3].strip(),
                    }
                )
    return hits, observations


def run_runtime_probe_job(ctx: JobContext, input_file: Path, wlog: Callable[[str], None]) -> int:
    runtime_probe = ctx.job.get("runtime_probe") if isinstance(ctx.job.get("runtime_probe"), dict) else {}
    probes = normalize_runtime_probes(ctx)
    timeout_seconds = float(runtime_probe.get("timeout_seconds", ctx.execution.timeout_seconds))
    gdb_argv, probe_binary = build_gdb_command_file(ctx, input_file, probes, wlog)

    start = time.time()
    timed_out = False
    exit_code = None

    wlog("Starting runtime probe under gdb")
    with ctx.paths.gdb_stdout_path.open("wb") as out_f, ctx.paths.gdb_stderr_path.open("wb") as err_f:
        try:
            proc = subprocess.run(
                gdb_argv,
                cwd=str(ctx.execution.work_dir),
                stdout=out_f,
                stderr=err_f,
                env=ctx.env,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True

    duration = round(time.time() - start, 6)
    stdout_text = ctx.paths.gdb_stdout_path.read_text(encoding="utf-8", errors="replace") if ctx.paths.gdb_stdout_path.is_file() else ""
    stderr_text = ctx.paths.gdb_stderr_path.read_text(encoding="utf-8", errors="replace") if ctx.paths.gdb_stderr_path.is_file() else ""
    hits, observations = parse_gdb_probe_output(stdout_text)

    execution_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "job_kind": "runtime_probe",
        "status": "timeout" if timed_out else "finished",
        "program_exit_code": exit_code,
        "timed_out": timed_out,
        "crashed": False,
        "duration_seconds": duration,
        "input_mode": ctx.execution.input_mode,
        "command": gdb_argv,
        "stdout_path": str(ctx.paths.gdb_stdout_path) if ctx.paths.gdb_stdout_path.exists() else None,
        "stderr_path": str(ctx.paths.gdb_stderr_path) if ctx.paths.gdb_stderr_path.exists() else None,
        "gdb_commands_path": str(ctx.paths.gdb_commands_path),
    }
    coverage_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "job_kind": "runtime_probe",
        "status": "runtime_probe",
        "coverage_collected": False,
        "processed_format": ctx.processed_format,
        "line_coverage_path": None,
        "branch_coverage_path": None,
        "profdata_path": None,
        "profjson_path": None,
        "note": "Runtime probe job; coverage export was not requested",
    }
    observations_doc = {
        "schema_version": 1,
        "job_id": ctx.job_id,
        "target_key": ctx.job.get("target_key"),
        "base_target_key": ctx.job.get("base_target_key") or ctx.job.get("target_key"),
        "job_kind": "runtime_probe",
        "status": "timeout" if timed_out else "finished",
        "timed_out": timed_out,
        "gdb_exit_code": exit_code,
        "duration_seconds": duration,
        "binary": str(probe_binary),
        "input_file": str(input_file),
        "gdb_commands_path": str(ctx.paths.gdb_commands_path),
        "gdb_stdout_path": str(ctx.paths.gdb_stdout_path),
        "gdb_stderr_path": str(ctx.paths.gdb_stderr_path),
        "probes_requested": probes,
        "probe_hits": hits,
        "observations": observations,
        "raw_stdout_excerpt": stdout_text[:12000],
        "raw_stderr_excerpt": stderr_text[:12000],
    }

    wlog("Runtime probe finished: timed_out={0} exit_code={1} observations={2}".format(
        timed_out,
        exit_code,
        len(observations),
    ))
    save_json(ctx.paths.execution_json_path, execution_doc)
    save_json(ctx.paths.coverage_json_path, coverage_doc)
    save_json(ctx.paths.runtime_observations_path, observations_doc)
    write_worker_log(ctx.paths.worker_log_path, ctx.worker_log_lines)
    return 124 if timed_out else 0


def write_failure_outputs(job_file: Path, job: Dict, status: str, error_message: str, exit_code: int) -> None:
    try:
        job_id = str(job.get("job_id", "unknown"))
        job_dir = Path(str(job.get("job_dir", job_file.parent.parent))).resolve()
        processed_format = str(job.get("coverage", {}).get("processed_format", "csv"))
        paths = build_job_paths(job_id, job_dir, job_file.resolve(), processed_format)

        execution_doc = {
            "schema_version": 1,
            "job_id": job_id,
            "status": status,
            "program_exit_code": None,
            "timed_out": False,
            "crashed": False,
            "duration_seconds": None,
            "input_mode": job.get("execution", {}).get("input_mode"),
            "command": None,
            "stdout_path": str(paths.stdout_path) if paths.stdout_path.exists() else None,
            "stderr_path": str(paths.stderr_path) if paths.stderr_path.exists() else None,
            "profraw_path": str(paths.profraw_path) if paths.profraw_path.exists() else None,
            "error": error_message,
            "runner_exit_code": exit_code,
        }

        coverage_doc = {
            "schema_version": 1,
            "job_id": job_id,
            "status": "failed",
            "coverage_collected": False,
            "processed_format": processed_format,
            "line_coverage_path": None,
            "branch_coverage_path": None,
            "profdata_path": None,
            "profjson_path": None,
            "note": error_message,
        }

        save_json(paths.execution_json_path, execution_doc)
        save_json(paths.coverage_json_path, coverage_doc)

        lines = [
            "[{0}] ERROR: {1}".format(time.strftime("%Y-%m-%d %H:%M:%S"), error_message)
        ]
        write_worker_log(paths.worker_log_path, lines)
    except Exception:
        pass


def run_job(job, job_file):
    ctx = build_job_context(job, job_file)
    wlog = make_worker_logger(ctx.worker_log_lines)

    input_file = prepare_input_file(ctx, wlog)

    if get_job_kind(job) == "runtime_probe":
        return run_runtime_probe_job(ctx, input_file, wlog)

    run_result = execute_target_program(ctx, input_file, wlog)
    execution_doc = build_execution_doc(ctx, run_result)

    if run_result.timed_out:
        return handle_timeout(ctx, execution_doc, wlog)

    if not ctx.paths.profraw_path.is_file() or ctx.paths.profraw_path.stat().st_size == 0:
        return handle_no_profile(ctx, execution_doc, wlog)

    line_cov_path, branch_cov_path = export_coverage_artifacts(ctx, wlog)
    coverage_doc = build_finished_coverage_doc(ctx, line_cov_path, branch_cov_path)
    finalize_outputs(ctx, execution_doc, coverage_doc)
    wlog("Emitted final execution and coverage results")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Run one coverage-instrumented execution job")
    parser.add_argument("--job-file", required=True, help="Path to job.json")
    return parser.parse_args()


def main():
    args = parse_args()
    job_file = Path(args.job_file).resolve()
    job = None

    try:
        job = load_json(job_file)
        return run_job(job, job_file)
    except JobError as e:
        msg = "JobError: {0}".format(e)
        log("ERROR: {0}".format(e))
        if job is None:
            try:
                job = load_json(job_file)
            except Exception:
                job = {}
        write_failure_outputs(job_file, job, "failed", msg, 2)
        return 2
    except subprocess.CalledProcessError as e:
        msg = "subprocess failed with return code {0}: {1}".format(e.returncode, e.cmd)
        log("ERROR: {0}".format(msg))
        if job is None:
            try:
                job = load_json(job_file)
            except Exception:
                job = {}
        write_failure_outputs(job_file, job, "failed", msg, 3)
        return 3
    except Exception as e:
        msg = "unexpected failure: {0}: {1}".format(type(e).__name__, e)
        log("ERROR: {0}".format(msg))
        if job is None:
            try:
                job = load_json(job_file)
            except Exception:
                job = {}
        write_failure_outputs(job_file, job, "failed", msg, 4)
        return 4

if __name__ == "__main__":
    raise SystemExit(main())
