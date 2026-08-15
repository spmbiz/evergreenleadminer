# AGENTS.md — Local LLM Cascade Direction

## Strategic direction

This repository should evolve from a mostly regex/rule-based lead miner into a **hybrid deterministic + local-LLM qualification system** that can run cheaply on free/ephemeral CI compute.

The goal is NOT to replace deterministic checks with an LLM. The goal is to use a small open-source model only where semantic judgment adds value, while keeping obvious decisions in code.

Target architecture:

```text
raw candidate
  -> deterministic normalization / exact checks
  -> cheap rules and hard evidence
  -> local small-LLM classifier for ambiguous cases
  -> confidence router
      -> accept/reject when confidence is high
      -> escalate uncertain/contradictory cases to a stronger model or manual/GPT review
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

Do not allow an LLM to invent business facts. Evidence must come from the fetched candidate text / structured input. UNKNOWN is valid.

## Secondary GWS uses

Small local models may also help classify:

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

Prefer quantized 4-bit models where accuracy is sufficient. Keep prompts/context small (generally a few thousand tokens, not full advertised context windows).

## CI design constraints

- Do not make every runner repeatedly download multi-GB model weights if avoidable; use caching or a shared inference service when economics are better.
- Keep local-LLM use optional/fail-open for discovery: a model failure must not destroy already harvested data.
- Cache model/runtime separately from lead outputs.
- Measure RAM, model download time, inference latency and throughput.
- Preserve deterministic fallbacks.

## Required benchmark before trusting automation

Create a real project benchmark from labeled GWS examples rather than relying on generic academic scores.

Suggested initial test set: 200–500 actual business/site pairs with a trusted reference label.

Track at minimum:

- precision for MATCH;
- recall for MATCH;
- false-positive rate (most dangerous metric);
- false-negative rate;
- UNCERTAIN rate;
- latency per candidate;
- peak RAM;
- throughput per GitHub Actions runner.

High-confidence autonomous acceptance should only be enabled once empirical precision is strong enough on our own data.

## Implementation philosophy

1. **Code first for facts.** Exact normalized phone/domain/address matches should not require an LLM.
2. **LLM for semantic ambiguity.** Use it where textual/business-context judgment is actually needed.
3. **Confidence routing, never blind trust.** Low-confidence or contradictory cases are escalated.
4. **Structured JSON only** for machine decisions.
5. **No hallucinated contacts or identities.** Blank/UNKNOWN beats guessing.
6. **Benchmark each model on our workload.** Do not assume a newer model is automatically better.
7. **Optimize cost per correct decision**, not benchmark prestige.

When modifying this repo, agents should look for opportunities to implement this cascade incrementally without breaking the current OSM/CommonCrawl -> dedupe -> fetch -> regex scoring pipeline.