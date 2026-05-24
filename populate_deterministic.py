"""
populate_deterministic.py

Builds a half-populated interview prep template from the CV, hiring brief,
and a curated question bank. The script fills the slots that need NO LLM
reasoning, plus builds a shortlist of candidate questions from the bank.

Filled by the script (no LLM):
    - candidate name + date
    - seniority_label
    - must_have_coverage  (CV ↔ hiring-brief keyword matching)
    - years_snapshot      (regex extraction from CV)
    - flags_extracted     (objective signals: low-years tech, missing must-
                           haves, "basic"/"familiarity" admissions, etc.)
    - exercises           (filter of an exercise bank by declared stack)

Left for the LLM (with the question shortlist appended as context):
    - flag_interpretation
    - warmup_questions      → pick from shortlist
    - technical_questions   → pick from shortlist
    - trap_questions        → pick from shortlist
    - interview_focus

The shortlist is appended at the end of the file as a YAML block. The LLM
prompt instructs the model to choose from it and to never invent
questions.
"""

import re
import yaml
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "inputs"
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Small exercise bank, kept inline for the demo.
# ---------------------------------------------------------------------------
EXERCISE_BANK = [
    {"id": "ex-aws-1", "tags": ["aws", "ecs"],
     "title": "Design a zero-downtime ECS deployment for a 5-service app"},
    {"id": "ex-cicd-1", "tags": ["jenkins", "gitlab-ci"],
     "title": "CI/CD migration: how would you move 20 Jenkins jobs to GitLab CI?"},
    {"id": "ex-terraform-1", "tags": ["terraform", "iac"],
     "title": "Refactor a 2000-line Terraform monolith into modules — what's your plan?"},
    {"id": "ex-incident-1", "tags": ["on-call", "incident"],
     "title": "P1: database CPU at 100%, all services degraded. First 15 minutes?"},
    {"id": "ex-k8s-1", "tags": ["kubernetes"],
     "title": "Design a multi-tenant Kubernetes platform for 30 internal teams."},
]


# ---------------------------------------------------------------------------
# CV parsing — small, deliberate, no LLM
# ---------------------------------------------------------------------------
def extract_years_by_tech(cv_text: str) -> dict[str, int]:
    pattern = re.compile(r"([A-Za-z][A-Za-z0-9 +/.-]*?)\s*\((\d+)\s*y\)")
    found = {}
    for name, years in pattern.findall(cv_text):
        key = name.strip()
        found[key] = max(found.get(key, 0), int(years))
    return found


def total_declared_years(cv_text: str) -> int:
    pattern = re.compile(r"(\d+)\s*years?\b", re.IGNORECASE)
    role_lines = [
        line for line in cv_text.splitlines()
        if line.startswith("###") and re.search(r"\d{4}", line)
    ]
    return sum(int(n) for line in role_lines for n in pattern.findall(line))


# ---------------------------------------------------------------------------
# Structured role parsing
# Extracts (title, company, start_year, end_year, years, bullets) per role.
# Used to build richer evidence in seniority / coverage / years snapshot.
# ---------------------------------------------------------------------------
ROLE_HEADER_RE = re.compile(
    r"^###\s+"
    r"(?P<title>[^—\-–]+?)\s*[—\-–]\s*"          # title before dash
    r"(?P<company>.+?)\s*"                        # company
    r"\((?P<start>\d{4})\s*[—\-–]\s*"            # start year
    r"(?P<end>\d{4}|present)"                     # end year or 'present'
    r"(?:,\s*(?P<years>\d+)\s*years?)?\s*\)",    # optional declared years
    re.IGNORECASE,
)


