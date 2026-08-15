# AGENTS.md — GWS Harvester Agent Contract

## Read first

Before making architectural or performance changes, read `docs/HARVESTER_SCALE_BLUEPRINT.md`.

### Real operating environment

This project does **not** assume OpenAI API agents. The high-level controller is ChatGPT Web (currently GPT-5.6 Sol in the user's workflow) using native GitHub and Google Drive/Sheets connectivity to launch, inspect, edit, debug and steer work. GitHub Actions / CircleCI / scripts are execution workers.

Design outputs for that reality: durable queues, checkpoints, JSON/CSV summaries, compact logs, deterministic executors and source-backed state that ChatGPT Web can inspect through native connectors. Do not introduce an OpenAI API dependency unless the user explicitly requests one.

## Strategic direction

This repository should evolve from a mostly regex/rule-based lead miner into a **hybrid deterministic + local-LLM qualification system** that can run cheaply on free/ephemeral CI compute, while also implementing the broader scale architecture in the blueprint.

The goal is NOT to replace deterministic checks with an LLM. Use a small open-source model only where semantic judgment adds value.

```text
raw candidate
  -> deterministic normalization / exact checks
  -> cheap rules and hard evidence
  -> local small-LLM classifier for ambiguous cases
  -> confidence router
      -> accept/reject when confidence is high
      -> escalate uncertain/contradictory cases to GPT Web review
```

## Primary GWS use case

The highest-value near-term use is **official-site/entity matching**.

Given a target business and a candidate website, determine whether the site truly belongs to that business using evidence such as:

- normalized business name;
- city / market;
- street address;
- public phone;
- email/domain identity;
- service/category match;
- legal/footer/about/contact-page text;
- contradictions such as another city, another company name, unrelated niche, directory/listicle status.

Desired machine-readable output:

```json
{
  "decision": "MATCH|PROBABLE|WRONG|UNCERTAIN",
  "confidence": 0.0,
  "matching_evidence": [],
  "contradictions": [],
  "reason": "short explanation"
}
```

Do not allow an LLM to invent business facts. Evidence must come from fetched candidate text / structured input. UNKNOWN is valid.

## Secondary GWS uses

Small local models may help classify:

- official site vs directory / social page / aggregator;
- active real website vs placeholder / parked / broken / nearly empty site;
- business niche / service fit;
- whether a website is sufficiently poor/outdated to merit opportunity review;
- structured extraction of name, phone, address, email, services and legal entity from already-fetched public text.

## Model/runtime preference

Optimize first for CPU-friendly GGUF inference under `llama.cpp` on GitHub-hosted runners.

Candidate families to benchmark rather than hard-code forever:

- Qwen 3/3.5 class ~3B–4B instruct models;
- Phi-4-mini class ~3.8B;
- SmolLM3 ~3B;
- sub-1B Qwen-class model only as an ultra-cheap garbage pre-filter.

Prefer quantized 4-bit models where accuracy is sufficient. Keep prompts/context small.

## CI design constraints

- Do not make every runner repeatedly download multi-GB model weights if avoidable; use caching or a shared inference service when economics are better.
- Keep local-LLM use optional/fail-open for discovery: a model failure must not destroy harvested data.
- Cache model/runtime separately from lead outputs.
- Measure RAM, model download time, inference latency and throughput.
- Preserve deterministic fallbacks.
- Prefer long-lived queue consumers, work stealing and adaptive batches where compatible with the current runtime.

## Required benchmark before trusting automation

Create a real project benchmark from labeled GWS examples rather than generic academic scores.

Suggested initial test set: 200–500 actual business/site pairs with a trusted reference label.

Track at minimum:

- precision for MATCH;
- recall for MATCH;
- false-positive rate;
- false-negative rate;
- UNCERTAIN rate;
- latency per candidate;
- peak RAM;
- throughput per runner.

High-confidence autonomous acceptance should only be enabled once empirical precision is strong enough on our own data.

## Implementation philosophy

1. **Code first for facts.** Exact normalized phone/domain/address matches should not require an LLM.
2. **LLM for semantic ambiguity.** Use it where textual/business-context judgment is needed.
3. **GPT Web for high-level control and hard cases.** Persist its decisions as reusable labels.
4. **Confidence routing, never blind trust.** Low-confidence or contradictory cases are escalated.
5. **Structured machine-readable outputs.** Prefer validated JSON for decisions.
6. **No hallucinated contacts or identities.** Blank/UNKNOWN beats guessing.
7. **Cache and checkpoint.** Repeated runs should not redo unchanged work.
8. **Measure yield.** Track source/query/geo performance and allocate compute accordingly.
9. **Optimize commercial opportunity, not raw row count.**
10. **Implement incrementally without breaking the current OSM/CommonCrawl -> dedupe -> fetch -> regex scoring pipeline.**