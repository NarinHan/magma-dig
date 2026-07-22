#!/usr/bin/env python3
"""Collect relay runtime observations with gdb.

This script is intended to run inside the target Docker container.  It reads
runtime probe requests from:

  $SHARED/jobs/turn2_results/probe_requests/<target_key>.json

uses the generated inputs from:

  $SHARED/jobs/turn1_results/targets/<target_key>/execution/generated_input/

and writes filled observation JSON files to:

  $SHARED/jobs/turn2_results/runtime_observations/<target_key>.json

The target binary defaults to $OUT/afl/$PROGRAM.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MARKER_HIT = "__RUNTIME_PROBE_HIT__"
MARKER_EXPR = "__RUNTIME_EXPR__"
MARKER_LIMIT = "__RUNTIME_PROBE_LIMIT__"


class ProbeError(Exception):
    pass


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def text_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def int_or_none(value: Any) -> Optional[int]:
    text = text_value(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def default_binary() -> Path:
    out_dir = os.environ.get("OUT")
    program = os.environ.get("PROGRAM")
    if not out_dir or not program:
        raise ProbeError("Set OUT and PROGRAM, or pass --binary explicitly")
    return Path(out_dir) / "afl" / program


def default_jobs_root() -> Path:
    shared = os.environ.get("SHARED")
    if not shared:
        raise ProbeError("Set SHARED, or pass --jobs-root/--turn1-results/--turn2-results")
    return Path(shared) / "jobs"


def find_input_file(turn1_results: Path, target_key: str) -> Path:
    target_dir = turn1_results / "targets" / target_key
    generated_dir = target_dir / "execution" / "generated_input"
    if generated_dir.is_dir():
        files = sorted(path for path in generated_dir.iterdir() if path.is_file())
        if files:
            return files[0]

    job_path = target_dir / "execution" / "job.json"
    if job_path.is_file():
        job = load_json(job_path)
        raw_input = text_value(job.get("input_file"))
        if raw_input:
            candidate = Path(raw_input)
            if candidate.is_file():
                return candidate
            local_candidate = target_dir / "execution" / "generated_input" / candidate.name
            if local_candidate.is_file():
                return local_candidate

    raise ProbeError("Could not locate generated input for target {0}".format(target_key))


def load_template_or_build(turn2_results: Path, target_key: str, probe_request: Dict[str, Any]) -> Dict[str, Any]:
    template_path = turn2_results / "runtime_observations" / (target_key + ".template.json")
    if template_path.is_file():
        return load_json(template_path)

    observations = []
    for probe in probe_request.get("runtime_probes", []):
        if not isinstance(probe, dict):
            continue
        breakpoint = text_value(probe.get("breakpoint") or probe.get("location"))
        if not breakpoint:
            source_file = source_file_name(
                probe.get("breakpoint_source_file")
                or probe.get("source_file")
                or probe.get("breakpoint_file")
                or probe.get("file")
            )
            parsed_source_file, _function = split_source_qualified_function(
                probe.get("breakpoint_function") or probe.get("function") or probe.get("function_name")
            )
            line = int_or_none(probe.get("breakpoint_line") or probe.get("line"))
            if not source_file:
                source_file = parsed_source_file
            if source_file and line is not None and line > 0:
                breakpoint = "{0}:{1}".format(source_file, line)
        observations.append(
            {
                "breakpoint": breakpoint,
                "expression": probe.get("expression"),
                "assumption_being_tested": probe.get("assumption_being_tested"),
                "expected_value": probe.get("expected_value"),
                "confidence": probe.get("confidence"),
                "uncertainty_reason": probe.get("uncertainty_reason"),
                "observed_value": "",
                "observed_values": [],
                "available": False,
                "notes": "",
            }
        )
    return {"target_key": target_key, "source": "automatic_gdb", "observations": observations}


def source_basename_from_artifact(turn2_results: Path, target_key: str) -> Optional[str]:
    artifact_path = turn2_results / "targets" / target_key / "artifact.json"
    if not artifact_path.is_file():
        return None
    artifact = load_json(artifact_path)
    source_path = text_value(artifact.get("source_path") or artifact.get("c_file"))
    if not source_path:
        return None
    return Path(source_path).name


def gdb_printf_literal(value: object) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n")


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = text_value(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def source_file_name(value: Any) -> str:
    text = text_value(value)
    if not text:
        return ""
    return Path(text).name


def split_source_qualified_function(value: Any) -> Tuple[str, str]:
    text = text_value(value)
    if ":" not in text:
        return "", text

    left, right = text.split(":", 1)
    source_file = source_file_name(left)
    lower_source_file = source_file.lower()
    if not lower_source_file.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp")):
        return "", text

    return source_file, right.strip()


def candidate_locations(
    probe: Dict[str, Any],
    target_source_basename: Optional[str],
) -> List[str]:
    breakpoint = text_value(probe.get("breakpoint") or probe.get("location"))
    if breakpoint:
        return [breakpoint]

    raw_function = text_value(probe.get("breakpoint_function") or probe.get("function") or probe.get("function_name"))
    source_file = source_file_name(
        probe.get("breakpoint_source_file")
        or probe.get("source_file")
        or probe.get("breakpoint_file")
        or probe.get("file")
    )
    parsed_source_file, function = split_source_qualified_function(raw_function)
    if not source_file:
        source_file = parsed_source_file
    line = int_or_none(probe.get("breakpoint_line") or probe.get("line"))

    candidates: List[str] = []
    if line is not None and line > 1:
        if source_file:
            candidates.append("{0}:{1}".format(source_file, line))
        elif target_source_basename:
            candidates.append("{0}:{1}".format(target_source_basename, line))
        if function and not source_file:
            candidates.append("{0}:{1}".format(function, line))
    if source_file and function:
        candidates.append("{0}:{1}".format(source_file, function))
    if function:
        candidates.append(function)
    if raw_function and raw_function != function:
        candidates.append(raw_function)
    if line is not None and line > 1 and not source_file and not function:
        candidates.append(str(line))
    return unique_keep_order(candidates)


def build_gdb_script(
    script_path: Path,
    binary: Path,
    input_file: Path,
    probe_id: str,
    location: str,
    expression: str,
    max_hits_per_probe: int,
) -> None:
    commands = [
        "set pagination off",
        "set confirm off",
        "set print pretty off",
        "set print elements 128",
        "set print repeats 16",
        "set breakpoint pending on",
        "file {0}".format(shlex.quote(str(binary))),
        "set $runtime_probe_hits = 0",
        "break {0}".format(location),
        "commands",
        "silent",
        "set $runtime_probe_hits = $runtime_probe_hits + 1",
        'printf "{0}|{1}|{2}|%d\\n", $runtime_probe_hits'.format(
            MARKER_HIT,
            gdb_printf_literal(probe_id),
            gdb_printf_literal(location),
        ),
        'printf "{0}|{1}|%d|", $runtime_probe_hits'.format(MARKER_EXPR, gdb_printf_literal(probe_id)),
        "print {0}".format(expression),
    ]
    if max_hits_per_probe > 0:
        commands.extend(
            [
                "if $runtime_probe_hits >= {0}".format(max_hits_per_probe),
                'printf "{0}|{1}|%d\\n", $runtime_probe_hits'.format(
                    MARKER_LIMIT,
                    gdb_printf_literal(probe_id),
                ),
                "quit",
                "end",
            ]
        )
    commands.extend(
        [
            "continue",
            "end",
            "run {0}".format(shlex.quote(str(input_file))),
            "quit",
        ]
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("\n".join(commands) + "\n", encoding="utf-8")


def parse_gdb_output(
    stdout_text: str,
    stderr_text: str,
    probe_id: str,
    location: str,
) -> Tuple[bool, Optional[str], List[Dict[str, Any]], int, bool, str]:
    hit = False
    hit_count = 0
    hit_limit_reached = False
    observed_values: List[Dict[str, Any]] = []
    notes: List[str] = []

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if line.startswith(MARKER_HIT + "|"):
            hit = True
            parts = line.split("|", 3)
            if len(parts) >= 4 and parts[1] == probe_id:
                hit_index = int_or_none(parts[3])
                if hit_index is not None:
                    hit_count = max(hit_count, hit_index)
            else:
                hit_count += 1
        elif line.startswith(MARKER_EXPR + "|"):
            parts = line.split("|", 3)
            if len(parts) == 4 and parts[1] == probe_id:
                hit_index = int_or_none(parts[2])
                value = parts[3].strip()
                if value.startswith("$") and "=" in value:
                    value = value.split("=", 1)[1].strip()
                observed_values.append(
                    {
                        "hit_index": hit_index if hit_index is not None else len(observed_values) + 1,
                        "value": value,
                    }
                )
            else:
                old_parts = line.split("|", 2)
                if len(old_parts) == 3 and old_parts[1] == probe_id:
                    value = old_parts[2].strip()
                    if value.startswith("$") and "=" in value:
                        value = value.split("=", 1)[1].strip()
                    observed_values.append({"hit_index": len(observed_values) + 1, "value": value})
        elif line.startswith(MARKER_LIMIT + "|"):
            parts = line.split("|", 2)
            if len(parts) >= 2 and parts[1] == probe_id:
                hit_limit_reached = True
                if len(parts) == 3:
                    hit_index = int_or_none(parts[2])
                    if hit_index is not None:
                        hit_count = max(hit_count, hit_index)

    hit_count = max(hit_count, len(observed_values))
    observed_value = observed_values[0]["value"] if observed_values else None

    if hit:
        notes.append("breakpoint hit {0} time(s)".format(hit_count))
    if observed_values:
        notes.append("captured {0} observed value(s)".format(len(observed_values)))
    if hit_limit_reached:
        notes.append("max hits per probe reached before program exit")
    if not hit:
        notes.append("breakpoint was not reached")
    if observed_value is None and hit:
        notes.append("expression was not evaluated successfully")

    stderr_excerpt = "\n".join(stderr_text.strip().splitlines()[:8]).strip()
    if stderr_excerpt:
        notes.append("gdb stderr from breakpoint {0}: {1}".format(location, stderr_excerpt[:1000]))

    return hit, observed_value, observed_values, hit_count, hit_limit_reached, "; ".join(notes)


def run_one_probe(
    *,
    gdb_bin: str,
    binary: Path,
    input_file: Path,
    probe: Dict[str, Any],
    probe_id: str,
    target_source_basename: Optional[str],
    run_dir: Path,
    timeout_seconds: float,
    max_hits_per_probe: int,
) -> Dict[str, Any]:
    expression = text_value(probe.get("expression"))
    if not expression:
        return {
            "available": False,
            "observed_value": "",
            "observed_values": [],
            "attempted_breakpoints": [],
            "primary_breakpoint": "",
            "notes": "probe did not contain an expression",
            "gdb": {},
        }

    locations = candidate_locations(probe, target_source_basename)
    if not locations:
        return {
            "available": False,
            "observed_value": "",
            "observed_values": [],
            "attempted_breakpoints": [],
            "primary_breakpoint": "",
            "notes": "probe did not contain a breakpoint",
            "gdb": {},
        }

    attempts = []
    for attempt_index, location in enumerate(locations, start=1):
        attempt_id = "{0}__attempt_{1:02d}".format(probe_id, attempt_index)
        script_path = run_dir / (attempt_id + ".gdb")
        stdout_path = run_dir / (attempt_id + ".stdout.txt")
        stderr_path = run_dir / (attempt_id + ".stderr.txt")
        build_gdb_script(script_path, binary, input_file, probe_id, location, expression, max_hits_per_probe)

        start = time.time()
        timed_out = False
        returncode: Optional[int] = None
        with stdout_path.open("wb") as stdout_f, stderr_path.open("wb") as stderr_f:
            try:
                proc = subprocess.run(
                    [gdb_bin, "--batch", "-x", str(script_path)],
                    stdout=stdout_f,
                    stderr=stderr_f,
                    timeout=timeout_seconds,
                    check=False,
                )
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True

        duration = round(time.time() - start, 6)
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        hit, observed_value, observed_values, hit_count, hit_limit_reached, notes = parse_gdb_output(
            stdout_text,
            stderr_text,
            probe_id,
            location,
        )
        distinct_observed_values = unique_keep_order(item.get("value") for item in observed_values)

        attempt_doc = {
            "location": location,
            "breakpoint_command": "break {0}".format(location),
            "timed_out": timed_out,
            "gdb_exit_code": returncode,
            "duration_seconds": duration,
            "hit": hit,
            "hit_count": hit_count,
            "hit_limit_reached": hit_limit_reached,
            "observed_value": observed_value,
            "observed_values": observed_values,
            "distinct_observed_values": distinct_observed_values,
            "observed_value_count": len(observed_values),
            "notes": notes,
        }
        attempts.append(attempt_doc)

        if hit and observed_values:
            return {
                "available": True,
                "observed_value": observed_value,
                "observed_values": observed_values,
                "distinct_observed_values": distinct_observed_values,
                "hit_count": hit_count,
                "hit_limit_reached": hit_limit_reached,
                "attempted_breakpoints": locations,
                "primary_breakpoint": locations[0] if locations else "",
                "gdb_attempt_count": len(attempts),
                "notes": notes,
                "gdb": attempt_doc,
                "attempts": attempts,
            }

    best = attempts[0] if attempts else {}
    last = attempts[-1] if attempts else {}
    attempted_breakpoints = [str(item.get("location") or "") for item in attempts if item.get("location")]
    failure_notes = text_value(last.get("notes") or best.get("notes") or "no gdb attempts were run")
    if attempted_breakpoints:
        failure_notes = "all breakpoint attempts failed; attempted breakpoints in order: {0}; last attempt notes: {1}".format(
            ", ".join(attempted_breakpoints),
            failure_notes,
        )
    return {
        "available": False,
        "observed_value": "",
        "observed_values": [],
        "distinct_observed_values": [],
        "hit_count": int(last.get("hit_count") or best.get("hit_count") or 0),
        "hit_limit_reached": bool(last.get("hit_limit_reached") or best.get("hit_limit_reached")),
        "attempted_breakpoints": attempted_breakpoints,
        "primary_breakpoint": attempted_breakpoints[0] if attempted_breakpoints else "",
        "gdb_attempt_count": len(attempts),
        "notes": failure_notes,
        "gdb": last,
        "attempts": attempts,
    }


def merge_probe_result(observation: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(observation)
    merged["observed_value"] = result.get("observed_value", "")
    merged["observed_values"] = result.get("observed_values", [])
    merged["distinct_observed_values"] = result.get("distinct_observed_values", [])
    merged["hit_count"] = int(result.get("hit_count") or 0)
    merged["observed_value_count"] = len(merged["observed_values"])
    merged["hit_limit_reached"] = bool(result.get("hit_limit_reached"))
    merged["attempted_breakpoints"] = result.get("attempted_breakpoints", [])
    merged["primary_breakpoint"] = result.get("primary_breakpoint", "")
    merged["gdb_attempt_count"] = int(result.get("gdb_attempt_count") or 0)
    merged["available"] = bool(result.get("available"))
    merged["notes"] = result.get("notes", "")
    merged["gdb"] = result.get("gdb", {})
    return merged


def collect_for_target(
    *,
    target_key: str,
    turn1_results: Path,
    turn2_results: Path,
    binary: Path,
    gdb_bin: str,
    timeout_seconds: float,
    max_hits_per_probe: int,
    overwrite: bool,
) -> Dict[str, Any]:
    probe_path = turn2_results / "probe_requests" / (target_key + ".json")
    if not probe_path.is_file():
        raise ProbeError("Missing probe request: {0}".format(probe_path))

    output_path = turn2_results / "runtime_observations" / (target_key + ".json")
    if output_path.is_file() and not overwrite:
        return {"target_key": target_key, "status": "skipped_existing", "output_path": str(output_path)}

    probe_request = load_json(probe_path)
    probes = [probe for probe in probe_request.get("runtime_probes", []) if isinstance(probe, dict)]
    template = load_template_or_build(turn2_results, target_key, probe_request)
    input_file = find_input_file(turn1_results, target_key)
    target_source_basename = source_basename_from_artifact(turn2_results, target_key)
    run_dir = turn2_results / "gdb_runs" / target_key
    run_dir.mkdir(parents=True, exist_ok=True)

    observations = []
    for index, observation in enumerate(template.get("observations", []), start=1):
        probe = probes[index - 1] if index - 1 < len(probes) else observation
        observation_doc = dict(observation)
        breakpoint = text_value(observation_doc.get("breakpoint") or probe.get("breakpoint") or probe.get("location"))
        if breakpoint:
            observation_doc["breakpoint"] = breakpoint
        probe_id = "probe_{0:02d}".format(index)
        result = run_one_probe(
            gdb_bin=gdb_bin,
            binary=binary,
            input_file=input_file,
            probe=probe,
            probe_id=probe_id,
            target_source_basename=target_source_basename,
            run_dir=run_dir,
            timeout_seconds=timeout_seconds,
            max_hits_per_probe=max_hits_per_probe,
        )
        observations.append(merge_probe_result(observation_doc, result))

    doc = dict(template)
    doc["target_key"] = target_key
    doc["source"] = "automatic_gdb"
    doc["observations"] = observations
    doc["runtime_probe_request_path"] = str(probe_path)
    doc["input_file"] = str(input_file)
    doc["binary"] = str(binary)
    doc["gdb_bin"] = gdb_bin
    doc["gdb_run_dir"] = str(run_dir)
    doc["max_hits_per_probe"] = max_hits_per_probe
    doc["available_observation_count"] = sum(1 for item in observations if item.get("available"))
    doc["observation_count"] = len(observations)
    save_json(output_path, doc)
    return {
        "target_key": target_key,
        "status": "written",
        "output_path": str(output_path),
        "available": doc["available_observation_count"],
        "total": doc["observation_count"],
    }


def discover_targets(turn2_results: Path, requested: Sequence[str]) -> List[str]:
    if requested:
        return sorted(set(requested))
    probe_dir = turn2_results / "probe_requests"
    return sorted(path.stem for path in probe_dir.glob("*.json") if path.is_file())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill relay runtime_observations JSON files by running gdb.")
    parser.add_argument("--jobs-root", type=Path, default=None, help="Root containing turn1_results and turn2_results. Defaults to $SHARED/jobs.")
    parser.add_argument("--turn1-results", type=Path, default=None, help="Path to turn1_results. Defaults to <jobs-root>/turn1_results.")
    parser.add_argument("--turn2-results", type=Path, default=None, help="Path to turn2_results. Defaults to <jobs-root>/turn2_results.")
    parser.add_argument("--binary", type=Path, default=None, help="Target binary. Defaults to $OUT/afl/$PROGRAM.")
    parser.add_argument("--gdb", default="gdb", help="gdb executable. Defaults to gdb.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Timeout in seconds per probe attempt.")
    parser.add_argument("--max-hits-per-probe", type=int, default=200, help="Maximum breakpoint hits to record per probe attempt. Use 0 for no scripted hit cap.")
    parser.add_argument("--target", action="append", default=[], help="Target key to process. May be repeated. Defaults to all probe_requests/*.json.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing runtime_observations/<target>.json files.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        jobs_root = args.jobs_root or default_jobs_root()
        turn1_results = args.turn1_results or (jobs_root / "turn1_results")
        turn2_results = args.turn2_results or (jobs_root / "turn2_results")
        binary = args.binary or default_binary()
        if not binary.is_file():
            raise ProbeError("Binary does not exist: {0}".format(binary))
        if args.max_hits_per_probe < 0:
            raise ProbeError("--max-hits-per-probe must be zero or positive")
        if not turn1_results.is_dir():
            raise ProbeError("turn1_results does not exist: {0}".format(turn1_results))
        if not turn2_results.is_dir():
            raise ProbeError("turn2_results does not exist: {0}".format(turn2_results))

        results = []
        for target_key in discover_targets(turn2_results, args.target):
            print("collecting {0}".format(target_key), flush=True)
            result = collect_for_target(
                target_key=target_key,
                turn1_results=turn1_results,
                turn2_results=turn2_results,
                binary=binary,
                gdb_bin=args.gdb,
                timeout_seconds=args.timeout,
                max_hits_per_probe=args.max_hits_per_probe,
                overwrite=args.overwrite,
            )
            results.append(result)
            print(
                "{target_key}: {status} available={available}/{total} output={output_path}".format(
                    target_key=result.get("target_key"),
                    status=result.get("status"),
                    available=result.get("available", "-"),
                    total=result.get("total", "-"),
                    output_path=result.get("output_path", ""),
                ),
                flush=True,
            )

        summary_path = turn2_results / "runtime_observations" / "automatic_gdb_summary.json"
        save_json(summary_path, {"results": results})
        print("summary: {0}".format(summary_path), flush=True)
        return 0
    except Exception as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
