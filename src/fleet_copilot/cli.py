"""Command line interface.

  fleet-copilot chat     --tenant acme-001 --user alice@acme.example      interactive, asks you to approve actions
  fleet-copilot ask      --tenant acme-001 "Which devices are low on disk?"
  fleet-copilot insights --tenant acme-001                                 detectors only, no LLM, no API key
  fleet-copilot audit                                                      verify the audit log hash chain
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


def _copilot(args):
    from .agent.graph import Copilot
    from .agent.llm import LLM
    return Copilot(llm=LLM(mode=args.mode) if args.mode else LLM())


def _show_plan(plan):
    if not plan:
        return
    steps = "\n".join(f"  {i + 1}. {s['tool']} - {s['purpose']}" for i, s in enumerate(plan.get("steps", [])))
    console.print(Panel(f"[b]{plan.get('intent')}[/b]: {plan.get('restated_question')}\n{steps}", title="Plan", style="cyan"))


def _show_proposals(props):
    t = Table(title="Proposed actions (nothing has run yet)")
    for c in ("id", "action", "target", "details"):
        t.add_column(c)
    for p in props:
        if p["status"] != "pending_approval":
            continue
        a = p["args"]
        target = a.get("device_id") or a.get("employee_id")
        detail = a.get("spec") or a.get("check_id") or a.get("reason") or a.get("message", "")[:60]
        t.add_row(p["proposal_id"], p["tool"], target, f"{detail}\nevidence: {', '.join(a['evidence'])}")
    console.print(t)


def _decide(props, user, auto: str | None):
    decisions = {}
    for p in props:
        if p["status"] != "pending_approval":
            continue
        if auto == "all":
            ok = True
        elif auto == "none":
            ok = False
        else:
            ok = console.input(f"Approve {p['proposal_id']} ({p['tool']})? [y/N] ").strip().lower() == "y"
        decisions[p["proposal_id"]] = {"approved": ok, "approver": user}
    return decisions


def _run_turn(cp, session, q, history, auto=None, as_json=False):
    r = cp.ask(session, q, history=history)
    if not as_json:
        _show_plan(r.plan)
    if r.status == "awaiting_approval":
        if not as_json:
            console.print(Markdown(r.answer))
            _show_proposals(r.proposals)
        r = cp.resume(r.thread_id, _decide(r.proposals, session.user_id, auto))
    if as_json:
        print(json.dumps({"answer": r.answer, "plan": r.plan, "proposals": r.proposals, "executed": r.executed,
                          "verification": r.verification, "tool_calls": r.tool_log}, indent=1, default=str))
    else:
        console.print(Panel(Markdown(r.answer), title="Answer", style="green"))
        v = r.verification or {}
        console.print(f"[dim]grounding: {'pass' if v.get('ok') else 'see warnings'} | citations: {len(v.get('citations', []))} | "
                      f"tool calls: {len(r.tool_log)} | LLM calls so far: {cp.llm.usage.calls} | est. cost ${cp.llm.usage.cost_usd()}[/dim]")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet-copilot")
    ap.add_argument("--mode", choices=["live", "record", "replay", "auto"], help="LLM mode (default: env FLEET_LLM_MODE or auto)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("chat", "ask"):
        p = sub.add_parser(name)
        p.add_argument("--tenant", required=True)
        p.add_argument("--user", default="it-admin")
        p.add_argument("--role", default="it_admin", choices=["it_admin", "viewer"])
        if name == "ask":
            p.add_argument("question")
            p.add_argument("--approve", choices=["ask", "all", "none"], default="ask")
            p.add_argument("--json", action="store_true")
    p = sub.add_parser("insights")
    p.add_argument("--tenant", required=True)
    p.add_argument("--kinds", nargs="*")
    sub.add_parser("audit")
    args = ap.parse_args(argv)

    if args.cmd == "insights":
        from .analytics.detectors import run_detectors
        from .analytics.policy import Policy
        from .data.store import TelemetryStore
        t = Table(title=f"Insights for {args.tenant}")
        for c in ("severity", "finding", "explanation"):
            t.add_column(c)
        for f in run_detectors(TelemetryStore.from_jsonl().for_tenant(args.tenant), Policy.load(), args.kinds):
            t.add_row(f.severity, f.finding_id, f.explanation)
        console.print(t)
        return 0

    if args.cmd == "audit":
        from .agent.graph import ROOT
        from .guardrails.audit import AuditLog
        log = AuditLog(ROOT / "var" / "audit.jsonl")
        ok, bad = log.verify_chain()
        console.print(f"{len(log.entries())} entries, chain {'intact' if ok else f'BROKEN at entry {bad}'}")
        return 0 if ok else 1

    from .tools.context import Session
    session = Session(args.tenant, args.user, args.role)
    cp = _copilot(args)
    if args.cmd == "ask":
        _run_turn(cp, session, args.question, [], auto=None if args.approve == "ask" else args.approve, as_json=args.json)
        return 0

    console.print(f"Fleet Copilot for [b]{args.tenant}[/b] as {args.user} ({args.role}). Type 'exit' to quit.")
    history: list[dict] = []
    while True:
        try:
            q = console.input("[b]> [/b]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("exit", "quit"):
            break
        if not q:
            continue
        r = _run_turn(cp, session, q, history)
        # Only the question and final answer carry over. Evidence must be re-fetched each turn.
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": r.answer}]
        history = history[-8:]
    return 0


if __name__ == "__main__":
    sys.exit(main())
