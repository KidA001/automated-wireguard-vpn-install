# Agents

Custom agents used by the [`/dev-code-review`](../skills/dev-code-review/SKILL.md) skill. Both are subagent-only (`user-invocable: false`) and review-only (no edit tools).

| Agent | File | Requested Model | Role |
| --- | --- | --- | --- |
| `Dev Code Review Reviewer GPT-6.1 Sol` | [dev-code-review-reviewer.agent.md](./dev-code-review-reviewer.agent.md) | GPT-6.1 Sol | Step 4: independent reviewer (Agent #2 bugs/contract, Agent #4 prior PR feedback) |
| `Dev Code Review Scout GPT-6 Luna` | [dev-code-review-scout.agent.md](./dev-code-review-scout.agent.md) | GPT-6 Luna (High reasoning) | Step 5: scores each issue 0-100 |

The `model` field is a best-effort request. If the model is unavailable, the runtime may fall back or fail; the skill reports models as requested, not verified.
