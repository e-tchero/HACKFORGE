# HACKFORGE

**Autonomous opportunity-to-execution agent.**

> Never register for an opportunity and then forget it.

HACKFORGE turns an opportunity email into an actively managed execution
project: detect, research, strategise, get human approval, build in an isolated
workspace, track deadlines, and report honestly on what actually happened.

---

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Opportunity discovery (Gmail → detection → registry) | **Working** (adapter hardened; not yet run against your real inbox) |
| 2 | Research engine (primary sources → evidence-backed findings) | **Working** (live HTTP fetch verified) |
| 3 | Strategy engine (candidates → scored recommendation → approval gate) | **Working** |
| 4 | Project workspace scaffolding + `HACKFORGE_STATE.md` | **Working** |
| 4.5 | Real-world validation, evidence trail, synthesis, validation report | **Implemented; awaiting real Gmail credentials** |
| 5 | Build agent / computer execution | Not started |
| 6 | Accountability (inactivity detection, scheduled reports) | Partial — engine exists, not scheduled |
| 7 | Submission preparation | Not started |

This is deliberately incremental, per specification section 30.

**Not yet wired to your real Gmail.** The Gmail adapter requires OAuth
credentials; see setup below. Until `GMAIL_CREDENTIALS_PATH` is configured,
`scan` exits with `GMAIL READ: FAILED (missing_config)` and reads nothing.

**Not yet validated against a real registered hackathon.** The full pipeline
has been exercised against realistic fixtures and live HTTP, but the
real-world test from specification section 34 has *not* been performed
because no real inbox has been read. Treat every capability below as
"verified in test/live-HTTP", not "verified in production".

---

## Principles enforced in code

These are not aspirations in a document — each has a test:

1. **Dates are never invented.** A date that cannot be parsed stays `None` and
   serialises as `UNKNOWN`. A bare "March 15" with no year is rejected outright
   rather than guessed.
2. **Research is never fabricated.** A finding is `VERIFIED` only when a real
   document backs it. Network failure yields `UNKNOWN` findings, not plausible
   text.
3. **No recommendation without evidence.** With zero verified findings the
   strategy engine refuses to recommend and says why.
4. **Reasoning is exposed.** Recommendations name the criteria that drove them,
   the weight, the score, and the margin over the runner-up.
5. **Human approval is not optional.** Nothing proceeds past `AWAITING_APPROVAL`
   without a recorded decision.
6. **Autonomy is enforced, not documented.** Workspace creation at level 0 is
   refused with an explanation.
7. **Existing work is never overwritten.** Re-scanning a mailbox cannot regress
   a record's progress or demote a registered opportunity.
8. **Failures surface as `BLOCKED`, not tracebacks.**
9. **Thin evidence blocks a recommendation.** A direction is only recommended
   when at least two research questions verify; a score never disguises a
   one-source evidence base.
10. **Configured is not scanned.** `CONFIGURED` means credentials look usable
   but no read was attempted — never reported as success.

---

## Install