def extract_roles(cv_text: str) -> list[dict]:
    """Return a list of role dicts in the order they appear in the CV."""
    lines = cv_text.splitlines()
    roles: list[dict] = []
    current: dict | None = None
    for line in lines:
        # New section heading (level 1-2) closes any in-progress role.
        if re.match(r"^#{1,2}\s+\S", line):
            if current is not None:
                roles.append(current)
                current = None
            continue
        m = ROLE_HEADER_RE.match(line)
        if m:
            if current is not None:
                roles.append(current)
            end_raw = m.group("end")
            end_year = (date.today().year if end_raw.lower() == "present"
                        else int(end_raw))
            start_year = int(m.group("start"))
            years_raw = m.group("years")
            years = int(years_raw) if years_raw else (end_year - start_year)
            current = {
                "title": m.group("title").strip(),
                "company": m.group("company").strip(),
                "start": start_year,
                "end": end_raw,
                "end_year": end_year,
                "years": years,
                "bullets": [],
            }
            continue
        if current is not None and line.strip().startswith("- "):
            current["bullets"].append(line.strip()[2:].strip())
    if current is not None:
        roles.append(current)
    return roles


def latest_role(roles: list[dict]) -> dict | None:
    if not roles:
        return None
    # Roles ending with 'present' first, otherwise the most recent end year
    present_roles = [r for r in roles if str(r["end"]).lower() == "present"]
    if present_roles:
        return present_roles[0]
    return max(roles, key=lambda r: r["end_year"])


def detect_stack(cv_text: str) -> set[str]:
    text = cv_text.lower()
    candidates = {
        "aws", "ecs", "eks", "rds", "iam", "vpc", "cloudwatch",
        "jenkins", "gitlab ci", "github actions",
        "terraform",
        "docker", "kubernetes",
        "prometheus", "grafana",
        "go", "python", "bash",
        "kafka", "spark",
    }
    return {t for t in candidates if t in text}


# ---------------------------------------------------------------------------
# Hiring-brief parsing
# ---------------------------------------------------------------------------
def extract_must_haves(brief_text: str) -> list[str]:
    inside = False
    items = []
    for line in brief_text.splitlines():
        if line.strip().startswith("## Must-haves"):
            inside = True
            continue
        if inside:
            if line.startswith("##"):
                break
            m = re.match(r"\s*-\s+(.*)", line)
            if m:
                items.append(m.group(1).strip())
    return items


# ---------------------------------------------------------------------------
# Coverage matrix
# ---------------------------------------------------------------------------
# Distinctive keyword extractor: filters short / generic tokens that produce
# weak evidence ("years", "experience", "in"). Used by both the matcher and
# the evidence builder.
_GENERIC_TOKENS = {
    "and", "the", "for", "any", "of", "or", "with", "in", "on", "at",
    "years", "year", "experience", "experiences", "role", "roles",
    "team", "teams", "track", "record", "hands", "comfortable", "working",
    "leading", "led", "able", "ability", "strong", "written", "spoken",
}


def _distinctive_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9+/.-]+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in _GENERIC_TOKENS]


def _evidence_from_roles(matched_tokens: list[str],
                         roles: list[dict],
                         years_map: dict[str, int]) -> str:
    """
    Build human-readable evidence anchored to roles and declared years.
    Falls back to the matched tokens themselves if no anchor is found.
    Crucially: only anchor to a role when its BULLETS actually contain
    one of the matched tokens (the title alone is too weak a signal).
    """
    if not matched_tokens:
        return "No clear evidence in CV"

    pieces: list[str] = []

    # 1. Anchor to declared years per technology if any matched token is
    #    a known tech with a year count.
    years_lower = {k.lower(): (k, v) for k, v in years_map.items()}
    for tok in matched_tokens:
        if tok in years_lower:
            name, yrs = years_lower[tok]
            pieces.append(f"{name} {yrs}y declared")
            if len(pieces) >= 2:
                break

    # 2. Anchor to a role whose BULLETS contain one of the matched tokens.
    #    Title-only matches are skipped — they produce misleading anchors.
    if len(pieces) < 2:
        for role in roles:
            bullets_blob = " ".join(role["bullets"]).lower()
            hits = [t for t in matched_tokens if t in bullets_blob]
            if hits:
                anchor = (
                    f"{role['title']} at {role['company']} "
                    f"({role['start']}–{role['end']})"
                )
                pieces.append(anchor)
                break

    # 3. Fallback: surface the actually-matched tokens, not generic ones.
    if not pieces:
        return "Matched terms: " + ", ".join(matched_tokens[:4])

    surfaced_terms = ", ".join(matched_tokens[:3])
    return f"{'; '.join(pieces)} ({surfaced_terms})"


