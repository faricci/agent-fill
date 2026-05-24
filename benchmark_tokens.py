"""
benchmark_tokens.py

Compares two ways of generating the interview prep document:

    A) Baseline:  CV + hiring brief + question bank + full template,
                  asked to fill ALL AGENT-FILL markers (pick from the bank
                  for question slots, generate for the others).

    B) Hybrid:    populate_deterministic.py first fills the script slots
                  and appends the pre-filtered shortlist. The LLM only
                  fills the reasoning slots, picking questions from the
                  already-filtered shortlist.

Both prompts instruct the model to never invent questions — only pick from
the provided pool. The difference is what pool the model sees: the full
bank (baseline) or the pre-filtered shortlist (hybrid).

Each run saves the full model output for manual review:
    outputs/result_baseline.md
    outputs/result_hybrid.md
"""

import time
from pathlib import Path

import anthropic
from anthropic import APIStatusError, APIConnectionError, RateLimitError

MODEL = "claude-sonnet-4-6"

MAX_RETRIES = 5
INITIAL_BACKOFF = 4
MAX_OUTPUT_TOKENS = 8192

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "inputs"
OUTPUTS = ROOT / "outputs"

client = anthropic.Anthropic()


def load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_baseline_prompt() -> str:
    cv = load(INPUTS / "cv.md")
    brief = load(INPUTS / "hiring_brief.md")
    template = load(INPUTS / "template.md")
    bank = load(INPUTS / "question_bank.yaml")
    return (
        "You are preparing an interview prep document for an engineering "
        "candidate.\n\n"
        "Fill every AGENT-FILL comment in the template using the CV and "
        "hiring brief as source data, and the question bank as the only "
        "source of questions.\n\n"
        "STRICT RULES:\n"
        "- For any slot that requests questions (warmup / technical / "
        "trap), pick questions ONLY from the question bank below. "
        "Write them out verbatim with their expected responses. "
        "Do not invent new questions and do not modify their wording.\n"
        "- For the coverage matrix, years snapshot, and flag extraction, "
        "use only what the CV evidences. Do not infer beyond the text.\n"
        "- Also replace [CANDIDATE_NAME] and [DATE] placeholders.\n\n"
        f"CV:\n{cv}\n\n"
        f"Hiring brief:\n{brief}\n\n"
        f"Question bank:\n```yaml\n{bank}\n```\n\n"
        f"Template:\n{template}"
    )


def build_hybrid_prompt() -> str:
    half = OUTPUTS / "template_half_populated.md"
    if not half.exists():
        raise SystemExit(
            "Run `python populate_deterministic.py` first to generate "
            "outputs/template_half_populated.md."
        )
    template = load(half)
    return (
        "Replace every remaining AGENT-FILL comment in the template below "
        "with the content described by its inline instruction.\n\n"
        "STRICT RULES:\n"
        "- For question slots (warmup / technical / trap), pick questions "
        "ONLY from the SHORTLIST block at the end of the template. "
        "Write them out verbatim with their expected responses. "
        "Do not invent new questions and do not modify their wording.\n"
        "- For flag_interpretation and interview_focus, reason from the "
        "objective data the script has already filled in above. Do not "
        "add new flags.\n"
        "- Remove the SHORTLIST block from your final output.\n\n"
        f"Template:\n{template}"
    )


def _call_with_retry(msgs: list[dict]) -> anthropic.types.Message:
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.messages.create(
                model=MODEL, max_tokens=MAX_OUTPUT_TOKENS, messages=msgs
            )
        except (RateLimitError, APIConnectionError) as exc:
            transient = True
            reason = type(exc).__name__
        except APIStatusError as exc:
            transient = exc.status_code in (429, 500, 502, 503, 504, 529)
            reason = f"HTTP {exc.status_code}"
            if not transient:
                raise

        if attempt == MAX_RETRIES:
            raise RuntimeError(
                f"API still failing after {MAX_RETRIES} attempts ({reason}). "
                "Try again in a few minutes."
            )

        print(f"  [retry] {reason}, sleeping {backoff}s "
              f"(attempt {attempt}/{MAX_RETRIES})")
        time.sleep(backoff)
        backoff *= 2


def measure(label: str, prompt: str, output_path: Path) -> dict:
    msgs = [{"role": "user", "content": prompt}]

    input_tokens = client.messages.count_tokens(
        model=MODEL, messages=msgs
    ).input_tokens

    t0 = time.time()
    resp = _call_with_retry(msgs)
    elapsed = time.time() - t0

    output_tokens = resp.usage.output_tokens
    stop_reason = resp.stop_reason

    full_text = "".join(
        block.text for block in resp.content if hasattr(block, "text")
    )
    output_path.write_text(full_text, encoding="utf-8")

    truncated = stop_reason == "max_tokens"
    if truncated:
        print(f"  [warn] {label}: output truncated at max_tokens="
              f"{MAX_OUTPUT_TOKENS}.")

    return {
        "label": label,
        "input": input_tokens,
        "output": output_tokens,
        "total": input_tokens + output_tokens,
        "seconds": elapsed,
        "stop_reason": stop_reason,
        "truncated": truncated,
        "output_path": output_path,
    }


def print_table(baseline: dict, hybrid: dict) -> None:
    def saved(a: float, b: float) -> str:
        return f"{(1 - b / a) * 100:5.1f}%" if a else "  n/a"

    print()
    print("=" * 64)
    print(f"{'Metric':<22}{'Baseline':>14}{'Hybrid':>14}{'Saved':>14}")
    print("-" * 64)
    print(f"{'Input tokens':<22}{baseline['input']:>14}{hybrid['input']:>14}"
          f"{saved(baseline['input'], hybrid['input']):>14}")
    print(f"{'Output tokens':<22}{baseline['output']:>14}{hybrid['output']:>14}"
          f"{saved(baseline['output'], hybrid['output']):>14}")
    print(f"{'Total tokens':<22}{baseline['total']:>14}{hybrid['total']:>14}"
          f"{saved(baseline['total'], hybrid['total']):>14}")
    print(f"{'Latency (seconds)':<22}{baseline['seconds']:>14.2f}{hybrid['seconds']:>14.2f}"
          f"{saved(baseline['seconds'], hybrid['seconds']):>14}")
    print("=" * 64)

    if baseline["truncated"] or hybrid["truncated"]:
        print()
        print("WARNING: output was truncated (hit max_tokens). "
              "Numbers above are NOT a fair comparison.")
        print(f"  Baseline stop_reason: {baseline['stop_reason']}")
        print(f"  Hybrid   stop_reason: {hybrid['stop_reason']}")
        print(f"  Raise MAX_OUTPUT_TOKENS in benchmark_tokens.py and rerun.")
    print()


def main() -> None:
    baseline_path = OUTPUTS / "result_baseline.md"
    hybrid_path = OUTPUTS / "result_hybrid.md"

    baseline = measure("Baseline", build_baseline_prompt(), baseline_path)
    hybrid = measure("Hybrid", build_hybrid_prompt(), hybrid_path)
    print_table(baseline, hybrid)

    print("Full model outputs saved for manual review:")
    print(f"  Baseline: {baseline_path}")
    print(f"  Hybrid:   {hybrid_path}")
    print()


if __name__ == "__main__":
    main()