Requires Python 3.11+ (you're on 3.11.9).

```bash
cd ~/HACKFORGE
python -m pip install -e ".[gmail,research,dev]"
```

The core has **no required dependencies** — it runs on the standard library
alone. Optional extras add Gmail, better HTML extraction, and the test runner.

Verify:

```bash
python -m pytest -q
```

---

## Configuration

Copy `.env.example` to `.env` in this directory. Everything is environment
driven with safe defaults.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HACKFORGE_AUTONOMY_LEVEL` | `0` | 0 observe, 1 prepare, 2 build, 3 autonomous |
| `HACKFORGE_DATA_DIR` | `~/.hackforge` | State, approvals, activity log |
| `HACKFORGE_WORKSPACE_ROOT` | `~/Hackathons` | Where project workspaces are created |
| `HACKFORGE_DATABASE_URL` | *(empty)* | `sqlite:path` for SQLite; JSON otherwise |
| `GMAIL_CREDENTIALS_PATH` | *(unset)* | OAuth desktop client JSON |
| `GMAIL_TOKEN_PATH` | *(unset)* | Where the token is cached |
| `GMAIL_QUERY` | `newer_than:90d` | Gmail search query |
| `HACKFORGE_CONFIG_FILE` | *(unset)* | Optional JSON for research/deadline tuning |

### Gmail OAuth setup

1. Google Cloud Console → enable the **Gmail API**.
2. Credentials → OAuth client ID → **Desktop app**.
3. Download the JSON and set `GMAIL_CREDENTIALS_PATH`.
4. Set `GMAIL_TOKEN_PATH` to a writable path.

Scopes requested are **read-only** (`gmail.readonly`). The first `scan` opens a
browser for consent. Without credentials, `scan` exits with `BLOCKED` and an
actionable reason — it does not pretend to have read anything.

---

## Commands

```bash
hackforge status                    # config + source readiness
hackforge scan                      # Phase 1: read inbox, register opportunities
hackforge list                      # the registry
hackforge research <id>             # Phase 2: research from primary sources
hackforge strategize <id>           # Phase 3: candidates + recommendation
hackforge validate <id>             # Phase 4.5: research + synthesis + 14-section
                                   # report, then STOP and request approval
hackforge approve <id> --select "..." # record the human decision
hackforge workspace <id>            # Phase 4: create the project tree
hackforge report [id]               # daily report, or completion report
```

`validate` is the recommended entry point. It runs research, synthesis and
strategy scoring, writes the fourteen-section validation report, and then
stops. It never approves and never creates a workspace.

### Evidence states

| State | Meaning |
| --- | --- |
| `VERIFIED` | A retrieved document supports the claim; the exact passage is retained |
| `UNKNOWN` | Asked, but no source answered it |
| `FAILED` | Retrieval was attempted and did not succeed |

These are deliberately distinct. A failed fetch is not a silent gap, and
neither is ever rendered as text.

Without installing, use `PYTHONPATH=src python -m hackforge.cli.main <cmd>`.

### The happy path

```text
scan         ->  Global AI Hackathon 2026 registered
research     ->  5/5 findings verified, completeness 100%
strategize   ->  On-device tool (83.1/100), reasoning attached
approve      ->  Approved -> execution status: approved
workspace    ->  ~/Hackathons/Global-AI-Hackathon-2026/
report       ->  urgency, risk, blockers, next action
```

### Autonomy levels

| Level | Capability |
| --- | --- |
| 0 OBSERVE | Read, research, track, report. No filesystem writes. |
| 1 PREPARE | Create documents and workspace directories. |
| 2 BUILD | Create and modify code, run tests. |
| 3 AUTONOMOUS | Full cycle. External actions still require authorisation. |

The level is a hard gate, not a suggestion — level 1+ is required to create a
workspace, and level 0 is the default so nothing happens by surprise.

---

## Architecture

```text
hackforge/
├── config.py              env-driven configuration, urgency thresholds
├── adapters/
│   ├── sources.py         OpportunitySource protocol + RawMessage
│   └── gmail.py           read-only Gmail via OAuth
├── core/
│   ├── models.py          Opportunity, Timeline, findings, blockers
│   ├── registry.py        merge policy, activity trail
│   ├── store.py           Store protocol + JsonStore + SqliteStore
│   └── yamlout.py         dependency-free YAML for the state file
├── engines/
│   ├── detection.py       classification + field extraction
│   ├── research.py        tiered fetching, evidence, completeness
│   ├── synthesis.py       traceable answers or INSUFFICIENT EVIDENCE
│   ├── strategy.py        candidates, scoring, recommendation
│   ├── strategy_report.py fourteen-section validation report
│   ├── approval.py        human decision gate + history
│   ├── deadlines.py       urgency bands + accountability/risk
│   ├── reporting.py       daily and completion reports
│   └── workspace.py       project tree + HACKFORGE_STATE.md
└── cli/main.py            command surface
```

### Adding PostgreSQL later

Engines depend only on the `Store` protocol (`load_all`, `get`, `upsert`,
`delete`). Implement a `PostgresStore` against those four methods and point
`HACKFORGE_DATABASE_URL` at it. No engine or CLI code changes. A
`postgresql://` URL currently raises `NotImplementedError` with that instruction
rather than silently falling back.

### Adding another opportunity source

Implement `OpportunitySource` (one method: `fetch`) returning `RawMessage`
records. Detection, extraction, and everything downstream is source-agnostic —
that is what keeps this from being a hackathon-only tool.

---

## Project workspace layout

Created per opportunity after approval:

```text
<workspace_root>/<Opportunity Name>/
├── 00_SOURCE/         registration email, rules, source links
├── 01_RESEARCH/       research reports
├── 02_STRATEGY/       problem, solution, idea comparison, architecture
├── 03_BUILD/          project source
├── 04_TESTING/        test plan, results, known issues
├── 05_SUBMISSION/     README, demo script, submission description
└── HACKFORGE_STATE.md machine-readable source of truth
```

The workspace is refused if it already exists and is non-empty — existing work
is never clobbered. Document writes are confined to the workspace directory.

`HACKFORGE_STATE.md` is refreshed whenever the registry changes, so it cannot
drift from the stored record.

---

## Known limitations

- **Not validated against a real inbox or a real hackathon.** No Gmail
  credentials have been configured, so the section-34 end-to-end test has not
  been performed. Everything else is verified against fixtures or live HTTP.
- **Research is keyword-extractive, not comprehending.** It retrieves primary
  sources and surfaces relevant passages with citations. It does not yet reason
  across documents; a summarising model adapter is the natural next step and
  must populate the same evidence fields.
- **Candidate generation uses five structural archetypes**, not bespoke
  ideation. Scores are rule-based and reproducible, which is the point — but
  they measure complexity heuristics, not suitability. The validation report
  states this in sections 7, 9 and 13 rather than burying it.
- **Extraction is English-only** and assumes labelled dates ("Submission
  deadline: ...").
- **Phase 6 reporting is manual.** Nothing is scheduled yet; run `report` when
  you want it.
- **No build agent.** Phases 5 and 7 are not implemented.
- **An unexplained `skills/` directory sits inside this project.** It contains
  190 files of UI/design agent documentation and scripts, is referenced by no
  part of HACKFORGE, is not in git, and was not created by the HACKFORGE build.
  PROVENANCE: UNKNOWN. It was left untouched and nothing in it was executed.

---

## Next steps

1. Point `GMAIL_CREDENTIALS_PATH` at your OAuth client and run `hackforge scan`
   against real mail.
2. Take one genuine registered hackathon end to end (specification section 34)
   and check the research report against the actual rules by hand.
3. Only then wire an LLM adapter for research synthesis and idea generation.