def status_for_requirement(requirement: str,
                           cv_text: str,
                           roles: list[dict],
                           years_map: dict[str, int]) -> tuple[str, str]:
    cv_lower = cv_text.lower()
    keywords = _distinctive_tokens(requirement)
    hits = [k for k in keywords if k in cv_lower]
    ratio = len(hits) / max(len(keywords), 1) if keywords else 0
    evidence = _evidence_from_roles(hits, roles, years_map)
    if ratio >= 0.6:
        return "✅", evidence
    if ratio >= 0.3:
        return "⚠️", evidence
    return "❌", "No clear evidence in CV"


def build_coverage_table(must_haves: list[str],
                         cv_text: str,
                         roles: list[dict],
                         years_map: dict[str, int]) -> str:
    lines = ["| Requirement | Status | Evidence |", "|---|---|---|"]
    for req in must_haves:
        icon, evidence = status_for_requirement(req, cv_text, roles, years_map)
        req_safe = req.replace("|", "\\|")
        lines.append(f"| {req_safe} | {icon} | {evidence} |")
    return "\n".join(lines)


def build_years_snapshot(years_map: dict[str, int],
                         roles: list[dict]) -> str:
    """
    Years snapshot enriched with the role that most likely covers each tech.
    Only anchors when a role's BULLETS contain the tech name — anchoring on
    title alone is misleading.
    """
    if not years_map:
        return "_No structured years-of-experience data found in CV._"

    def role_for_tech(tech: str) -> str | None:
        tech_lc = tech.lower()
        for role in roles:
            bullets_blob = " ".join(role["bullets"]).lower()
            if tech_lc in bullets_blob:
                return f"{role['company']}, {role['start']}–{role['end']}"
        return None

    rows = sorted(years_map.items(), key=lambda kv: -kv[1])
    out = []
    for tech, yrs in rows:
        unit = "year" if yrs == 1 else "years"
        anchor = role_for_tech(tech)
        if anchor:
            out.append(f"- {tech}: {yrs} {unit} ({anchor})")
        else:
            out.append(f"- {tech}: {yrs} {unit}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Flag extraction — richer rules, still deterministic
# ---------------------------------------------------------------------------
DATE_RANGE_RE = re.compile(r"\((\d{4})\s*[—\-–]\s*(\d{4}|present)", re.IGNORECASE)


def extract_flags(cv_text: str,
                  years_map: dict[str, int],
                  must_haves: list[str]) -> list[str]:
    flags: list[str] = []
    cv_lower = cv_text.lower()

    # Career gaps
    ranges = []
    for start, end in DATE_RANGE_RE.findall(cv_text):
        end_year = date.today().year if end.lower() == "present" else int(end)
        ranges.append((int(start), end_year))
    ranges.sort()
    for i in range(1, len(ranges)):
        gap_years = ranges[i][0] - ranges[i - 1][1]
        if gap_years >= 1:
            flags.append(
                f"🚩 Career gap of ~{gap_years} year(s) between "
                f"{ranges[i - 1][1]} and {ranges[i][0]}"
            )

    # Must-haves with no clear evidence
    for req in must_haves:
        keywords = re.findall(r"[a-zA-Z][a-zA-Z0-9+/.-]+", req.lower())
        keywords = [k for k in keywords if len(k) > 2 and k not in {
            "and", "the", "for", "any", "with"
        }]
        if keywords and not any(k in cv_lower for k in keywords):
            flags.append(f"🚩 Must-have not evidenced in CV: {req}")

    # Low-experience tech
    for tech, yrs in years_map.items():
        if yrs <= 1:
            flags.append(f"🚩 Low experience declared: {tech} ({yrs} year)")

    # Self-flagged "basic" or "familiarity"
    for label in ["basic", "familiarity"]:
        for line in cv_text.splitlines():
            ll = line.lower()
            if label in ll:
                m = re.search(rf"([\w/.+ -]+?)\s*\([^)]*{label}[^)]*\)", ll)
                if m:
                    flags.append(
                        f"🚩 Skill self-flagged as '{label}': {m.group(1).strip()}"
                    )
                elif label in ll and ("declared" not in ll):
                    flags.append(
                        f"🚩 '{label}' admission in CV (line: \"{line.strip()[:80]}...\")"
                    )

    # Certification status
    cert_status_re = re.compile(
        r"(renewal in progress|in progress|expired|lapsed)", re.IGNORECASE
    )
    for line in cv_text.splitlines():
        if "cert" in line.lower() and cert_status_re.search(line):
            flags.append(f"🚩 Certification status flagged: \"{line.strip()[:100]}\"")

    # SRE claimed but no observability tooling
    if any(x in cv_lower for x in ["sre", "incident", "on-call"]):
        obs_tools = ["prometheus", "grafana", "datadog", "cloudwatch", "new relic"]
        if not any(t in cv_lower for t in obs_tools):
            flags.append(
                "🚩 SRE / incident leadership claimed but no observability "
                "tooling declared (Prometheus / Grafana / Datadog / CloudWatch)"
            )

    return flags


def build_flags_section(flags: list[str]) -> str:
    return "\n".join(flags) if flags else "_No objective flags detected._"


# ---------------------------------------------------------------------------
# Exercise bank filter
# ---------------------------------------------------------------------------
def build_exercise_selection(stack: set[str]) -> str:
    selected = []
    for ex in EXERCISE_BANK:
        if any(tag in stack or tag.replace("-", " ") in stack for tag in ex["tags"]):
            selected.append(ex)
        if len(selected) >= 2:
            break
    if not selected:
        return "_No matching exercises in the bank for this stack._"
    return "\n".join(f"- **[{ex['id']}]** {ex['title']}" for ex in selected)


def seniority_label(total_years: int, roles: list[dict]) -> str:
    """Contextual seniority signal using the latest role + total tenure."""
    base = (
        "Senior" if total_years >= 5
        else "Mid-level" if total_years >= 3
        else "Junior"
    )
    latest = latest_role(roles)
    if latest is None:
        return f"{base} ({total_years} years total declared experience)"

    # Detect management role in the latest title
    title_lower = latest["title"].lower()
    is_manager = any(
        kw in title_lower
        for kw in ["manager", "head of", "director", "lead", "principal"]
    )
    role_descriptor = (
        f"explicit {latest['title']} title since {latest['start']}"
        if is_manager
        else f"{latest['title']} at {latest['company']}"
    )

    return (
        f"{base} — {latest['years']} year(s) in current role "
        f"({role_descriptor}); {total_years} years total declared "
        f"across all roles."
    )


# ---------------------------------------------------------------------------
# Question bank shortlist
# ---------------------------------------------------------------------------
def load_question_bank() -> list[dict]:
    path = INPUTS / "question_bank.yaml"
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["questions"]


def build_question_shortlist(bank: list[dict],
                             stack: set[str],
                             flags: list[str]) -> list[dict]:
    """
    Score each question by relevance and return the top ~12.
    Relevance signals:
      - tag overlap with the candidate's declared stack
      - tags matching themes mentioned in the extracted flags
      - generic behaviour questions always included (career, ownership, people)
    """
    flag_text = " ".join(flags).lower()

    flag_themes = []
    if "github actions" in flag_text:
        flag_themes.append("github-actions")
    if "remote" in flag_text or "async" in flag_text:
        flag_themes.append("remote")
    if "basic" in flag_text and "kubernetes" in flag_text:
        flag_themes.append("k8s")
    if "observability" in flag_text or "sre" in flag_text:
        flag_themes.append("observability")
    if "iam" in flag_text or "networking" in flag_text or "vpc" in flag_text:
        flag_themes.append("iam")

    generic_behaviour_tags = {"career-arc", "ownership", "team-size",
                              "underperformance", "remote"}

    scored = []
    for q in bank:
        score = 0
        q_tags = set(q.get("tags", []))

        # Stack overlap
        for tag in q_tags:
            normalised = tag.replace("-", " ")
            if tag in stack or normalised in stack:
                score += 3

        # Flag-driven themes
        for theme in flag_themes:
            if theme in " ".join(q_tags):
                score += 4

        # Always-relevant behaviour questions
        if q_tags & generic_behaviour_tags:
            score += 2

        # Trap questions get a small boost so they appear in the shortlist
        if "trap" in q_tags:
            score += 1

        scored.append((score, q))

    scored.sort(key=lambda x: -x[0])
    return [q for _, q in scored[:12]]


def render_shortlist_block(shortlist: list[dict]) -> str:
    return (
        "## SHORTLIST (do not place this block in the final output)\n\n"
        "The following questions have been pre-filtered from the question bank "
        "as candidates for this CV. The LLM must choose from this list. "
        "Do not invent new questions and do not modify the wording or "
        "expected responses.\n\n"
        "```yaml\n"
        + yaml.safe_dump({"shortlist": shortlist}, sort_keys=False,
                         allow_unicode=True, width=88)
        + "```\n"
    )


# ---------------------------------------------------------------------------
# Marker substitution
# ---------------------------------------------------------------------------
MARKER_RE = re.compile(
    r"<!--\s*AGENT-FILL:\s*(?P<key>[a-z_]+)(?:\s*\|\s*[^>]*?)?-->",
    re.DOTALL,
)


def fill_markers(template: str, fills: dict[str, str]) -> str:
    def repl(match):
        key = match.group("key")
        return fills.get(key, match.group(0))
    return MARKER_RE.sub(repl, template)


def fill_simple_placeholders(text: str, candidate_name: str) -> str:
    text = text.replace("[CANDIDATE_NAME]", candidate_name)
    text = text.replace("[DATE]", date.today().isoformat())
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(candidate_name: str = "Luca Bianchi"):
    cv = (INPUTS / "cv.md").read_text(encoding="utf-8")
    brief = (INPUTS / "hiring_brief.md").read_text(encoding="utf-8")
    template = (INPUTS / "template.md").read_text(encoding="utf-8")

    years_map = extract_years_by_tech(cv)
    stack = detect_stack(cv)
    total_yrs = total_declared_years(cv)
    roles = extract_roles(cv)
    must_haves = extract_must_haves(brief)
    flags = extract_flags(cv, years_map, must_haves)

    fills = {
        "seniority_label": seniority_label(total_yrs, roles),
        "must_have_coverage": build_coverage_table(must_haves, cv, roles, years_map),
        "years_snapshot": build_years_snapshot(years_map, roles),
        "flags_extracted": build_flags_section(flags),
        "exercises": build_exercise_selection(stack),
    }

    populated = fill_simple_placeholders(template, candidate_name)
    populated = fill_markers(populated, fills)

    # Build and append the shortlist of candidate questions.
    bank = load_question_bank()
    shortlist = build_question_shortlist(bank, stack, flags)
    populated = (
        populated.rstrip()
        + "\n\n---\n\n"
        + render_shortlist_block(shortlist)
    )

    out_path = OUTPUTS / "template_half_populated.md"
    out_path.write_text(populated, encoding="utf-8")

    remaining = len(MARKER_RE.findall(populated))
    print(f"Wrote {out_path}")
    print(f"Filled {len(fills)} deterministic slots.")
    print(f"Shortlisted {len(shortlist)} questions from the bank "
          f"(of {len(bank)} total).")
    print(f"Remaining AGENT-FILL markers for the LLM: {remaining}")


if __name__ == "__main__":
    main()
