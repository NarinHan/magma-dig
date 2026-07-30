#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
from pathlib import Path


BRANCH_KIND = 3


class CoverageProcessingError(RuntimeError):
    pass


def load_json(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CoverageProcessingError(
            "Expected one JSON object in {0}".format(path)
        )
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


def extract_executed_lines(coverage_json):
    result = {}

    def add_line(line_counts, line, count):
        if (
            isinstance(line, int)
            and line > 0
            and isinstance(count, int)
            and count > 0
        ):
            line_counts[line] = max(line_counts.get(line, 0), count)

    for data_item in coverage_json.get("data", []):
        for file_item in data_item.get("files", []):
            filename = file_item.get("filename")
            if not filename:
                continue
            line_counts = result.setdefault(filename, {})
            segments = [
                segment
                for segment in file_item.get("segments", [])
                if isinstance(segment, list) and len(segment) >= 4
            ]
            for index, segment in enumerate(segments):
                line = segment[0]
                count = segment[2]
                has_count = segment[3]
                if not (
                    has_count
                    and isinstance(count, int)
                    and count > 0
                ):
                    continue
                add_line(line_counts, line, count)
                if index + 1 >= len(segments):
                    continue
                next_line = segments[index + 1][0]
                if not isinstance(next_line, int) or next_line <= line:
                    continue
                for covered_line in range(line + 1, next_line):
                    add_line(line_counts, covered_line, count)

    return {
        filename: line_counts
        for filename, line_counts in result.items()
        if line_counts
    }


def extract_executed_branch_regions(coverage_json):
    result = {}

    def add_region(filename, region):
        if not isinstance(region, list) or len(region) < 8:
            return
        count = region[4]
        if region[7] != BRANCH_KIND or not isinstance(count, int) or count <= 0:
            return
        result.setdefault(filename, []).append(
            {
                "line_start": region[0],
                "col_start": region[1],
                "line_end": region[2],
                "col_end": region[3],
                "count": count,
            }
        )

    for data_item in coverage_json.get("data", []):
        for file_item in data_item.get("files", []):
            filename = file_item.get("filename")
            if not filename:
                continue
            for region in file_item.get("regions", []) or []:
                add_region(filename, region)
            for expansion in file_item.get("expansions", []) or []:
                for region in expansion.get("target_regions", []) or []:
                    add_region(filename, region)

    cleaned = {}
    for filename, regions in result.items():
        seen = set()
        unique = []
        for region in regions:
            key = (
                region["line_start"],
                region["col_start"],
                region["line_end"],
                region["col_end"],
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(region)
        unique.sort(key=lambda item: (item["line_start"], item["col_start"]))
        cleaned[filename] = unique
    return cleaned


def coverage_seed_name(profjson_path):
    name = Path(profjson_path).name
    if name.endswith(".prof.json"):
        return name[: -len(".prof.json")]
    if name.endswith(".json"):
        return name[: -len(".json")]
    return name


def write_line_coverage(path, processed_format, seed, line_coverage):
    if processed_format == "json":
        save_json_atomic(
            path,
            {
                "seed": seed,
                "files": [
                    {
                        "filename": filename,
                        "lines": [
                            {"line": line, "count": count}
                            for line, count in sorted(
                                line_coverage[filename].items()
                            )
                        ],
                    }
                    for filename in sorted(line_coverage)
                ],
            },
        )
        return

    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["filename", "line", "count"])
        for filename in sorted(line_coverage):
            for line, count in sorted(line_coverage[filename].items()):
                writer.writerow([filename, line, count])


def write_branch_coverage(path, processed_format, seed, branch_coverage):
    if processed_format == "json":
        save_json_atomic(
            path,
            {
                "seed": seed,
                "files": [
                    {
                        "filename": filename,
                        "branches": branch_coverage[filename],
                    }
                    for filename in sorted(branch_coverage)
                ],
            },
        )
        return

    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "filename",
                "line_start",
                "col_start",
                "line_end",
                "col_end",
                "count",
            ]
        )
        for filename in sorted(branch_coverage):
            for branch in branch_coverage[filename]:
                writer.writerow(
                    [
                        filename,
                        branch["line_start"],
                        branch["col_start"],
                        branch["line_end"],
                        branch["col_end"],
                        branch["count"],
                    ]
                )


def process_coverage_export(profjson_path, output_dir, processed_format):
    processed_format = str(processed_format).strip().lower()
    if processed_format not in ("csv", "json"):
        raise CoverageProcessingError(
            "format must be csv or json: {0}".format(processed_format)
        )

    coverage_json = load_json(profjson_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    seed = coverage_seed_name(profjson_path)
    line_path = output_dir / "line.{0}".format(processed_format)
    branch_path = output_dir / "branch.{0}".format(processed_format)
    write_line_coverage(
        line_path,
        processed_format,
        seed,
        extract_executed_lines(coverage_json),
    )
    write_branch_coverage(
        branch_path,
        processed_format,
        seed,
        extract_executed_branch_regions(coverage_json),
    )
    return line_path, branch_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert one llvm-cov export into line and branch coverage."
    )
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        line_path, branch_path = process_coverage_export(
            profjson_path=Path(args.input_file),
            output_dir=Path(args.output_dir),
            processed_format=args.format,
        )
    except (CoverageProcessingError, OSError, ValueError) as error:
        print(
            "Coverage processing failed: {0}".format(error),
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        print(
            json.dumps(
                {
                    "line_coverage_path": str(line_path),
                    "branch_coverage_path": str(branch_path),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
