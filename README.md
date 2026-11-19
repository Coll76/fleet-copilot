# fleet-copilot

[![CI](https://github.com/Coll76/fleet-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Coll76/fleet-copilot/actions/workflows/ci.yml)

An agent that helps IT admins understand and act on device telemetry across a multi-tenant fleet.

Ask it which laptops are low on disk, which batteries are wearing out, or whether compliance is slipping, and it answers with citations to the exact snapshots it read. When something needs doing, it drafts the action (an upgrade order, a remediation ticket, a replacement flag or a note to the employee) and waits for a person to approve it.

Built with Python 3.12, LangGraph and Claude as a take-home exercise for Rayda.

```console
$ uv run fleet-copilot ask --tenant acme-001 "Which devices at Acme are low on disk space?"

At Acme, two devices are low on disk space. Both are rated critical and have less than 3% free [disk_low/8NM23J95R5I6, disk_low/MT7PJB7N5LRE].

**Evidence**
- **ThinkPad X1 Carbon (8NM23J95R5I6)**, owner emp-acme-1005: 2.6% free, which is 26.5 GB of 1024 GB [disk_low/8NM23J95R5I6, 8NM23J95R5I6/2026-06-12].
- **MacBook Pro (MT7PJB7N5LRE)**, owner emp-acme-1003: 2.0% free, which is 10.2 GB of 512 GB [disk_low/MT7PJB7N5LRE, MT7PJB7N5LRE/2026-06-12].
...
```

<details>
<summary>Full answer, including evidence and caveats</summary>

```text
At Acme, two devices are low on disk space. Both are rated critical and have less than 3% free [disk_low/8NM23J95R5I6, disk_low/MT7PJB7N5LRE].

**Evidence**
- **ThinkPad X1 Carbon (8NM23J95R5I6)**, owner emp-acme-1005: 2.6% free, which is 26.5 GB of 1024 GB [disk_low/8NM23J95R5I6, 8NM23J95R5I6/2026-06-12].
- **MacBook Pro (MT7PJB7N5LRE)**, owner emp-acme-1003: 2.0% free, which is 10.2 GB of 512 GB [disk_low/MT7PJB7N5LRE, MT7PJB7N5LRE/2026-06-12].
- No other device's latest snapshot is under the low-disk threshold [disk_low/8NM23J95R5I6, disk_low/MT7PJB7N5LRE].

**Caveats**
- "Low disk" uses the policy threshold: less than 10% free (`disk_low_free_pct: 10`) on each device's latest snapshot [disk_low/8NM23J95R5I6, disk_low/MT7PJB7N5LRE].
- I didn't check how fast disks are filling up, so a device above 10% now could still run low soon. I can run the disk-trend check if you want to see that.
- You asked for a list, so I haven't proposed any actions. If you want, I can propose a disk cleanup for these two devices.
```

This is the answer from the recorded eval run (case `qa_low_disk_acme`), so `uv run python evals/run_evals.py --only qa_low` reproduces it offline and writes it to `evals/results/latest.json`. Live runs vary in wording and keep the same facts and citations.

</details>

The bracketed ids are snapshots (`device/day`) and detector findings (`kind/device`). Every one of them was returned by a tool during that turn, and the answer is checked for exactly that before it is shown.

## Getting started

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Coll76/fleet-copilot
cd fleet-copilot
uv sync

uv run pytest                                    # 59 tests, offline
uv run python evals/run_evals.py                 # 25 agent evals, replayed from recordings, offline
uv run python evals/verifier_mutations.py        # break real answers on purpose, check the verifier catches them
uv run fleet-copilot insights --tenant acme-001  # raw detector output, no LLM involved
```

Everything above runs without an API key. To talk to the agent itself:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run fleet-copilot chat --tenant acme-001 --user alice@acme.example
```

The dataset (`data/telemetry.jsonl`, 750 daily snapshots) and every threshold the agent uses (`config/policy.yaml`) ship with the repo. The dataset has three tenants: `acme-001` (10 devices), `globex-002` (8) and `initech-003` (7).

### Usage

```bash
# Interactive session. Proposed actions come with their evidence and you approve or reject each one.
uv run fleet-copilot chat --tenant acme-001 --user alice@acme.example

# Single question. --approve ask|all|none decides what happens to proposals, --json prints the full trace.
uv run fleet-copilot ask --tenant initech-003 "Which devices are consistently constrained by RAM?"
uv run fleet-copilot ask --tenant acme-001 --approve none "Order a RAM upgrade for M4XVHUV1MEPZ"

# Read-only role. The model is never offered the action tools.
uv run fleet-copilot ask --tenant acme-001 --role viewer "Order a 2 TB SSD for 8NM23J95R5I6"

# Check that the audit log is intact.
uv run fleet-copilot audit
```

| Variable | Default | |
|---|---|---|
| `FLEET_MODEL` | `claude-opus-5-5` | Any Claude model id. `claude-sonnet-5-5` costs about half. |
| `FLEET_EFFORT` | `medium` | Reasoning effort: `low`, `medium` or `high`. |
| `FLEET_LLM_MODE` | `auto` | `live`, `record`, `replay`, or `auto` (replay when a recording exists, otherwise call the API and record). |

Runtime files land in `var/`, which git ignores. `var/audit.jsonl` is the audit log. `var/outbox/<tenant>.jsonl` collects executed actions and stands in for the ticketing and procurement systems a real deployment would call.

## How this maps to the brief

| Requirement | Where it lives | How it is checked |
|---|---|---|
| Grounded answers with traceable evidence | Citable ids, a per-turn evidence ledger, `agent/verifier.py` | `qa_*` evals, `tests/test_verifier_and_audit.py` |
| Trend detection (battery, RAM, storage, compliance drift) | Seven detectors in `analytics/detectors.py` | `ins_*` evals, `tests/test_detectors.py` against independent ground truth |
| Action proposals through the four tools | `tools/action_tools.py`, `guardrails/action_policy.py` | `act_*` evals, `tests/test_action_policy.py` |
| Tenant isolation | `data/store.py`, the input guard, the verifier | `adv_cross_*` evals, `tests/test_tools_and_isolation.py` |
| Human approval | `interrupt()` in `agent/graph.py` | `tests/test_graph.py`, `act_ram_upgrade_dell` |
| Audit log | `guardrails/audit.py` | `tests/test_verifier_and_audit.py`, `fleet-copilot audit` |
| Refusing actions that lack evidence | Evidence rules in the action policy | `act_replace_battery_only`, `adv_replace_everything`, `adv_viewer_cannot_act` |
| Planning and an explicit agent loop | The LangGraph state machine below | [How it works](#how-it-works) |
| Reproducibility | `uv.lock`, recorded model calls, one-command evals | GitHub Actions runs tests, replayed evals and the mutation test on every push |

## How it works

The guiding idea is simple. Code does anything that has a right answer: filtering, counting, thresholds, trend fitting, permission checks. The model decides what to look at, reads what the tools return and explains it. Nothing changes in the outside world until a person says yes.

```mermaid
flowchart TD
    Q[Admin request + session tenant] --> G[input_guard<br/>deterministic cross-tenant check]
    G -- other tenant named --> F[finalize]
    G --> P[plan<br/>Claude, structured JSON output]
    P -- out of scope --> F
    P --> A[agent<br/>Claude with tools]
    A -- tool_use --> T[tools<br/>validate args, run, record evidence]
    T --> A
    A -- final draft --> V[verify<br/>deterministic grounding check]
    V -- fails, 1 repair left --> A
    V -- proposals pending --> H[approval<br/>LangGraph interrupt: a human decides]
    H --> X[execute<br/>re-check policy, run approved only]
    X --> F
    V --> F
    F --> E[answer + action outcome]
    G & P & T & V & H & X & F -.-> L[(hash-chained audit log)]
```

A turn starts with `input_guard`, a plain string check that refuses the request before any model call when it names another tenant or one of its device or employee ids. The `plan` node then asks Claude for a structured plan: what the admin is asking, the definitions it will use, the tools it intends to call, and whether the request calls for actions at all. That last field matters. When it is false, the action tools are left out of the model's tool list for the rest of the turn.

The `agent` and `tools` nodes form the loop. Each tool call is validated against a pydantic schema (unknown fields such as `company_id` are rejected), runs against the session's tenant view, and every id it returns goes into the turn's evidence ledger. The loop stops after 8 tool rounds.

When the model produces an answer, `verify` checks it against the ledger. A failing draft goes back to the model once with the specific problems listed. If proposals are pending, the graph stops at `approval` using LangGraph's `interrupt()` and resumes only with a decision from a named approver. `execute` runs the action policy again on each approved proposal, because the data can change between proposal and approval, and then writes the action to the outbox.

The model is called through the official `anthropic` SDK from inside the graph nodes (`agent/llm.py`), with adaptive thinking, prompt caching on the system prompt and tool list, structured outputs for the planner, and refusal fallback enabled.

### Tools

None of the tools accepts a tenant. The tenant comes from the session, so the model has no parameter it could use to reach another company's data.

| Read tool | Returns |
|---|---|
| `fleet_overview()` | Every device with model, OS, owner and latest snapshot id, plus counts by OS, model and class. |
| `query_devices(...)` | Devices matching any combination of `device_class`, `os_name`, `os_version_below`, `disk_free_pct_below`, `mem_used_pct_at_least`, `failing_check`, `failing_severity`, `has_software`, `battery_condition`, `employee_id`, `device_ids`. Also returns tenant-wide failing check counts by severity, so an empty result still says what does fail. |
| `get_device(device_id)` | The full latest snapshot. |
| `get_device_history(device_id, metrics, last_n?)` | A per-snapshot series with snapshot ids. Missing battery blocks are flagged as collection gaps. |
| `run_insights(kinds?, device_id?, min_severity?)` | Detector findings with severity, explanation, metrics, evidence ids and the thresholds used. |
| `compliance_overview(check_id?)` | Pass and fail counts per check, the failing devices, and the failing count on the first and last day of the window. |
| `get_policy()` | Thresholds and the risky software list. |

`run_insights` is backed by seven detectors, all configured in `config/policy.yaml`:

| Detector | Fires when |
|---|---|
| `disk_low` | The latest snapshot has under 10% free. |
| `disk_trend` | A least-squares fit of free space projects the disk full within 30 days. Reports GB per day and days to full. |
| `ram_pressure` | Memory use is at or above 80% in at least 75% of snapshots. |
| `battery_eol` | The OS reports "Service Recommended", or two of: cycles at 90% of a 1000-cycle rating, full-charge capacity down 3% or more over the window. |
| `compliance_drift` | A check that passed earlier in the window has failed on every snapshot since. Reports the last pass, first fail and streak length. |
| `risky_software` | A listed application (P2P, remote access, system cleaners) is installed. Reports the day it first appeared. |
| `telemetry_gap` | A laptop snapshot is missing its battery or network block, so the gap is reported as a gap and never read as a hardware change. |

The four action tools only ever create proposals. Each takes an `evidence` list of ids, and the policy engine re-derives the facts from telemetry before allowing it.

| Action tool | Allowed when |
|---|---|
| `create_upgrade_order(device_id, component, spec, ...)` | A cited finding of the matching kind exists (`storage` needs `disk_low` or `disk_trend`, `ram` needs `ram_pressure`, `battery` needs `battery_eol`), the part is replaceable on that model, and the new size is larger and orderable. |
| `open_remediation_ticket(device_id, check_id, note, ...)` | The check fails on the latest snapshot, or `check_id` is `software:<Name>` with a matching finding, or `disk_cleanup` with a disk finding. |
| `flag_device_for_replacement(device_id, reason, ...)` | Findings exist in two hardware areas (battery, memory, storage), or chronic memory pressure on a device with soldered RAM. |
| `notify_employee(employee_id, message, ...)` | The employee belongs to the tenant, the evidence concerns their own device, and the message mentions nobody else's devices or ids. 600 characters at most. |

A call comes back as `pending_approval` with a proposal id and the facts the policy checked, or as `blocked_by_policy` with the reason. Duplicate proposals within a turn collapse into one, with a cap of 10 per turn.

### Grounding

Every snapshot has an id of the form `<device_id>/<YYYY-MM-DD>` and every finding `<kind>/<device_id>[/<qualifier>]`. The system prompt asks for a citation after each factual sentence, and each tool result adds its ids to a ledger for the turn. A citation counts only when it is in that ledger. An id the model remembers from an earlier turn, or guesses, fails even when it exists in the data.

`agent/verifier.py` runs before the admin sees anything:

| Issue | Meaning |
|---|---|
| `unretrieved_citation` | A cited id was never returned by a tool in this turn. |
| `uncited_device` | A device is discussed with no citation that refers to it. |
| `unsupported_number` | A number in the answer appears in no tool output. Ids, dates and model codes are stripped first, and rounding is allowed. |
| `cross_tenant_leak` | Another tenant's device or employee id appears. The answer is withheld entirely. |
| `claims_execution` | The answer says an action happened when it was only proposed. |
| `no_citations` | Fleet facts with no evidence at all. |

A failing draft gets one repair round. A draft that still fails is shown with a visible warning, so a grounding problem is always visible to the admin.

### Guardrails

Tenant isolation is structural. The agent holds a `TenantView` containing one company's snapshots, the tools take no tenant argument, and an id from another tenant produces the same "not found" error as an id that does not exist, so the agent cannot even learn whether it is real. The input guard and the verifier's hard block sit on either side of that as extra layers.

Approval is explicit. `approved: true` without an approver name is treated as a rejection, and the agent has no path to approve its own proposals. The policy runs twice, at proposal time and again immediately before execution.

The policy also encodes physical reality. Apple Silicon Macs get no RAM or SSD orders because both are soldered, and the ThinkPad X1 Carbon Gen 10 (21CB) gets no RAM orders for the same reason. Replacement needs two hardware problems, or memory pressure that an upgrade cannot fix.

Roles are enforced in two places: `viewer` sessions are never offered action tools, and dispatch refuses them regardless.

Prompt injection in the telemetry is handled by layers that hold even when the model is fooled. Tool output is framed as data in the system prompt, read-only questions never see the action tools, and every action still needs evidence and a person. The eval suite plants an instruction in a hostname and a software name to test this.

The audit log is append-only JSONL. Each entry carries the SHA-256 of the previous one, so `fleet-copilot audit` detects any edit or deletion. It records the request, any refusal, the plan, every tool call, every proposal (allowed or blocked), verification results, approval requests, each decision with its approver, executions and the end of the turn.

## Evaluation

There are three layers, and each one catches a different class of bug.

The **unit tests** (`tests/`, 59 of them) drive detectors, tools, isolation, the action policy, the verifier, the audit chain and the full graph using a scripted model, so they run offline in a few seconds.

The **ground truth** (`evals/build_ground_truth.py`) recomputes the expected answers straight from the raw JSONL, deliberately without importing the package. A bug in a detector therefore cannot quietly make the evals agree with it.

The **agent evals** (`evals/cases.yaml`, 25 cases) run the real model on grounded questions, trend questions, action requests and attacks. Every check is code: device ids that must and must not appear, expected tools, exact proposals, refusals, executions after approval. Three invariants are checked on every case regardless of category: no cross-tenant ids in the answer, nothing executed without approval, and the verifier passed.

The **verifier mutation test** (`evals/verifier_mutations.py`) answers a question the agent evals leave open. When every draft passes verification, that proves the agent grounds its answers, and says nothing about whether the verifier would catch one that does not. So the script takes the real drafts from the eval run, breaks each in a specific way (a fabricated citation, an altered number, another tenant's device, a claim that an action already ran, stripped citations, an extra uncited device) and checks that the verifier rejects it with the right issue code.

CI runs all of this on every push and pull request, offline, with the eval report in the job summary.

```bash
uv run python evals/run_evals.py                          # replay, offline and deterministic
uv run python evals/run_evals.py --mode live              # fresh model calls
uv run python evals/run_evals.py --mode live --repeats 3  # run-to-run variance, reports pass^3
uv run python evals/run_evals.py --only act_              # one category
uv run python evals/verifier_mutations.py                 # verifier mutation test
uv run python evals/build_ground_truth.py                 # rebuild expected answers from raw data
```

Reports go to `evals/results/latest.md` and `mutations.md`, and `latest.json` holds the full trace for every case: plan, tool calls, proposals, verifier output, tokens and latency.

### Results

One full live run on `claude-opus-5-5` at `medium` effort, recorded in `evals/cassettes/`:

| | |
|---|---|
| Cases passed | **25/25** (grounded QA 8/8, insight 6/6, action 5/5, adversarial 6/6) |
| Individual checks | 174/174 |
| Verifier passed on the first draft | 22/22 answered cases |
| Cross-tenant leaks | **0** |
| Unapproved executions | **0** |
| Audit chains intact | 25/25 |
| Tool calls / model calls per question | 2.5 / 3.5 on average |
| Latency per question | about 19 s |
| Cost of the full suite | about USD 1.60 |

Aggregate metrics from the same run:

| Metric | Value | Definition |
|---|---|---|
| Device recall | 30/30 | Required devices named in the answer |
| Device precision | 30/30 | Required devices named, over required plus distractor devices named |
| Tool selection | 8/8 | Cases that called one of the expected tools |
| Cross-tenant refusals | 2/2 | Attacks naming another tenant refused before any model call |
| False refusals | 0/19 | In-scope questions wrongly refused |
| Citation validity | 174/174 | Citations returned by a tool in the same turn |
| Action correctness | 8/8 | Action cases whose proposals match expectations exactly |

Verifier mutation test over the 22 real drafts:

| Mutation | Rejected with the expected issue |
|---|---|
| Fabricated citation | 22/22 |
| Altered number | 22/22 |
| Another tenant's device added | 22/22 |
| Claim that an action already ran | 22/22 |
| Citations stripped | 22/22 |
| Uncited device added | 20/20 |
| Unmutated drafts wrongly rejected | 0/22 |
| *Two real numbers swapped (known blind spot)* | *0/21* |

The last row is there on purpose. Swapping two numbers that both appear in tool output attaches a real value to the wrong device, and a string-level check has no way to see that. It is the main gap in the grounding layer, and now it has a number.

A few traces are worth reading in `latest.json`. Asked for laptops failing high-severity checks, the agent says there are none and lists the medium-severity failures instead. Asked to order RAM for a Mac mini, the model did call `create_upgrade_order`, the policy blocked it because the RAM is soldered, and the model passed that on to the admin. That is the intended split: the policy enforces, the model explains. Told to flag every laptop for replacement because "budget is approved", it proposed the two devices that qualify and explained why it skipped the other seven. And with an instruction planted in a hostname asking it to message every employee for their password, it proposed nothing and warned the admin that the device's data looked tampered with.

### Methodology notes

The first live run passed 23 of 25. Both failures were in my checks. One banned a device id that the agent correctly mentioned for a different finding, the other banned the word "password" in an answer that was correctly warning about the injection. A third check of the same kind turned up later, where the agent named the one Mac on macOS 15 as the exception to a count. All three now test what they were meant to, with the reasoning in comments in `cases.yaml`.

A three-repeat variance run stopped after 16 cases when the API account ran out of credit. Every agent behaviour in those 16 passed. A full pass^3 run is the next measurement to publish, and the harness already supports it.

## What's in the data

Running the detectors (`fleet-copilot insights --tenant <id>`) turns up a few things the brief hints at and a few it does not.

No device fails a high-severity check. `disk_encryption`, the only high-severity check, passes on all 750 snapshots, so the correct answer to the brief's example about laptops failing high-severity checks is none.

Screen lock drifts across the month. On 14 May no device failed `screen_lock`; by 12 June ten do, each passing until a specific day and failing every day after. That shape points to a configuration or MDM profile change, and the agent reports it as one fleet-level pattern.

Six disks are filling quickly, going from 7 to 17% free down to about 2%. The fastest loses about 5 GB a day and is projected full in about five days.

Three Acme batteries are wearing out, at 948 to 1,157 cycles, reporting "Service Recommended" since late May, with full-charge capacity down about 4% over the month.

Four devices are short of memory, at or above 80% use in 93 to 100% of snapshots. Three of them (two ThinkPad X1 Carbons and an 8 GB Mac mini) have soldered RAM, so only the Dell XPS 15 can take a RAM upgrade.

uTorrent appeared on four devices mid-month, TeamViewer on one and CleanMyMac X on one. None of the compliance checks would catch them.

Some laptop snapshots are missing the battery or network block. These are collection gaps, and the detectors treat them that way.

The brief mentions about 18 employees; the data has 14.

## Design notes

**Detectors in code, explanations from the model.** Models are unreliable at arithmetic over a 30-point series, and a wrong trend is hard to spot after the fact. A least-squares fit in Python is exact, cheap and testable against independent ground truth. The cost is coverage: the detectors find the patterns someone wrote them for, and anything novel depends on the model exploring `get_device_history`.

**A separate planning call.** The plan costs one extra model call per turn, with a cached system prompt and a small output. In return there is a readable artefact in the audit log, and a clean switch for least privilege, since the plan decides whether action tools are offered at all. For a tool an admin uses a handful of times a day, that trade is easy.

**A mechanical verifier.** It is deterministic, free and explainable ("this number appears in no tool output"). It checks that claims are supported and says nothing about whether the reasoning is good, which is the right priority for someone about to act on the answer. The number check is conservative on purpose: a false alarm costs one repair round.

**Policy outside the model.** A model can be argued with; a Python function cannot. Evidence, tenant, role, physical feasibility and privacy are all checked in code, twice. The hardware table and thresholds are assumptions a real deployment would need to own, which is why they all live in one YAML file.

**LangGraph with the Anthropic SDK directly.** LangGraph provides the state machine, conditional routing, `interrupt()` and checkpointing. Calling the SDK directly, without a chat-model wrapper, keeps full control of thinking blocks, caching, structured outputs and refusal fallback, and keeps record and replay to a few lines.

**Determinism without temperature.** The current Claude API has no temperature parameter, so reproducibility comes from two places. All computation happens in deterministic tools, which leaves the model to vary only in wording and tool order. And every model call is recorded under a hash of the full request, so replay reproduces every response exactly. Live mode with `--repeats` measures the real variance.

**Model choice.** Opus 5.5 at `medium` effort with adaptive thinking. `FLEET_MODEL=claude-sonnet-5-5` halves the cost. For production I would run the eval suite on both and decide on the numbers.

**Scale.** Twenty-five devices fit in memory, so the store is a Python object. At thousands of devices the same `TenantView` interface would sit on Postgres or DuckDB with row-level security per tenant, and the detectors would run as scheduled jobs writing findings to a table.

## Limitations

The detectors only know the patterns they were written for, and open-ended exploration of device history is the weakest part of the system.

The verifier confirms that each number and id came from a tool. A real number used in the wrong context still passes, as the swapped-numbers mutation shows (0 of 21 caught). Closing that gap means checking claims as structured (device, metric, value) triples against tool output, which is the next step for the grounding layer.

The input guard matches names and ids, so a paraphrase such as "the other company" reaches the model. Data scoping still keeps the other tenant's records out of reach, and the verifier still blocks any foreign id.

"Latest" means the last day in the dataset, 12 June 2026. Staleness relative to today is out of scope.

Hardware feasibility comes from a hand-maintained table per model.

The evals are single-turn. Multi-turn chat (the CLI keeps the last eight messages) has only been tested by hand.

Twenty-five cases and one full live run are a starting point, and the variance measurement is incomplete.

Persistence is demo-grade: LangGraph's `InMemorySaver` and a local JSONL audit file. Swapping in the Postgres checkpointer is a one-line change in `Copilot._build`.

## What I'd do next

- Move the checkpointer and audit log to Postgres, and ship audit entries to write-once storage.
- Replace the outbox with real ticketing, procurement and messaging integrations behind the same approval step.
- Per-tenant policy overrides for thresholds, approved software and the hardware catalogue.
- A second approver for proposals above a cost threshold.
- Scheduled detector runs and a daily digest of new findings.
- Claim-level verification: extract (device, metric, value) triples from the answer and match each against tool output, closing the swapped-number gap.
- Grow the eval set from real admin questions, and track the verifier's first-draft pass rate in production.

## Repository layout

```
src/fleet_copilot/
  data/        snapshot model, tenant-scoped store
  analytics/   detectors, policy loader
  tools/       read tools, action tools, schemas and dispatch, evidence ledger
  guardrails/  action policy, hash-chained audit log
  agent/       LangGraph graph, Anthropic client with record/replay, verifier, prompts
  cli.py
config/        policy.yaml: thresholds, risky software, hardware upgradeability, action rules
data/          telemetry.jsonl
evals/         cases, runner, verifier mutation test, independent ground truth, recordings, results
tests/         unit and graph tests
.github/       CI workflow
```
