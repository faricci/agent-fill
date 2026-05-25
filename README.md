# AGENT-FILL: a minimal example

A small, runnable example of the **AGENT-FILL** pattern: split work between
a deterministic script and an LLM on the same markdown document. The LLM
picks from a curated bank instead of generating, cutting tokens and
hallucinations.

Companion repository to the Medium article _AGENT-FILL: A Markdown Comment
That Cuts LLM Costs and Hallucinations_: https://medium.com/@faricci_62865/agent-fill-a-markdown-comment-that-cuts-llm-costs-and-hallucinations-580e84d370e5 

---

## What the pattern does

The example generates an interview prep document for an engineering
candidate, and compares two approaches:

1. **Baseline.** The LLM receives the CV, the hiring brief, the full
   template, and the curated question bank. It picks questions from the
   bank for the question slots and fills the rest of the document itself.

2. **Hybrid.** A Python script first fills the deterministic slots
   (coverage matrix, years per technology, objective flags) and
   pre-filters the question bank down to a shortlist relevant to this
   candidate. The LLM then receives the half-populated document plus
   the shortlist and fills only the remaining slots.

In both cases, **the LLM never generates a question from scratch.** It
picks from a curated, reviewed bank. The script pre-computes facts and
narrows the candidate pool. The LLM does semantic matching: which
question from the pool best fits this candidate, this slot, this
context. Nothing more.

The benchmark measures the token, latency, and quality difference
between the two.

---

## Cost

The benchmark makes **two real API calls** per run (baseline + hybrid).
With Claude Sonnet 4.6 ($3 input / $15 output per million tokens), each
run costs approximately **$0.20**.

The `populate` step is free (no API calls). `ping_api.py` is a fraction
of a cent.

## Quick start (Docker)

The recommended way to run the example. No local Python needed.

```bash
# 1. Set your API key
cp .env.example .env
# then edit .env and put your key in

# 2. Build the image (once)
docker compose build

# 3. (Optional) Sanity check the API key
docker compose run --rm populate python ping_api.py

# 4. Run the deterministic populator
docker compose run --rm populate
#   → writes outputs/template_half_populated.md

# 5. Run the benchmark (two LLM calls: baseline + hybrid)
docker compose run --rm benchmark
```

The benchmark prints a comparison table and saves both model outputs:

- `outputs/result_baseline.md` — what the LLM produced from the full prompt
- `outputs/result_hybrid.md` — what the LLM produced from the half-populated template

Open them side by side to compare quality, not just cost.

If you see `WARNING: output was truncated`, raise `MAX_OUTPUT_TOKENS`
in `benchmark_tokens.py` and rerun. Depending on the token number you may want to change model as well.

## Quick start (local Python)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python populate_deterministic.py
python benchmark_tokens.py
```

## Repository layout

```
agent-fill-example/
├── inputs/
│   ├── cv.md                 # Anonymised candidate CV
│   ├── hiring_brief.md       # Role description with must-haves
│   ├── template.md           # Interview prep template with AGENT-FILL markers
│   └── question_bank.yaml    # Curated, reviewed question bank
├── outputs/                  # Generated files appear here
├── populate_deterministic.py # Fills script slots + builds the shortlist
├── benchmark_tokens.py       # Compares baseline vs hybrid
├── ping_api.py               # API key + connectivity check
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

## What the populator does

`populate_deterministic.py` reads the CV, the hiring brief, and the
question bank, then fills these AGENT-FILL slots **without** involving
an LLM:

| Slot | How it's filled |
|---|---|
| `seniority_label` | Latest role + total declared years |
| `must_have_coverage` | Keyword match between CV and hiring brief, anchored to roles when possible |
| `years_snapshot` | Regex extraction from the CV, enriched with the role each tech belongs to |
| `flags_extracted` | Career gaps, missing must-haves, low-years tech, `basic`/`familiarity` admissions, certification status, SRE claim vs missing observability tooling |
| `exercises` | Filter of an exercise bank by declared stack |

It also expands `[CANDIDATE_NAME]` and `[DATE]`, and appends a
**shortlist** of ~12 questions filtered from the question bank, scored
by stack overlap and flag-driven themes.

These slots are left for the LLM:

- `flag_interpretation` — judgement on each 🚩
- `warmup_questions` — pick 2-3 from the shortlist
- `technical_questions` — pick 3-4 from the shortlist
- `trap_questions` — pick 1-2 from the shortlist
- `interview_focus` — top 3 areas to weight

## Where AGENT-FILL fits in the context stack

AGENT-FILL is the most granular layer of context available to an LLM
agent today:

- **Model fine-tuning** sets behaviour globally.
- **System prompts** set it per session.
- **Instruction files** (`copilot-instructions.md`, `CLAUDE.md`, skills)
  set it per project.
- **AGENT-FILL** sets it per slot in a single document.

It travels inside the deliverable rather than in a configuration file,
which makes it portable across agents that can read markdown.

## Adapting it to your workflow

The pattern is language- and domain-agnostic. The reference
implementation is in Python; the same logic works in PowerShell, Node,
Go, or anything that can do regex substitution. The only contract is
the marker syntax:

```text
<!-- AGENT-FILL: section_key | instruction for the agent -->
```

When you adapt this to a new document type:

1. **Build the bank.** Write or curate the candidate content (questions,
   exercises, snippets, whatever). The LLM never invents — it only
   picks from this.
2. **Split slots.** Identify which slots are deterministic (lookups,
   counts, matches) → script. Identify which slots need semantic
   matching (which item from the bank fits here) → LLM.
3. **Keep the marker syntax consistent** so a single regex finds them all.

## License

MIT
