from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from pipeline.graph import run_pipeline
from pipeline.models import PipelineState


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Agentic OpenAPI contract test pipeline")
    parser.add_argument("--spec", required=True, help="Path to OpenAPI YAML/JSON")
    parser.add_argument("--base-url", required=True, help="API base URL for generated tests")
    parser.add_argument("--out", required=True, help="Output path for generated pytest module")
    parser.add_argument("--report-out", required=True, help="Output path for markdown report")
    parser.add_argument("--reset-path", help="Reset endpoint path for test isolation (e.g., /__test/reset)")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    args = parser.parse_args(argv)

    spec_path = str(Path(args.spec).resolve())
    code_path = str(Path(args.out).resolve())
    report_path = str(Path(args.report_out).resolve())

    initial = PipelineState(
        spec_path=spec_path,
        base_url=args.base_url.rstrip("/"),
        code_path=code_path,
        report_path=report_path,
        reset_path=args.reset_path or None,
        max_iterations=args.max_iterations,
        model=args.model,
    )

    final = run_pipeline(initial)
    print(f"Generated tests: {final.code_path}")
    print(f"Report: {report_path}")
    print(f"Iteration count: {final.iteration}")
    real_bugs = [t for t in final.triage if t.category == "real_bug"]
    if real_bugs:
        print(f"Contract violations found: {len(real_bugs)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
