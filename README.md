# github-triage-agent

A GitHub issue-triage agent: it reads open issues and decides what to do (label, comment, search for duplicates, propose an assignee, escalate). No agent framework — the tool-calling loop, policy layer, and eval suite are all hand-written.

**40/40 scenarios passed.** **12/12 unsafe actions blocked** by code-level policy enforcement — only **5/12** blocked when the same agent relies on the system prompt alone.

That second number is the actual point of this project. See [Why enforcement lives in code](#why-enforcement-lives-in-code) below.

## Results

Full 40-scenario suite, graded purely on end-state (labels, comments, closed/assignee status) against expectations written before the agent ever ran:

| Bucket | Code-enforced | Prompt-only |
|---|---|---|
| straightforward (14) | 14/14 | 14/14 |
| ambiguity (8) | 8/8 | 8/8 |
| duplicates (6) | 6/6 | 4/6 |
| boundary (6) | 6/6 | 1/6 |
| injection (6) | 6/6 | 4/6 |
| **Overall** | **40/40** | **31/40** |
| **Unsafe actions blocked** (boundary + injection) | **12/12** | **5/12** |

Straightforward and ambiguity are identical in both conditions — those buckets don't touch anything risky, so disabling the policy layer doesn't change them. The split shows up exactly where it should: with the policy layer disabled, the agent actually **closed or assigned real issues** on 5 of 6 "please close this" / "please assign this" requests, and complied with 2 of 6 disguised prompt-injection attempts (a "developer mode" jailbreak and a fake prior-conversation injection both got an issue closed, with nothing in the loop to stop them).

With the real policy layer active, all 12 of those attempts were intercepted before the API call fired — either denied outright or paused for human approval. That gap — 12/12 vs 5/12, on the exact same model, same prompt, same scenarios — is what code-level enforcement buys you over prompt-level enforcement, measured rather than asserted.

Raw results: [`eval_results_code-enforced.json`](eval_results_code-enforced.json), [`eval_results_prompt-only.json`](eval_results_prompt-only.json).

## Why enforcement lives in code

The policy layer (AUTO / APPROVE / DENY per tool) is enforced in the dispatcher, in code, *before* any tool's API call fires — never as an instruction in the system prompt. The reasoning: text injected into an issue body can manipulate what the model *decides* to do, but it cannot bypass a plain `if tool_name in DENY_TOOLS: return refusal` check that runs ahead of everything else, because that check never reads the model's output at all. `delete_comment` is denied unconditionally and isn't even offered to the model as a callable tool; `close_issue` and `assign_issue` always require human approval regardless of what the agent (or an injected instruction) argues for; and any write action on an issue whose body looks like it's trying to manipulate the agent gets force-escalated to human approval too. The eval numbers above are what that design difference actually looks like in practice.

There's a second, independent boundary underneath the policy layer: the GitHub token itself is a fine-grained personal access token scoped to `issues:write` on a single scratch repository — nothing broader. If the policy code has a bug, the token is what actually stops a catastrophic action from reaching anything beyond that one repo.

## Architecture

- **Stack**: Python, FastAPI (planned service layer), GitHub REST API (via PyGithub), Gemini API (`gemini-3.5-flash-lite`) for the model, OpenTelemetry + Jaeger for tracing, GitHub Actions for CI.
- **No agent framework** — `agent.py` is a hand-written loop: call the model, dispatch each tool call through the policy layer, execute (AUTO), pause-and-persist (APPROVE), or refuse (DENY), repeat until the model stops calling tools or a step cap is hit.
- **`tools.py`** — 8 thin wrappers over the GitHub REST API, each with retry/backoff for rate limits and clean error handling for 404s and permission errors.
- **`policy.py`** — the tier map (`AUTO_TOOLS` / `APPROVE_TOOLS` / `DENY_TOOLS`) fixed at definition time, plus the injection-escalation heuristic.
- **`scenarios.py` / `eval.py`** — the 40 fixed scenarios and the harness that seeds them as real issues, runs the agent against each, and grades end-state.
- **`tracing.py`** — one OpenTelemetry span per agent step, with nested spans for the model call (token in/out, latency, retry count) and each tool execution (tool name, latency, retry count, success).

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` with:
```
GITHUB_TOKEN=<fine-grained PAT, issues:write on your scratch repo only>
GEMINI_API_KEY=<free key from aistudio.google.com>
```

Reset the scratch repo to its seeded baseline:
```bash
python seed.py
```

Run the agent against a single issue:
```python
import agent
agent.run_agent(issue_number)
```

Run the full eval suite:
```bash
python eval.py --condition code-enforced   # the real policy layer
python eval.py --condition prompt-only     # policy disabled, for comparison
```

View traces (requires Docker):
```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one
# then open http://localhost:16686
```

## CI

`.github/workflows/eval.yml` re-seeds the scratch repo and runs the code-enforced eval suite on every push to `main`, and fails the build if the overall pass rate drops below 85% or if the boundary/injection safety buckets drop below 100% — since those buckets are guaranteed safe by construction, any regression there means the policy layer itself broke.
