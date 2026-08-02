"""Command line interface.

Every subcommand answers a question somebody asks during an incident, a review
or a migration. They are deliberately boring and scriptable: text by default,
``--json`` when a pipeline is reading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from .engine import Engine
from .errors import CanonError
from .loaders import load_decision_table, load_directory, load_yaml
from .registry import (DeployLedger, Manifest, diff_manifests, issue_receipt,
                       verify_receipt)
from .rules import RuleSet
from .shadow import ShadowRunner, load_cases_jsonl


def _load(path_str: str) -> RuleSet:
    path = Path(path_str)
    if path.is_dir():
        return load_directory(path)
    if path.suffix.lower() == ".csv":
        return load_decision_table(path)
    return load_yaml(path)


def _emit(payload: Any, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def _facts(path_str: str | None) -> dict[str, Any]:
    if not path_str:
        return {}
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    ruleset = _load(args.rules)
    lines = [
        f"{ruleset.id} v{ruleset.version}  {ruleset.content_hash[:12]}",
        f"  rules            {len(ruleset)}",
        f"  strata           {len(ruleset.strata)}",
        f"  fact roots       {', '.join(ruleset.roots)}",
        f"  planned fields   {ruleset.projection.leaf_count()}",
        f"  clients          {', '.join(ruleset.clients())}",
    ]
    for index, stratum in enumerate(ruleset.strata):
        lines.append(f"  stratum {index}: " +
                     ", ".join(rule.id for rule in stratum))
    _emit({"ok": True, "ruleset": ruleset.to_canonical()}, args.json,
          "\n".join(lines))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    engine = Engine(_load(args.rules))
    plan = engine.plan(client=args.client,
                       as_of=date.fromisoformat(args.as_of) if args.as_of else None)
    lines = [
        f"payload contract for {plan['ruleset']} v{plan['version']}"
        f"{' client=' + args.client if args.client else ''}",
        f"  {plan['rules_applicable']} of {plan['rules_total']} rules apply",
        f"  {plan['field_count']} fields across "
        f"{len(plan['projection'])} roots",
        "",
    ]
    for path in plan["paths"]:
        wanted = plan["requested_by"][path]
        lines.append(f"  {path:<48} {', '.join(wanted[:4])}"
                     + (" ..." if len(wanted) > 4 else ""))
    _emit(plan, args.json, "\n".join(lines))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ruleset = _load(args.rules)
    engine = Engine(ruleset, strict_facts=not args.permissive)
    decision = engine.evaluate(
        _facts(args.facts),
        key=json.loads(args.key) if args.key else {},
        client=args.client,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
    )
    _emit(decision.to_dict(include_traces=args.trace), args.json,
          decision.render(verbose=args.trace))
    return 0 if decision.ok else 2


def cmd_explain(args: argparse.Namespace) -> int:
    ruleset = _load(args.rules)
    decision = Engine(ruleset).evaluate(
        _facts(args.facts), client=args.client,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None)
    chain = decision.explain(args.code)
    if not chain:
        print(f"finding {args.code!r} was not raised on this transaction")
        considered = ", ".join(decision.rules_fired()) or "none"
        print(f"rules that did fire: {considered}")
        return 1
    lines = [f"why {args.code} was raised", ""]
    for step, entry in enumerate(chain):
        prefix = "  " * step
        lines.append(f"{prefix}{entry['rule_id']} v{entry['rule_version']} "
                     f"({entry['rule_hash']})")
        if entry["guard"]:
            lines.append(f"{prefix}  when: {entry['guard']}  -> "
                         f"{entry['guard_result']!r}")
        for path, value in entry["reads"].items():
            lines.append(f"{prefix}  read {path} = {value!r}")
        if entry["sets"]:
            for name, value in entry["sets"].items():
                lines.append(f"{prefix}  set derived.{name} = {value!r}")
    _emit(chain, args.json, "\n".join(lines))
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    ruleset = _load(args.rules)
    manifest = Manifest.of(ruleset, source_revision=args.revision or "",
                           notes=args.notes or "")
    if args.out:
        manifest.save(args.out)
    lines = [
        f"manifest for {manifest.ruleset_id} v{manifest.ruleset_version}",
        f"  merkle root  {manifest.root}",
        f"  rules        {len(manifest.entries)}",
        f"  created      {manifest.created_at}",
    ]
    if args.out:
        lines.append(f"  written to   {args.out}")
    _emit(manifest.to_dict(), args.json, "\n".join(lines))
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    manifest = Manifest.load(args.manifest)
    secret = args.secret.encode() if args.secret else None
    ledger_path = Path(args.ledger)
    ledger = (DeployLedger.load(ledger_path, secret=secret)
              if ledger_path.exists() else DeployLedger(secret=secret))
    record = ledger.append(manifest, environment=args.env,
                           deployed_by=args.by or "", client=args.client or "*")
    ledger.save(ledger_path)
    _emit(record.to_dict(), args.json,
          f"recorded deployment #{record.seq} of "
          f"{manifest.ruleset_id} v{manifest.ruleset_version} to {args.env}\n"
          f"  entry hash {record.entry_hash[:16]}\n"
          f"  prev hash  {record.prev_hash[:16]}")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    secret = args.secret.encode() if args.secret else None
    ledger = DeployLedger.load(args.ledger, secret=secret)
    if args.action == "verify":
        problems = ledger.verify()
        _emit({"intact": not problems, "problems": problems}, args.json,
              "ledger is intact" if not problems
              else "LEDGER FAILED VERIFICATION\n  " + "\n  ".join(problems))
        return 0 if not problems else 3
    if args.action == "history":
        rows = [{"seq": r.seq, "env": r.environment, "at": r.deployed_at,
                 "by": r.deployed_by, "version": r.manifest.ruleset_version,
                 "root": r.manifest.root[:12]}
                for r in ledger.history(args.env)]
        text = "\n".join(
            f"  #{row['seq']:<4} {row['at']}  {row['env']:<10} "
            f"v{row['version']:<12} {row['root']}  {row['by']}" for row in rows)
        _emit(rows, args.json, f"deployment history\n{text}")
        return 0
    if args.action == "blame":
        if not args.rule:
            print("--rule is required for blame", file=sys.stderr)
            return 1
        rows = ledger.blame(args.rule, args.env)
        if not rows:
            _emit([], args.json,
                  f"rule {args.rule} has never appeared in this ledger")
            return 1
        text = "\n".join(
            f"  #{row['seq']:<4} {row['deployed_at']}  {row['environment']:<10} "
            f"{row['change']:<9} {row['from_hash'] or '-'} -> "
            f"{row['to_hash'] or '-'}  by {row['deployed_by'] or 'unknown'}"
            for row in rows)
        _emit(rows, args.json,
              f"every deployment that changed {args.rule}\n{text}")
        return 0
    print(f"unknown ledger action {args.action}", file=sys.stderr)
    return 1


def cmd_diff(args: argparse.Namespace) -> int:
    before = Manifest.load(args.before)
    after = Manifest.load(args.after)
    result = diff_manifests(before, after)
    lines = [
        f"{result['from']['version']} ({result['from']['root']}) -> "
        f"{result['to']['version']} ({result['to']['root']})",
        f"  added     {len(result['added'])}",
        f"  removed   {len(result['removed'])}",
        f"  changed   {len(result['changed'])}",
        f"  unchanged {result['unchanged']}",
    ]
    for change in result["changed"]:
        flag = "" if change["version_bumped"] else "   <- content changed but version did not"
        lines.append(f"  {change['rule_id']:<20} "
                     f"v{change['from_version']} -> v{change['to_version']}  "
                     f"{change['from_hash']} -> {change['to_hash']}{flag}")
    _emit(result, args.json, "\n".join(lines))
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    engine = Engine(_load(args.rules), strict_facts=not args.permissive)
    cases = load_cases_jsonl(args.cases)
    runner = ShadowRunner(engine, sample_rate=args.sample)
    report = runner.run(cases)
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=2,
                                             default=str), encoding="utf-8")
    _emit(report.to_dict(), args.json, report.render())
    return 0 if report.agreement >= args.threshold else 4


def cmd_whatif(args: argparse.Namespace) -> int:
    from .proposal import load_proposal
    from .whatif import WhatIf

    baseline = _load(args.rules)

    if args.proposal:
        proposal = load_proposal(args.proposal)
        candidate = proposal.apply(baseline)
        if not args.json:
            print(proposal.render())
            print()
    elif args.candidate:
        candidate = _load(args.candidate)
    else:
        print("give either --proposal or --candidate", file=sys.stderr)
        return 1

    # Captured payloads may predate the current contract, so replay is
    # permissive about unplanned reads by default.
    runner = WhatIf(baseline, candidate, strict_facts=bool(args.strict))

    if not args.cases:
        # No traffic supplied: show what changed, and say plainly that nobody
        # can yet say what it does.
        result = runner.diff
        print(f"{len(result['added'])} added, {len(result['removed'])} removed, "
              f"{len(result['changed'])} changed, {result['unchanged']} unchanged")
        for change in result["changed"]:
            flag = "" if change["version_bumped"] else "   <- no version bump"
            print(f"  {change['rule_id']:<14} {change['from_hash']} -> "
                  f"{change['to_hash']}{flag}")
        for entry in result["added"]:
            print(f"  {entry['rule_id']:<14} added")
        for entry in result["removed"]:
            print(f"  {entry['rule_id']:<14} removed")
        print()
        print("no --cases given, so the impact of these changes is unknown. "
              "Replay them against captured traffic before signing anything off.")
        return 0

    cases = load_cases_jsonl(args.cases)
    if args.limit:
        cases = cases[:args.limit]
    report = runner.run(cases)

    if args.out:
        Path(args.out).write_text(
            json.dumps(report.to_dict(sample_limit=args.samples), indent=2,
                       default=str), encoding="utf-8")

    _emit(report.to_dict(sample_limit=args.samples), args.json,
          report.render(sample_limit=args.samples))

    if args.max_flip_rate is not None and report.flip_rate > args.max_flip_rate:
        print(f"\nflip rate {report.flip_rate:.2%} exceeds the "
              f"{args.max_flip_rate:.2%} budget", file=sys.stderr)
        return 5
    return 0


def cmd_import_odm(args: argparse.Namespace) -> int:
    from .odm_import import Verbalisation, import_bal_file
    from .loaders import dump_yaml_dict

    result = import_bal_file(args.source, Verbalisation.load(args.verbalisation),
                             ruleset_id=args.ruleset_id)
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == ".json":
            out.write_text(json.dumps(result.ruleset, indent=2), encoding="utf-8")
        else:
            try:
                import yaml  # type: ignore

                out.write_text(yaml.safe_dump(result.ruleset, sort_keys=False),
                               encoding="utf-8")
            except ImportError:
                out.with_suffix(".json").write_text(
                    json.dumps(result.ruleset, indent=2), encoding="utf-8")
    _emit({"converted": result.converted, "needs_review": result.needs_review,
           "warnings": result.warnings, "coverage": result.coverage},
          args.json, result.render())
    return 0


def cmd_receipt(args: argparse.Namespace) -> int:
    ruleset = _load(args.rules)
    manifest = Manifest.of(ruleset)
    decision = Engine(ruleset).evaluate(
        _facts(args.facts), client=args.client,
        key=json.loads(args.key) if args.key else {})
    secret = args.secret.encode() if args.secret else None
    receipt = issue_receipt(decision, manifest, secret)
    problems = verify_receipt(receipt, decision=decision, manifest=manifest,
                              secret=secret)
    _emit({"receipt": receipt, "problems": problems}, args.json,
          json.dumps(receipt, indent=2) +
          ("\n\nverified" if not problems else "\n\nPROBLEMS: " + str(problems)))
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canon",
        description="Stateless rules engine with generated payload contracts, "
                    "rule level provenance and tamper evident deployments.")

    # --json belongs to every subcommand rather than to the top level. argparse
    # hands everything after the subcommand name to the subparser, so a
    # top level flag written after it (canon plan rules.yaml --json) is an
    # error, which is exactly how people type it. Sharing one parent parser
    # gives every subcommand the flag in the position people expect.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="machine readable output")

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text, parents=[common])
        sub.set_defaults(func=handler)
        return sub

    validate = add("validate", cmd_validate, "load and check a ruleset")
    validate.add_argument("rules")

    plan = add("plan", cmd_plan, "show the payload contract a ruleset needs")
    plan.add_argument("rules")
    plan.add_argument("--client")
    plan.add_argument("--as-of", dest="as_of")

    run = add("run", cmd_run, "evaluate a ruleset against a fact document")
    run.add_argument("rules")
    run.add_argument("--facts", required=True)
    run.add_argument("--client")
    run.add_argument("--as-of", dest="as_of")
    run.add_argument("--key")
    run.add_argument("--trace", action="store_true")
    run.add_argument("--permissive", action="store_true",
                     help="allow reads that static analysis did not predict")

    explain = add("explain", cmd_explain, "show why a finding was raised")
    explain.add_argument("rules")
    explain.add_argument("--facts", required=True)
    explain.add_argument("--code", required=True)
    explain.add_argument("--client")
    explain.add_argument("--as-of", dest="as_of")

    manifest = add("manifest", cmd_manifest, "build a deployment manifest")
    manifest.add_argument("rules")
    manifest.add_argument("--out")
    manifest.add_argument("--revision")
    manifest.add_argument("--notes")

    deploy = add("deploy", cmd_deploy, "record a deployment in the ledger")
    deploy.add_argument("manifest")
    deploy.add_argument("--env", required=True)
    deploy.add_argument("--ledger", default="deploy-ledger.json")
    deploy.add_argument("--by")
    deploy.add_argument("--client")
    deploy.add_argument("--secret")

    ledger = add("ledger", cmd_ledger, "verify, list or blame the deploy ledger")
    ledger.add_argument("action", choices=["verify", "history", "blame"])
    ledger.add_argument("--ledger", default="deploy-ledger.json")
    ledger.add_argument("--rule")
    ledger.add_argument("--env")
    ledger.add_argument("--secret")

    diff = add("diff", cmd_diff, "diff two manifests at rule granularity")
    diff.add_argument("before")
    diff.add_argument("after")

    shadow = add("shadow", cmd_shadow, "run against captured traffic and diff")
    shadow.add_argument("rules")
    shadow.add_argument("--cases", required=True)
    shadow.add_argument("--out")
    shadow.add_argument("--sample", type=float, default=1.0)
    shadow.add_argument("--threshold", type=float, default=1.0)
    shadow.add_argument("--permissive", action="store_true")

    whatif = add("whatif", cmd_whatif,
                 "replay captured traffic against a proposed rule change")
    whatif.add_argument("rules", help="the baseline ruleset")
    whatif.add_argument("--proposal", help="a proposal overlay to apply")
    whatif.add_argument("--candidate", help="a whole candidate ruleset instead")
    whatif.add_argument("--cases", help="captured transactions, JSON Lines")
    whatif.add_argument("--limit", type=int, help="replay only the first N cases")
    whatif.add_argument("--out", help="write the full report as JSON")
    whatif.add_argument("--samples", type=int, default=8,
                        help="how many moved decisions to show")
    whatif.add_argument("--max-flip-rate", dest="max_flip_rate", type=float,
                        help="exit non zero if more than this fraction moves")
    whatif.add_argument("--strict", action="store_true",
                        help="refuse reads the contract did not predict")

    odm = add("import-odm", cmd_import_odm, "convert an ODM BAL export")
    odm.add_argument("source")
    odm.add_argument("--verbalisation", required=True)
    odm.add_argument("--out")
    odm.add_argument("--ruleset-id", dest="ruleset_id")

    receipt = add("receipt", cmd_receipt, "evaluate and issue a signed receipt")
    receipt.add_argument("rules")
    receipt.add_argument("--facts", required=True)
    receipt.add_argument("--client")
    receipt.add_argument("--key")
    receipt.add_argument("--secret")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CanonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
