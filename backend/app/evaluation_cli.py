"""Run with: python -m app.evaluation_cli --gold gold.json --predictions predictions.json."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from app.evaluation import (
    EvaluationConfig,
    evaluate_dataset,
    load_gold,
    load_predictions,
    render_markdown,
    write_machine_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local extraction evaluation; not official scoring"
    )
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--json-output", type=Path, default=Path("evaluation-report.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("evaluation-report.md"))
    parser.add_argument(
        "--allow-draft", action="store_true", help="Explicit provisional draft preview"
    )
    parser.add_argument("--money-tolerance", type=float, default=0.01)
    parser.add_argument("--quantity-tolerance", type=float, default=0.000001)
    parser.add_argument("--relative-tolerance", type=float, default=1e-9)
    args = parser.parse_args(argv)
    try:
        inputs = {args.gold.resolve(), args.predictions.resolve()}
        outputs = {args.json_output.resolve(), args.markdown_output.resolve()}
        if len(inputs) != 2:
            raise ValueError("gold and predictions must be separate files")
        if len(outputs) != 2 or inputs & outputs:
            raise ValueError("report paths must be distinct and must not overwrite input files")
        report = evaluate_dataset(
            load_gold(args.gold),
            load_predictions(args.predictions),
            EvaluationConfig(
                monetary_absolute_tolerance=args.money_tolerance,
                quantity_absolute_tolerance=args.quantity_tolerance,
                relative_tolerance=args.relative_tolerance,
            ),
            allow_draft=args.allow_draft,
        )
        write_machine_report(report, args.json_output)
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Local evaluation JSON: {args.json_output}")
    print(f"Local evaluation Markdown: {args.markdown_output}")
    print("Local proxy only; not an official competition score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
