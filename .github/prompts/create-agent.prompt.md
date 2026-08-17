---
mode: agent
description: Create a new project-scoped coding agent blueprint and starter implementation.
---

You are creating a focused coding agent for this repository.

Goal:
- Create a new agent scaffold that helps automate a specific workflow in this project.
- Keep changes minimal, production-safe, and consistent with the existing FastAPI + worker architecture.

What to do:
1. Ask the user for the agent purpose if it is not already provided.
2. Inspect current files before editing:
   - app/main.py
   - app/worker.py
   - app/db.py
   - README.md
3. Propose a short implementation plan with:
   - New/updated files
   - Data flow
   - Failure handling
4. Implement only the required changes.
5. Validate with the most relevant checks (for example, import/syntax checks or project tests if present).
6. Summarize exactly what was created and how to use it.

Guardrails:
- Preserve existing public behavior unless the user asks for a behavior change.
- Prefer additive, backwards-compatible edits.
- Do not remove unrelated code.
- Keep comments concise and only where logic is non-obvious.

Output format:
- "Plan"
- "Changes Made"
- "Validation"
- "How To Use"
