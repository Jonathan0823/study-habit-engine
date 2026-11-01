# Domain Docs

## Layout

Single-context: `GLOSSARY.md` at the repository root and ADRs in `docs/adr/`.
Create these lazily when domain terms or architectural decisions are resolved.

## Before exploring

Read the root `GLOSSARY.md` and ADRs relevant to the area being explored.
If a root `GLOSSARY-MAP.md` exists, follow it to relevant context glossaries
and also check context-specific ADR directories.

If these files do not exist, proceed silently. Do not flag their absence
or suggest creating empty files upfront.

## Vocabulary

Use the glossary's canonical terms in code discussions, issue titles,
and design proposals. Do not substitute synonyms it explicitly avoids.

If a needed concept is missing, reconsider whether it belongs, or note
the gap for domain modeling.

## ADR conflicts

Explicitly flag proposals that contradict an existing ADR, identifying
the ADR and why reconsidering it may be justified.
