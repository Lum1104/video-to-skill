# Ambitious Startups Skill: V1–V2 Product Audit

## Historical status

This is a point-in-time audit of two generated artifacts, not a description of the current generator. Its package measurements and product comparison remain useful, but its then-current generator gaps led to the implemented curriculum checkpoint, instructional-affordance ledger, isolated behavior validation, artifact-language contract, adaptive depth profiles, named editions, tool provenance, and evidence bundles documented elsewhere in this repository. Update and fold-in of an existing generated Skill remain deliberately deferred.

## Scope

This audit compares only the generated Skill artifacts:

- the V1 `ambitious-ai-startup-playbook` package at commit [`9ce0a7d`](https://github.com/Lum1104/ambitious-ai-startup-playbook/commit/9ce0a7d9e0152881f3cc2f84f691f564ffcb29ce);
- the V2 `ambitious-startups-in-the-ai-era` package generated from the same 39-minute Y Combinator interview.

Repository presentation, installation documentation, licensing files, and other manually added showcase material are outside the comparison. The question is strictly whether V2 produced a better Skill.

## Executive conclusion

V2 is a substantially better evidence compiler and a stronger long-term foundation, but it is not an unqualified improvement as a founder-coaching product.

It preserves much more of the source, represents uncertainty more honestly, supports current validation, and provides a better runtime contract. However, its generated teaching and application layer is thinner in several places. In particular, V2 regresses in quick decision lookup, operational review playbooks, capstone practice, scoring, progressive hints, validation, and recovery guidance.

The right direction is not to revert to V1. It is to retain the V2 evidence and runtime architecture while restoring the strongest V1 instructional affordances.

## Quantitative comparison

The counts below exclude repository-only presentation material and compare the Skill packages themselves.

| Measure | V1 | V2 | Interpretation |
| --- | --: | --: | --- |
| Resident `SKILL.md` | 1,189 words | 1,438 words | The V2 front door is larger, not smaller. |
| Supporting instructional artifacts | 9 files | 4 files | V2 consolidated the product layer aggressively. |
| Supporting instructional prose | 3,446 words | 2,685 words | V2 reduced directly usable teaching and workflow content by about 22%. |
| All human-facing instructional prose, including `SKILL.md` | 4,635 words | 4,123 words | The overall instructional layer is about 11% smaller. |
| Core generated package size | 73,410 bytes | 140,544 bytes | V2 is almost twice as large because evidence metadata grew substantially. |
| Canonical semantic units | none | 54 | V2 adds high-recall semantic accounting. |
| Semantic relations | none | 21 | V2 explicitly preserves relationships among source ideas. |
| Build manifest | none | present | V2 has a portable build receipt and managed-file hashes. |

The apparent contradiction is real: V2 is a larger package but a smaller instructional product. Most of the growth is in `source-map.md` and `provenance.json`, not in lessons, exercises, playbooks, or quick-reference tools.

## What V2 improves

### 1. Source coverage

V2 accounts for 54 semantic units:

- 43 are directly included;
- 4 are merged into retained units with explicit reasons;
- 7 are preserved as context-only units.

No core source unit silently disappears. V2 also restores several subjects that were absent or underdeveloped in V1's teaching artifacts:

- hard-tech participation and falling startup costs;
- startups as a mechanism for economic renewal and possible power diffusion;
- taste, agency, business physics, and tool fluency as distinct founder capacities;
- the fact that the PhD and credentials question is asked but not answered;
- earnest building versus status-seeking or sarcastic attacks;
- time-sensitive model, inference, and token-demand forecasts;
- the 640K memory analogy and its limits;
- privacy, meaningful work, sustainable ambition, failure, and happiness.

This makes the V2 course more faithful to the interview as a whole rather than only to a small set of selected founder principles.

### 2. Evidence and uncertainty

V2 more consistently distinguishes:

- a speaker's claim;
- a project fact;
- a curriculum or application inference;
- a time-sensitive prediction;
- a promotional assertion;
- an unanswered question;
- a claim that lacks the visual or technical evidence required for stronger treatment.

The safety incident, Bay Area advice, YC advantage, productivity figures, model progress, inference growth, and token-demand claims all receive more careful boundaries. V2 is less likely to convert rhetoric, recollection, or forecasts into authoritative operating instructions.

### 3. Runtime behavior

The V2 `SKILL.md` is a stronger operational front door. It adds:

- a side-effect-free empty invocation;
- course-specific starter questions;
- adaptive Learn, Practice, Apply, and Reference behavior without exposing an internal mode menu;
- language behavior;
- outside-knowledge and inference boundaries;
- an evidence-derived capability profile;
- recommended and alternate learning paths;
- explicit source and application limits.

This is a meaningful improvement in host usability and progressive disclosure.

### 4. Current structural compatibility

The V2 package passed the validator used for this audit with code checks:

```text
VALID
Files checked: 9
No issues found.
```

The V1 package uses the legacy provenance schema and lacks V2 semantic units, so the validator used at audit time rejected it. This is a format and compatibility result, not evidence that V1's instructional content is inferior.

## Where V2 regresses

### 1. Quick reference

V1 contains a compact decision-rules artifact that maps:

- a condition;
- the corresponding course decision;
- a guardrail;
- a source timestamp.

V2 replaces this practical lookup surface with a 692-line canonical source map. The source map is much better for auditability and exact evidence retrieval, but it is worse for answering small operational questions quickly.

This creates a distinction that the current capability profile does not fully capture: V2 has strong evidence reference capability but weaker decision reference ergonomics.

### 2. Independent application playbooks

V1 has two independently useful workflows:

1. a startup-opportunity review;
2. a safety, power, and agency review.

Each includes prerequisites, a procedure, an expected output state, validation, recovery, and limitations. The governance workflow also asks who controls access, data, model behavior, economics, rules, and appeals, and considers exit, reversibility, portability, alternative providers, and transparency.

V2 merges these concerns into one broader seven-part venture decision guide. The consolidated guide adds useful founder-operating-system and wellbeing questions, but loses much of the operational depth:

- no explicit expected state for each review;
- no structured validation section;
- little recovery guidance;
- less detailed power mapping;
- less support for diagnosing a failed or non-informative next step.

Startup review and governance review can be requested independently, so they meet V2's own criterion for separate artifacts with independent loading reasons. Their consolidation is an over-application of artifact minimization.

### 3. Capstone practice and feedback

V1 provides one integrated Founder Thesis Stress Test with:

- seven required dimensions;
- explicit success criteria;
- a complete submission process;
- a 0–2 scoring rubric for each dimension;
- a minimal hint sequence;
- a retry before full solution disclosure;
- an example response structure.

V2 replaces it with seven short decision labs. This improves modular practice and topic breadth, but each lab is brief and the associated rubric has no score, hint progression, retry protocol, or integrated founder-thesis submission.

The two forms serve different purposes and should coexist:

- micro-labs for focused retrieval and transfer;
- a capstone for synthesis, scoring, feedback, and deliberate retry.

### 4. Teaching scaffolding

Each V1 chapter includes learning objectives, common failure modes, a knowledge check, a micro-exercise, and localized evidence windows.

The V2 course is more coherent and comprehensive as a source-faithful reading, but it behaves more like a well-structured essay. It relies on the runtime agent to generate retrieval and transfer questions dynamically rather than providing strong section-level teaching scaffolds.

That design can work, but it makes teaching quality more dependent on the consuming model. V2 improves knowledge coverage while reducing the consistency of the authored learning experience.

## Root cause

The V2 build manifest records:

```json
"parent_build_id": null
```

The package was synthesized as a new build rather than created through an update or fold-in operation over the V1 Skill. As a result, V2 protected source semantics but did not treat the generator-created instructional value in V1 as material that required preservation or an explicit replacement decision.

At audit time, this exposed a gap in the quality model:

> Semantic coverage is not instructional capability coverage.

The semantic ledger can prove that source meaning was retained while the product still loses:

- decision aids;
- workflow checkpoints;
- expected states;
- validation and recovery behavior;
- scoring rubrics;
- hint sequences;
- retry mechanics;
- capstone synthesis.

At the time of this audit, the critic and validator had stronger defenses against source meaning loss than against instructional affordance loss.

## Recommended target

Keep the following V2 capabilities:

- the resident runtime contract;
- the thematic course and alternate paths;
- the 54-unit canonical semantic map;
- semantic relations;
- V2 provenance;
- the build manifest;
- explicit uncertainty and inference handling;
- the seven modular decision labs.

Restore or regenerate the following product artifacts:

1. a compact decision-rules reference;
2. an independent startup-opportunity review playbook;
3. an independent safety, power, and agency review playbook;
4. an integrated Founder Thesis Stress Test;
5. scored rubrics, progressive hints, retry behavior, and an example structure;
6. section-level retrieval or transfer prompts for the main course.

## Generator implications recorded at audit time

This audit recommended that V2 criticism and validation track two separate ledgers; the current workspace-centered pipeline implements both:

1. **Semantic coverage:** whether all material source meaning is included, merged, contextualized, or omitted with a defensible reason.
2. **Instructional affordance coverage:** whether the generated Skill has enough independently useful teaching, practice, application, feedback, validation, recovery, and reference surfaces for its claimed capability levels.

For an existing related Skill, regeneration should also compare the prior artifact plan against the new one. Removing an artifact should require an explicit judgment that its user job is preserved elsewhere, not merely that its underlying source claims remain represented.

## Final assessment

V2 is clearly better as an evidence-grounded, maintainable Skill foundation. As an immediately useful founder coach, it is approximately a lateral move with specific regressions in Apply, Practice, and quick Reference behavior.

The V2 architecture should remain. The next improvement is to rebuild the product layer on top of it, not to reduce the semantic map or return to the V1 format.
