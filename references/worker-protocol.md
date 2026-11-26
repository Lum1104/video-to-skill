# Worker Protocol & Generated Skill Contract

Detailed role boundaries, evidence rules, and output quality requirements for the `Analyze → Author → Review` pipeline.

## Worker Roles

### Analyze
Combine high-recall extraction, terminology normalization, relation linking, materiality review, disposition accounting, conflict capture, and capability-ceiling analysis in one bounded role.

- Preserve claims, reasoning, examples, analogies, definitions, distinctions, qualifications, counterpoints, predictions, recommendations, warnings, value judgments, and open questions, along with how they relate to each other.
- Use speech-first packets for interviews and talking heads. Inspect slides, code, UI, diagrams, or physical state only when visual evidence is material. For long courses, process section groups first, then run the dependent integration Analyze task without deleting source-specific units.
- The engine supplies the visual evidence index (first frame, scene changes, periodic fallbacks, deduplication, OCR/type hints, and bounded dense windows). Analyze proposes at most 24 non-duplicate teaching visual candidates (single frame, normalized crop, or 2–4 frame sequence) only when a visual materially improves teaching or verification. Never edit pixels or supply arbitrary image paths.
- Every semantic unit requires a stable ID, source/time range, kind, compact summary, materiality, disposition, modality, evidence IDs, observed-versus-inferred status, confidence, and uncertainty. Merged, context-only, and omitted items require an explicit reason.

### Author
Author has two bounded task shapes:

1. **Curriculum planning**: read canonical semantic records, recommend a thematic path, and propose up to two justified alternates. Write only curriculum options, ordered semantic-unit sequences, and concise decision metadata—never artifact plans, claims, assets, or Markdown drafts. Echo the fixed artifact language (or make the one constrained declaration for mixed/unknown source evidence). When alternatives materially change the learning experience, pause at the `ask-user` action; otherwise the coordinator selects the recommendation.
2. **Full Skill authoring**: preserve the selected curriculum path, semantic order, and canonical artifact language while binding them to justified artifacts and writing Markdown drafts inside the task output directory. Do not reopen the curriculum choice during authoring or repair.

Authoring rules:
- Treat Learn, Practice, Apply, and Reference as evidence-bounded capability levels, not artifact quotas. Never exceed the Analyze capability ceiling.
- Complete the instructional-affordance ledger independently from semantic coverage (learning objectives, misconceptions, retrieval/transfer prompts, focused exercises, success criteria, scored rubrics, progressive hints, retry, capstone synthesis, operational playbooks, expected states, validation, recovery, quick reference, decision rules). Mark each `provided`, `unsupported`, or `not-applicable` with a rationale.
- Give every artifact a stable ID, user job, supported behaviors, `normal` or `after-attempt` disclosure, independent loading reason, semantic-unit links, affordance links, destination path, draft path, and digest. Keep solutions and answer-bearing rubrics separate and `after-attempt`.
- Select visuals only from verified candidates in the Author packet. Link each retained PNG from its `used_by` artifacts and bind it to claims preserving the same visual/temporal evidence. Leave decorative, redundant, illegible, private, or text-recoverable candidates unused.

### Review
Use a fresh critic independent of both curriculum-planning and artifact-Author producers. Review actual canonical drafts and records, not an author-supplied summary.

- Audit source-meaning retention and instructional-affordance retention separately, then grounding, uncertainty, disclosure, empty invocation, runtime behavior, safety, scope, source failures, and shareability. Inspect every selected visual for necessity, legibility, context, ordering, privacy, grounding, and on-demand loading.
- Before the final critic, the engine renders canonical Author state into a private immutable behavior target and generates a versioned scenario catalog (empty invocation, start/intake, bounded teaching, practice solution withholding, application context gathering, grounded reference, out-of-scope honesty, plus deterministic semantic pressure cases).
- Dispatch each applicable `behavior-trial` Review task in a genuinely fresh context. A trial receives only the immutable target and one prompt, echoes its lease `execution_context_id`, records the exact bounded exchange, file accesses, and side effects, and makes no pass/fail judgment.
- After all trials finish, dispatch a separate Review critic to evaluate the rendered target, canonical evidence, expectations, and raw trial files. Raw trials and reports stay in the private workspace.
- A failed Review creates a new Author revision pinned to the same curriculum and a fresh Review (up to 3 repair cycles). Never weaken a capability claim merely to hide an affordance the evidence supports.

## Evidence Rules

- Speech establishes stated intent or explanation.
- A visible-state claim requires visual evidence; an action or transition requires ordered before/after evidence; a successful procedure requires both action and observable result.
- Exact code, commands, labels, or values remain uncertain when illegible. Two probes that add no evidence are a stopping signal, not permission to guess.
- Low-confidence exact details never become authoritative instructions.
- Keep source-acquisition coverage and semantic coverage separate. Every material semantic unit gets a disposition; every included core or supporting unit appears in a grounded artifact.
- Prefer native host multimodal inspection before any hosted vision provider. Keep hosted vision disabled unless native viewing is unavailable or the user explicitly authorizes the privacy and cost boundary.

## Generated Skill Contract

Every generated course Skill lives outside the private workspace and contains five fixed root records plus at least one authored Markdown artifact:

```text
<course-skill>/
├── SKILL.md
├── source-map.md
├── sources.md
├── provenance.json
├── build-manifest.json
├── chapters/      # independently loadable teaching units
├── exercises/     # practice tasks
├── solutions/     # after-attempt answers and scored rubrics
├── playbooks/     # operational workflows
├── reference/     # decision rules, trade-offs, quick lookup
└── assets/        # indispensable evidence-grounded PNGs only
```

Supported optional root files also include `learning-path.md`, `glossary.md`, `patterns.md`, and `cheatsheet.md`. Only create files the evidence justifies—never manufacture symmetrical directories, split files that always load together, or include raw video, audio, full transcripts, databases, cookies, caches, or decorative frames.

### Quality Floor

- Produce a reusable capability product, not a transcript summary or thin index files. Preserve named frameworks, principles, demonstrated techniques, reasoning, examples, qualifications, counterpoints, warnings, failure modes, decision rules, and open questions.
- Make each teaching artifact self-contained with a clear user job, grounded explanation, concrete example (when supported), and retrieval/transfer prompt. Do not repeat the same shallow summary across files.
- Label generator-created exercises, adaptations, or conceptual workflows as inference rather than pretending the source demonstrated them.
- Mark generated Skills with `<!-- video-to-skill:course-skill:v2 -->`.
- Keep resident `SKILL.md` concise and operational: brief course-specific welcome on empty invocation, ask at most 3 starter questions after the user begins, teach one concept at a time, withhold solutions until an attempt or explicit request, and load only the smallest relevant artifact and its linked visual.
- Keep learner progress in conversation or host memory, never in the portable package. Keep failed/partial sources visible in `sources.md` and private local paths out of every shareable file.
