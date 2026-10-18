# Labelling guidelines

These rules define the "right answer" for every field. The Phase 2 extraction
prompt will be written from this file, and the LLM is scored against my labels,
so **when a rule changes, re-check the files already labelled**.

**Core principle: label the posting, not my fit.** Labels say what the posting
requires; whether *I* qualify (my German, my degree, my citizenship) is computed
later by comparing these requirements with my profile. Never fill a field from
my own situation — e.g. a degree that isn't mentioned is `unknown`, not my degree.

Workflow: open a file in `eval/labels/`, read the description, fill the
`labels:` block, run `uv run python -m job_search_assistant.evalset check`.
Label from the posting text only, never from what I know about the company.
If I'm unsure, pick the closest value and say why in `notes`.

## Fields

**is_target_role** — `true` if the job is one of the roles in CLAUDE.md
"Search scope" (data analyst, data engineer, data scientist, AI engineer,
ML engineer, analytics engineer, BI analyst), judged by the *work described*,
not the title alone. "AI Frontend Engineer" building UIs → `false`.
Seniority, location and eligibility do not matter here: an ML internship is
`is_target_role: true` with `seniority: intern`. "Is this job for me?" is a
query combining fields, not a label.

**role_family** — the closest one of the seven roles; `other` if
`is_target_role` is false. Tie-breakers: pipelines/warehouses → data_engineer;
dashboards/reporting/SQL analysis → data_analyst (bi_analyst if centred on a BI
tool like Power BI/Tableau); dbt/data modelling for analytics →
analytics_engineer; statistics/experiments/predictive models → data_scientist;
training/deploying models, MLOps → ml_engineer; building products on LLMs/APIs,
agents, RAG → ai_engineer.

**seniority** — from title first, then requirements:
- `intern` – Praktikum, internship, Pflichtpraktikum, thesis
- `working_student` – Werkstudent, working student, student job
- `junior` – junior/entry/graduate/trainee, or ≤ 2 years required
- `mid` – 2–5 years, no level word
- `senior` – senior, or ≥ 5 years
- `lead` – lead, principal, staff, head, manager of people
- `unknown` – nothing to go on

No years stated? Use the experience wording: "first/some experience",
"erste/grundlegende Erfahrung", "Grundkenntnisse" → `junior`; "solid/proven/
several years of experience", "fundierte/mehrjährige/langjährige Erfahrung" →
`mid` (unless a level word says otherwise). Only when neither years nor such
wording exist → `unknown`.

**employment_type** — `full_time`, `part_time`, `contract` (fixed-term via an
agency or project contract), `freelance`, `unknown`. A fixed-term (befristet)
employee job is still `full_time`/`part_time`.
Only what the posting states ("Vollzeit", "full-time", "20 h/week"), with one
exception: working student / Werkstudent → `part_time` (part-time by
definition in Germany). Never infer it from seniority: a junior or intern
posting that says nothing → `unknown`. Both offered ("Voll- oder Teilzeit")
→ `full_time`.

**german_level / english_level** — the *requirement*, using this scale:
| value | meaning | typical wording |
|---|---|---|
| `none` | not mentioned | — |
| `plus` | nice to have | "German is a plus", "Deutschkenntnisse von Vorteil" |
| `basic` | required, A1–B1 | "basic German", "Grundkenntnisse", "at least B1" |
| `business` | required, B2 | "business fluent", "gute Deutschkenntnisse" |
| `fluent` | required, C1 | "fluent", "fließend", "verhandlungssicher", "sehr gute/excellent German", "C1" |
| `native` | required, C2 | "Muttersprache", "native speaker", "C2" |

When a CEFR level is stated, it wins over the wording. "At least B1" → `basic`
(required), never `plus`.

**german_implied** — `true` when the level is inferred rather than stated.
Rule: a posting written entirely in German with no language statement →
`german_level: fluent`, `german_implied: true`. An English posting with no
statement → `none`, `false`. Same idea applies to English (no separate flag;
mention it in `notes`).

**other_languages** — required or nice-to-have languages other than German
and English, as `"Language: level"` with the same scale (`plus` … `native`),
e.g. `["Dutch: fluent"]`, `["French: plus"]`. Empty if none.

**workplace** — `remote` (fully, maybe occasional office days), `hybrid`
(regular office + home office), `onsite`, `unknown`. "Home office possible"
without detail → `hybrid`. "Minimum 3 days in the office" → `hybrid`. Split
between client sites, office and home → `hybrid`.

**years_experience_min** — the smallest number stated ("3+ years" → 3,
"2–4 years" → 2, "first experience" → leave empty). Empty if not stated.

**degree** — the minimum *required* degree. "Bachelor or Master" → bachelor.
"Degree in CS or comparable experience" → bachelor. Ausbildung → vocational.
For interns/working students, enrolment in a degree programme → `bachelor`.
Not mentioned → `unknown`. `none` only if the posting explicitly says no degree
is needed.

**required_skills / nice_to_have_skills** — tools, languages, methods, as
written in the posting (`Power BI`, `PySpark`, `A/B testing`). Not soft skills,
not human languages, not degrees. Don't normalize names (that's Phase 4);
do split lists ("Python/R" → `[Python, R]`, "ETL/ELT" → `[ETL, ELT]`;
established names like `CI/CD` and `A/B testing` stay whole). In a German
posting, keep the posting's own word — `Datenmodellierung`, not "Data
Modeling"; English terms it already uses (`Power BI`, `RAG`) stay English.
Translating is normalization, i.e. Phase 4. Ambiguous wording ("ideally",
"wünschenswert", "a plus") → nice to have.

**restrictions** — who may apply, as short phrases close to the posting's
wording: `["enrolled student", "EU citizen or studying in NL", "no visa
sponsorship", "valid German work permit"]`. Not skills or languages (those
have their own fields). Empty if none stated. Only *eligibility* — conditions
that rule a person out: student status, citizenship, work permit, visa,
time-zone overlap. Not availability or willingness (travel, weekly hours,
start date, duration): those are part of the job, not who may apply. One
phrase per condition; details of the same condition stay in it
(`"enrolled student, 3rd year or beyond"`).

**salary** — copy as written (`"€55,000–65,000"`), else leave empty.

**notes** — anything surprising, and the reason for any judgement call.

## Proposed rules (from drafting files 05–30 — accept, change or reject)

These came up repeatedly while drafting. The drafts already follow them; if
you reject one, the files listed need re-checking.

1. **Tools named only in the task list or a "our stack" section** → `nice_to_have_skills`, not required (06, 14, 19, 25, 28).
2. **"X, Y or similar" / "e.g. X, Y"** → the category (if named) is required, the named examples are nice to have (13, 15, 19). Exception: examples listed *inside* a hard requirement bullet stay required (11, 18).
3. **Years that depend on the degree** ("8+ with Bachelor's, 6+ with Master's") → use the years for the minimum degree you labelled (18).
4. **Title vs body conflict** → the body wins; explain in `notes` (14 "Freelance" vs "CDI").
5. **Time-zone or citizenship conditions** count as `restrictions` (16 NATO citizenship, 17 time-zone overlap).
6. **Wording without CEFR**: "gute …kenntnisse" / "versiert" / "proficient" → `business`; "sehr gut" / "excellent" / "verhandlungssicher" / "fließend" → `fluent` (20, 22, 23, 25, 26).
7. **"Studium oder vergleichbare Ausbildung"** → `vocational` (the minimum that's accepted) (24).
8. **Recruiter / agency postings** (Hays, Orizon, recruitIT): label the client job; agency leasing (Personalüberlassung) → `contract` (25).
