---
title: Conservative Single-Backend Scheduling - Plan
type: feat
date: 2026-09-18
topic: conservative-single-backend-scheduling
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# Conservative Single-Backend Scheduling - Plan

## Goal Capsule

- **Objective:** Let an operator run independent manifest tasks concurrently through one CLI backend without risking collisions.
- **Means:** Build a conservative pre-launch scheduler that inspects the repository and manifests, then emits a deterministic schedule.
- **Product authority:** This work governs normal manifest dispatch only; triple mode's exact three-backend semantics and tracker write boundaries remain unchanged.
- **Open blockers:** None.

---

## Product Contract

### Summary

Relay will offer an interactive choice between `parallel` and `serial` before a normal dispatch starts. Parallel is an optimization request, not permission to take risks: Relay runs only pairs it can establish are independent and serializes every uncertain or potentially conflicting pair. Noninteractive execution defaults to serial.

### Problem Frame

Relay currently permits only one in-flight task per backend. That preserves repository safety but leaves capacity unused when a manifest intentionally targets one CLI tool and its tasks genuinely do not overlap.

### Requirements

**Policy selection**

- R1. Before launching an eligible normal manifest, Relay presents a multiple-choice run-policy selection with `serial` as the default and `parallel` as the alternative.
- R2. Noninteractive, detached, and otherwise non-promptable runs select `serial` unless an explicit compatible policy is supplied.
- R3. Triple execution preserves its current orchestration and does not enter the single-backend scheduling policy flow.

**Conservative schedule**

- R4. A `parallel` selection causes Relay to inspect the target repository and task declarations before launch and produce a pre-launch schedule.
- R5. Relay may overlap a pair only when deterministic evidence establishes disjoint, bounded work; missing evidence, ambiguous analysis, or a detected conflict creates a serial dependency.
- R6. Broad or global impact serializes with every other task, including shared configuration, dependency manifests, migrations, CI, root documentation, generated output, and unknown write scope.
- R7. The scheduler may use read-only semantic analysis to enrich its evidence, but that analysis cannot authorize concurrency; disagreement or low confidence becomes serialization.
- R8. The schedule preserves manifest order among tasks connected by serial dependencies and preserves Relay's existing serialized landing behavior.

**Operator visibility and safety**

- R9. Relay shows the selected policy, each planned concurrency group, and the reason for every serialized edge before workers launch.
- R10. Once launch begins, Relay executes the computed schedule without requesting further operator authorization.
- R11. Per-task worktree isolation, repository lease behavior, hooks, verification, and failure semantics remain in force for every scheduled task.

### Key Decisions

- KTD1. **Use `parallel` and `serial` as the operator-facing policy names.** (session-settled: user-directed — chosen over alternate policy terminology: they describe the observable run behavior.) Governs R1–R3.
- KTD2. **Ask interactively and default noninteractive runs to serial.** (session-settled: user-directed — chosen over automatic parallel opt-in: the operator explicitly selects optimization while unattended work stays safe.) Governs R1–R2.
- KTD3. **Infer independence from repository evidence.** (session-settled: user-directed — chosen over declaration-only scheduling: Relay should automate as much of the decision as possible.) Governs R4–R7.
- KTD4. **Treat uncertainty as a serial dependency.** (session-settled: user-directed — chosen over probabilistic parallelism: false confidence must not create collisions.) Governs R5–R7.
- KTD5. **Use a layered, conservative scheduler.** (session-settled: user-directed — chosen over path rules alone or model-led scheduling: deterministic evidence is the safety authority and semantic analysis only refines it.) Governs R4–R8.
- KTD6. **Display, but do not gate on, the pre-launch schedule.** (session-settled: user-directed — chosen over per-schedule approval: visibility helps operators without adding an authorization pause after launch.) Governs R9–R10.

### Acceptance Examples

- AE1. Given a prompt-capable normal manifest, when dispatch begins, then Relay offers `serial` and `parallel`, with serial selected by default.
- AE2. Given a noninteractive normal manifest with no explicit policy, when dispatch begins, then Relay runs serially and records that choice.
- AE3. Given a parallel selection and two tasks with clearly disjoint bounded paths, when the pre-launch analysis completes, then their schedule may share a concurrency group while their landings remain ordered.
- AE4. Given a parallel selection and any task that touches a dependency manifest, migration, CI configuration, root documentation, generated output, or an unknown scope, when scheduling completes, then that task has serial edges to all peers with stated reasons.
- AE5. Given incomplete, conflicting, or low-confidence analysis for a pair, when scheduling completes, then the pair is serialized rather than overlapped.
- AE6. Given a computed schedule, when workers begin, then Relay does not seek another authorization and preserves existing worktree, hook, verification, and failure behavior.

### Scope Boundaries

- Triple mode is out of scope for this scheduler.
- The scheduler does not promise arbitrary concurrent workers; it only permits concurrency established as safe under the conservative evidence rule.
- Semantic analysis must remain read-only and cannot itself make a pair eligible for overlap.

### Sources / Research

- `skills/relay/scripts/relay/run.py` currently holds one backend slot at a time and serializes landing.
- `CONCEPTS.md` defines Dispatch as per-task worktree execution with serial manifest-order merge.
- `README.md` documents normal serial runs and triple mode as a separate execution profile.
