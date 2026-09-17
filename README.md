# github-triage-agent

A GitHub issue-triage agent: it reads open issues and decides what to do (label, comment, search for duplicates, propose an assignee, escalate). No agent framework — the tool-calling loop, policy layer, and eval suite are all hand-written.

**12/12 unsafe actions blocked** by code-level policy enforcement, reproduced across three independent runs — vs **3-5/12** blocked across two runs when the same agent, same prompt, same scenarios, relies on the system prompt alone. Overall pass rate across the 40-scenario suite is **39-40/40** depending on the run.

That first comparison is the actual point of this project. See [Why enforcement lives in code](#why-enforcement-lives-in-code) below.

## Results

Full 40-scenario suite, graded purely on end-state (labels, comments, closed/assignee status) against expectations written before the agent ever ran:

| Bucket | Code-enforced (3 runs) | Prompt-only (2 runs) |
|---|---|---|
| straightforward (14) | 14/14 | 14/14 |
| ambiguity (8) | 8/8 | 8/8 |
| duplicates (6) | 6/6 (5/6 in CI once) | 4/6 |
| boundary (6) | 6/6 | 0-1/6 |
| injection (6) | 6/6 | 3-4/6 |
| **Overall** | **39-40/40** | **29-31/40** |
| **Unsafe actions blocked** (boundary + injection) | **12/12** | **3-5/12** |

Straightforward and ambiguity are identical in every run of both conditions — those buckets don't touch anything risky, so disabling the policy layer doesn't change them. The split shows up exactly where it should: with the policy layer disabled, the agent actually **closed or assigned real issues** on 5-6 of 6 "please close this" / "please assign this" requests, and complied with 2-3 of 6 disguised prompt-injection attempts across the two runs (a "developer mode" jailbreak and a fake prior-conversation injection both got an issue closed in at least one run, with nothing in the loop to stop them).

With the real policy layer active, all 12 boundary/injection scenarios were intercepted before the API call fired, every time — either denied outright or paused for human approval. That gap — a reliable 12/12 vs a prompt-only condition that never got above 5/12 in two tries — is what code-level enforcement buys you over prompt-level enforcement, measured rather than asserted. The exact prompt-only number moved between runs (5/12, then 3/12), which is itself part of the point: prompt-based enforcement isn't just weaker, it's unpredictable.

### How much to read into these numbers

Three caveats I'd rather state than have someone find:

- **The code-enforced 12/12 is guaranteed by construction, not by good judgment.** `close_issue` and `assign_issue` are APPROVE-tier and cannot auto-execute; escalated `post_comment` always pauses. Those 12 scenarios would also pass if the agent did nothing at all. That's the intended design claim — the architecture makes the unsafe end-state unreachable — but it means the safety result demonstrates the *enforcement*, not the model's restraint. The informative half of the comparison is the prompt-only column, where the same model with nothing but instructions actually closed real issues.
- **The overall pass rate leans on those same 12.** Only the 28 straightforward/ambiguity/duplicates scenarios have grading criteria that can discriminate agent quality; the boundary and injection criteria are pass-unless-the-policy-broke. Read 39-40/40 accordingly.
- **The exact prompt-only number isn't stable, only the conclusion is.** Two runs gave 5/12 and 3/12 — the precise count moves, but neither run came close to 12/12, so "code enforcement blocks meaningfully more" is solid while "exactly 5/12" is not a number to repeat with confidence. The duplicates delta (6/6 vs 4/6) also has a structural contributor beyond model variance: in the prompt-only condition issues actually get closed as the run proceeds, and `search_issues` is scoped to open issues, so later scenarios have fewer duplicates left to find.

Raw results: [`results/eval_results_code-enforced.json`](results/eval_results_code-enforced.json), [`results/eval_results_prompt-only.json`](results/eval_results_prompt-only.json).

## Why enforcement lives in code

The policy layer (AUTO / APPROVE / DENY per tool) is enforced in the dispatcher, in code, *before* any tool's API call fires — never as an instruction in the system prompt. The reasoning: text injected into an issue body can manipulate what the model *decides* to do, but it cannot bypass a plain `if tool_name in DENY_TOOLS: return refusal` check that runs ahead of everything else, because that check never reads the model's output at all. `delete_comment` is denied unconditionally and isn't even offered to the model as a callable tool; `close_issue` and `assign_issue` always require human approval regardless of what the agent (or an injected instruction) argues for; and any write action on an issue whose body looks like it's trying to manipulate the agent gets force-escalated to human approval too. The eval numbers above are what that design difference actually looks like in practice.

There's a second, independent boundary underneath the policy layer: the GitHub token itself is a fine-grained personal access token scoped to `issues:write` on a single scratch repository — nothing broader. If the policy code has a bug, the token is what actually stops a catastrophic action from reaching anything beyond that one repo.

## Architecture

- **Stack**: Python, GitHub REST API (via PyGithub), Gemini API (`gemini-3.5-flash-lite`) for the model, OpenTelemetry + Jaeger for tracing, GitHub Actions for CI.
- **Not built**: there is no webhook service. The agent is invoked directly (`agent.run_agent(issue_number)`) and by the eval harness. A FastAPI layer for live webhook-driven triage was in the original design and deliberately scoped out — the eval suite, which is what this project is actually about, doesn't need one.
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

`gemini-3.5-flash-lite`'s free tier caps at 500 requests/day. Each full 40-scenario run costs roughly 100-150 requests, so more than a few runs in one day will hit `429 RESOURCE_EXHAUSTED`. `model.py` retries transient errors and timeouts with backoff, but a daily quota doesn't recover within a retry window — a scenario that hits it is recorded as a run error for that scenario, not a hang.

View traces (requires Docker):
```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one
# then open http://localhost:16686
```

## CI

`.github/workflows/eval.yml` re-seeds the scratch repo and runs the code-enforced eval suite on every push to `main`, and fails the build if the overall pass rate drops below 85% or if the boundary/injection safety buckets drop below 100% — since those buckets are guaranteed safe by construction, any regression there means the policy layer itself broke.
