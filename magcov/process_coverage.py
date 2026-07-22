import csv
import json
import os
import sys
import argparse
from pathlib import Path

BRANCH_KIND = 3  # llvm-cov region kind for branches

def log(msg):
    print(f"[INFO] {msg}")

def log_error(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)

def normalize_seed_base(filename):
    if filename.endswith(".prof.json"):
        return filename[:-len(".prof.json")]
    if filename.endswith(".json"):
        return filename[:-len(".json")]
    return filename

# ---------------- LINE EXTRACTION ----------------

def extract_executed_lines(cov_json):
    out = {}

    def add_line(line_counts, line, count):
        if isinstance(line, int) and line > 0 and isinstance(count, int) and count > 0:
            line_counts[line] = max(line_counts.get(line, 0), count)

    for b in cov_json.get("data", []):
        for fobj in b.get("files", []):
            fname = fobj.get("filename")
            if not fname:
                continue

            line_counts = out.setdefault(fname, {})

            segments = [
                seg for seg in fobj.get("segments", [])
                if isinstance(seg, list) and len(seg) >= 4
            ]

            # llvm-cov export segments are coverage change points, not per-line rows.
            # A covered segment starting on line N can span multiple source lines until
            # the next segment begins, so record the whole covered line range.
            for idx, seg in enumerate(segments):
                line, count, hasCount = seg[0], seg[2], seg[3]
                if not (hasCount and isinstance(count, int) and count > 0):
                    continue

                add_line(line_counts, line, count)

                if idx + 1 >= len(segments):
                    continue

                next_line = segments[idx + 1][0]
                if not isinstance(next_line, int) or next_line <= line:
                    continue

                for covered_line in range(line + 1, next_line):
                    add_line(line_counts, covered_line, count)

    return {fn: lc for fn, lc in out.items() if lc}

# ---------------- BRANCH EXTRACTION ----------------

def extract_executed_branch_regions(cov_json):
    out = {}

    def add_region(fname, arr):
        if not (isinstance(arr, list) and len(arr) >= 8):
            return
        ls, cs, le, ce, cnt, kind = arr[0], arr[1], arr[2], arr[3], arr[4], arr[7]
        if kind != BRANCH_KIND or cnt <= 0:
            return

        out.setdefault(fname, []).append({
            "line_start": ls,
            "col_start": cs,
            "line_end": le,
            "col_end": ce,
            "count": cnt,
        })

    for b in cov_json.get("data", []):
        for fobj in b.get("files", []):
            fname = fobj.get("filename")
            if not fname:
                continue

            # direct regions (if present)
            for r in fobj.get("regions", []) or []:
                add_region(fname, r)

            # expansions (your case)
            for ex in fobj.get("expansions", []) or []:
                for r in ex.get("target_regions", []) or []:
                    add_region(fname, r)

    # deduplicate and sort
    cleaned = {}
    for fn, lst in out.items():
        seen = set()
        uniq = []
        for br in lst:
            key = (br["line_start"], br["col_start"], br["line_end"], br["col_end"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(br)
        uniq.sort(key=lambda x: (x["line_start"], x["col_start"]))
        cleaned[fn] = uniq

    return cleaned

# ---------------- WRITERS ----------------

def write_lines_json(path, seed, lines_cov):
    obj = {
        "seed": seed,
        "files": [
            {"filename": fn, "lines": [{"line": l, "count": c} for l, c in sorted(lines_cov[fn].items())]}
            for fn in sorted(lines_cov.keys())
        ]
    }
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)

def write_branches_json(path, seed, br_cov):
    obj = {
        "seed": seed,
        "files": [
            {"filename": fn, "branches": br_cov[fn]}
            for fn in sorted(br_cov.keys())
        ]
    }
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)

def write_lines_csv(path, lines_cov):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "line", "count"])
        for fn in sorted(lines_cov.keys()):
            for l, c in sorted(lines_cov[fn].items()):
                w.writerow([fn, l, c])

def write_branches_csv(path, br_cov):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "line_start", "col_start", "line_end", "col_end", "count"])
        for fn in sorted(br_cov.keys()):
            for br in br_cov[fn]:
                w.writerow([fn, br["line_start"], br["col_start"], br["line_end"], br["col_end"], br["count"]])

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Process llvm-cov JSON to extract line and branch coverage.")
    parser.add_argument("--format", choices=["json", "csv"], default="csv",
                        help="Output format: json or csv (default: csv)")
    parser.add_argument("--input-file", type=str, default=None,
                        help="Process a single llvm-cov JSON file (path). If omitted, process all in $COV_DIR/profjson.")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory. Default: $COV_DIR/coverage")
    args = parser.parse_args()

    cov_dir = os.environ.get("COV_DIR")
    if not cov_dir:
        log_error("Please set COV_DIR environment variable")
        sys.exit(1)

    cov_dir = Path(cov_dir)
    input_dir = cov_dir / "profjson"

    output_dir = Path(args.output_dir) if args.output_dir else (cov_dir / "coverage")
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Output format: {args.format}")

    # Determine input files
    if args.input_file:
        jp = Path(args.input_file)
        files = [jp]
        log(f"Reading single file: {jp}")
    else:
        files = sorted(input_dir.glob("*.json"))
        log(f"Reading from: {input_dir}")

    log(f"Writing to: {output_dir}")
    log(f"Found {len(files)} file(s)")

    for i, jp in enumerate(files, 1):
        seed = normalize_seed_base(jp.name)
        log(f"[{i}/{len(files)}] Processing {jp.name}")

        try:
            with open(jp) as f:
                cov_json = json.load(f)
        except Exception as e:
            log_error(f"Failed to read {jp}: {e}")
            continue

        lines_cov = extract_executed_lines(cov_json)
        branch_cov = extract_executed_branch_regions(cov_json)

        if args.format == "json":
            write_lines_json(output_dir / f"{seed}.line.json", seed, lines_cov)
            write_branches_json(output_dir / f"{seed}.branch.json", seed, branch_cov)
        else:
            write_lines_csv(output_dir / f"{seed}.line.csv", lines_cov)
            write_branches_csv(output_dir / f"{seed}.branch.csv", branch_cov)

        log(f"  -> written {seed}.line.{args.format} and {seed}.branch.{args.format}")

    log("Done.")

if __name__ == "__main__":
    main()

