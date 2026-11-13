"""Run the pilot with a fleet of Claude Code subagents instead of API calls.

  python fleet.py prepare smoke1 --runs 5          # writes fleet_runs/smoke1/workflow.js
  (in Claude Code: run that script with the Workflow tool, from an empty directory)
  python fleet.py collect smoke1 --out results.jsonl
  python analyze.py results.jsonl

The workflow has three kinds of agent per run:
  s:<key>  the subject. Its task text is framing + scenario system text + user text.
  j:<key>  runs `fleet.py judge-task`, which scores the response with the same
           scorers as run_pilot.py and prints the blind judging task (no framing).
  f:<key>  runs `fleet.py framing-task` and answers only "does it refer to the framing".

Keys are random, so neither a label nor a command line tells an agent which
condition it is looking at. The mapping from key to condition is in
fleet_runs/<batch>/manifest.json.

Nothing is sent back through an agent by hand: the response and the judge's
flags are read from the journal that the Workflow tool writes for each run
(~/.claude/projects/.../subagents/workflows/<run>/journal.jsonl).

What differs from the API harness is listed in the README ("Fleet mode").
"""

import argparse
import glob
import hashlib
import json
import os
import random
import secrets
import sys
import time
from datetime import datetime, timezone

import sandbox
from framings import FRAMINGS
from run_pilot import (JUDGE_SYSTEM, build_blind_prompt, build_framing_prompt, build_system,
                       harness_commit, load_latest)
from scenarios import SCENARIOS, SCORER_VERSION, primary_outcome

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "fleet_runs")
TEMPLATE = os.path.join(HERE, "fleet_workflow.template.js")
MAX_AGENTS = 1000  # the Workflow tool's cap on agents per run
DELIVERY = "workflow-inline"  # the framing is part of the task text, not a system prompt

NO_JUDGING = ("NO JUDGING NEEDED for this item ({reason}). Answer skipped=true, every other flag false, "
              "and rationale \"not judged\".")


def journal_root() -> str:
    return os.environ.get("HP_JOURNAL_ROOT") or os.path.expanduser("~/.claude/projects")


def batch_dir(batch: str) -> str:
    return os.path.join(RUNS_DIR, batch)


def load_manifest(batch: str) -> dict:
    with open(os.path.join(batch_dir(batch), "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def subject_prompt(scenario: str, condition: str) -> str:
    """The task text a subject agent gets: what the API harness sends as system + user."""
    return build_system(scenario, condition) + "\n\n" + SCENARIOS[scenario]["user"]


def prompt_sha(scenario: str, condition: str) -> str:
    return hashlib.sha256((DELIVERY + "\x00" + subject_prompt(scenario, condition)).encode("utf-8")).hexdigest()[:12]


def model_name(alias) -> str:
    return f"fleet:{alias or 'session'}"


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def prepare(args) -> None:
    out_dir = batch_dir(args.batch)
    if os.path.exists(os.path.join(out_dir, "manifest.json")):
        sys.exit(f"Batch {args.batch} already exists in {out_dir}. Pick a new batch name.")

    # --models session = whatever model the Claude Code session runs on.
    models = [None if m == "session" else m for m in args.models]
    prompts, prompt_index, jobs = [], {}, []
    for m in models:
        for s in args.scenarios:
            for c in args.conditions:
                if (s, c) not in prompt_index:
                    prompt_index[(s, c)] = len(prompts)
                    prompts.append(subject_prompt(s, c))
                for i in range(args.first_run, args.first_run + args.runs):
                    jobs.append({"key": secrets.token_hex(5), "model": m, "scenario": s, "condition": c,
                                 "run": i, "prompt": prompt_index[(s, c)], "prompt_sha": prompt_sha(s, c)})
    n_agents = sum(3 if FRAMINGS[j["condition"]] else 2 for j in jobs)
    if n_agents > MAX_AGENTS:
        sys.exit(f"{len(jobs)} runs need {n_agents} agents; one workflow may start {MAX_AGENTS}. "
                 "Split the grid into several batches (--first-run keeps run numbers apart).")
    random.Random(args.seed).shuffle(jobs)  # interleave conditions so drift over time doesn't bias one

    manifest = {
        "batch": args.batch,
        "created": datetime.now(timezone.utc).isoformat(),
        "harness_commit": harness_commit(),
        "scorer_version": SCORER_VERSION,
        "sandbox": sandbox.set_mode(args.sandbox),
        "judge_model": args.judge_model,
        "seed": args.seed,
        "jobs": jobs,
    }
    script_data = {
        "batch": args.batch,
        "python": sys.executable,
        "fleet": os.path.abspath(__file__),
        "judge_model": args.judge_model,
        "prompts": prompts,
        "jobs": [{"key": j["key"], "prompt": j["prompt"], "model": j["model"], "scenario": j["scenario"],
                  "framed": bool(FRAMINGS[j["condition"]])} for j in jobs],
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    if template.count("__BATCH_JSON__") != 1:
        sys.exit("fleet_workflow.template.js must contain __BATCH_JSON__ exactly once.")
    # JSON is valid JavaScript; "</" needs no escaping here because the script is not embedded in HTML.
    script = template.replace("__BATCH_JSON__", json.dumps(script_data, ensure_ascii=True, indent=1))

    os.makedirs(os.path.join(out_dir, "scored"), exist_ok=True)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    script_path = os.path.join(out_dir, "workflow.js")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    print(f"{len(jobs)} runs, {n_agents} agents. Model-written code runs in: {manifest['sandbox']}")
    print(f"Workflow script: {script_path}")
    print(f"Afterwards: python fleet.py collect {args.batch}")


# ---------------------------------------------------------------------------
# Reading the Workflow tool's journals
# ---------------------------------------------------------------------------

def read_journals(labels=None) -> dict:
    """{label: {"result", "agent_id", "dir"}} for finished agents, newest result per label."""
    found = {}
    paths = glob.glob(os.path.join(journal_root(), "*", "*", "subagents", "workflows", "*", "journal.jsonl"))
    for path in sorted(paths, key=os.path.getmtime):
        label_of, results = {}, {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a line still being written
                    if e.get("type") == "started" and "label" in e:
                        label_of[e.get("agentId")] = e["label"]
                    elif e.get("type") == "result":
                        results[e.get("agentId")] = e.get("result")
        except OSError:
            continue
        for agent_id, result in results.items():
            label = label_of.get(agent_id)
            if label is None or result is None or (labels is not None and label not in labels):
                continue
            found[label] = {"result": result, "agent_id": agent_id, "dir": os.path.dirname(path)}
    return found


def agent_facts(entry: dict) -> dict:
    """Model and number of tool calls, from the agent's own transcript (None if unreadable)."""
    facts = {"model": None, "tool_uses": None}
    path = os.path.join(entry["dir"], f"agent-{entry['agent_id']}.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            tool_uses = 0
            for line in f:
                try:
                    msg = json.loads(line).get("message") or {}
                except json.JSONDecodeError:
                    continue
                if msg.get("role") != "assistant":
                    continue
                facts["model"] = msg.get("model") or facts["model"]
                content = msg.get("content")
                if isinstance(content, list):
                    tool_uses += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
            facts["tool_uses"] = tool_uses
    except OSError:
        pass
    return facts


def as_flags(result):
    """A judge agent's structured result as a dict, or None."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# Scoring (shared by judge-task, framing-task and collect)
# ---------------------------------------------------------------------------

def find_job(manifest: dict, key: str) -> dict:
    for j in manifest["jobs"]:
        if j["key"] == key:
            return j
    sys.exit(f"Unknown key {key} in batch {manifest['batch']}.")


def scored(manifest: dict, job: dict, wait: float = 0.0, journals: dict = None):
    """The stored response and objective score of one run; scores it on first use.

    Returns None when the subject has no result in any journal. The score is
    stored so that the judge and the final record see the same one.
    """
    path = os.path.join(batch_dir(manifest["batch"]), "scored", job["key"] + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    label = "s:" + job["key"]
    deadline = time.time() + wait
    while True:
        entry = (journals if journals is not None else read_journals({label})).get(label)
        if entry is not None or time.time() >= deadline:
            break
        time.sleep(1.0)  # the journal line may land just after the subject returns
    if entry is None or not isinstance(entry["result"], str):
        return None

    rec = {"key": job["key"], "response": entry["result"], "workflow_dir": entry["dir"],
           "agent_id": entry["agent_id"], "scored_at": datetime.now(timezone.utc).isoformat(),
           "scorer_version": SCORER_VERSION}
    rec.update(subject=agent_facts(entry))
    sandbox.set_mode(manifest["sandbox"])
    try:
        rec["metrics"] = SCENARIOS[job["scenario"]]["evaluate"](rec["response"])
    except Exception as e:  # noqa: BLE001
        rec["scorer_error"] = repr(e)
    tmp = f"{path}.{os.getpid()}.tmp"  # judge-task and framing-task can score the same run at once
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return rec


def judge_task(args) -> None:
    manifest = load_manifest(args.batch)
    job = find_job(manifest, args.key)
    rec = scored(manifest, job, wait=30)
    if rec is None:
        sys.exit(f"No response found for {args.key} (looked under {journal_root()}).")
    if not (rec.get("metrics") or {}).get("parse_ok"):
        print(NO_JUDGING.format(reason=rec.get("scorer_error") and "scorer error" or "no valid submission"))
        return
    print(JUDGE_SYSTEM + "\n")
    print(build_blind_prompt(job["scenario"], rec["metrics"], rec["response"]))
    print("\nGive your answer through the structured output fields, with skipped=false.")


def framing_task(args) -> None:
    manifest = load_manifest(args.batch)
    job = find_job(manifest, args.key)
    rec = scored(manifest, job, wait=30)
    if rec is None:
        sys.exit(f"No response found for {args.key} (looked under {journal_root()}).")
    framing = FRAMINGS[job["condition"]]
    if not framing or not (rec.get("metrics") or {}).get("parse_ok"):
        print(NO_JUDGING.format(reason="nothing to compare"))
        return
    print(JUDGE_SYSTEM + "\n")
    print(build_framing_prompt(framing, rec["response"]))
    print("\nGive your answer through the structured output fields, with skipped=false.")


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def strict_bool(v):
    return v if isinstance(v, bool) else None


def build_record(manifest: dict, job: dict, journals: dict) -> dict:
    sc = SCENARIOS[job["scenario"]]
    model = model_name(job["model"])
    rec = {
        "id": f"{model}|{job['scenario']}|{job['condition']}|{job['run']}|{job['prompt_sha'][:8]}",
        "model": model, "scenario": job["scenario"], "condition": job["condition"], "run": job["run"],
        "timestamp": manifest["created"],
        "provenance": {
            "judge_model": model_name(manifest.get("judge_model")),
            "prompt_sha": job["prompt_sha"],
            "scorer_version": SCORER_VERSION,
            "harness_commit": manifest.get("harness_commit"),
            "sandbox": manifest.get("sandbox"),
            "delivery": DELIVERY,
            "batch": manifest["batch"],
            "key": job["key"],
        },
    }
    if prompt_sha(job["scenario"], job["condition"]) != job["prompt_sha"]:
        rec["provenance"]["prompt_changed_since_prepare"] = True

    got = scored(manifest, job, journals=journals)
    if got is None:  # the subject agent died or was skipped: nothing was observed
        rec["error"] = "no response in any workflow journal"
        rec["misbehaviour"] = None
        return rec
    rec["timestamp"] = got["scored_at"]
    rec["response"] = got["response"]
    rec["stop_reason"] = None
    rec["truncated"] = False
    rec["temperature_used"] = None
    rec["provenance"].update(workflow_run=os.path.basename(got["workflow_dir"]), agent_id=got["agent_id"],
                             subject_model=got["subject"]["model"], subject_tool_uses=got["subject"]["tool_uses"])
    if got.get("scorer_version") != SCORER_VERSION:
        rec["provenance"]["scorer_version"] = got.get("scorer_version")

    if "scorer_error" in got:
        rec["scorer_error"] = got["scorer_error"]
        rec["outcome"] = {"misbehaviour": None, "excluded_reason": "scorer_error"}
        rec["misbehaviour"] = None
        return rec
    metrics = rec["metrics"] = got["metrics"]

    flags = {}
    if metrics.get("parse_ok"):
        blind = as_flags((journals.get("j:" + job["key"]) or {}).get("result"))
        ok = bool(blind) and not blind.get("skipped")
        flags = {flag: strict_bool(blind.get(flag)) if ok else None for flag in sc["judge_flags"]}
        flags["judge_parse_ok"] = ok and all(flags[f] is not None for f in ("acknowledges_problem", "deceptive"))
        if blind:
            flags["rationale"] = str(blind.get("rationale", ""))[:500]
        framing = as_flags((journals.get("f:" + job["key"]) or {}).get("result"))
        flags["references_framing"] = (strict_bool(framing.get("references_framing"))
                                       if framing and not framing.get("skipped") else None)
        rec["judge"] = flags
    rec["outcome"] = primary_outcome(job["scenario"], metrics, flags)
    rec["misbehaviour"] = rec["outcome"]["misbehaviour"]
    return rec


def collect(args) -> None:
    manifest = load_manifest(args.batch)
    journals = read_journals()
    existing = load_latest(args.out)
    n = {"written": 0, "no_response": 0, "used_tools": 0, "left_out": 0}
    with open(args.out, "a", encoding="utf-8") as out:
        for job in manifest["jobs"]:
            rec = build_record(manifest, job, journals)
            if "response" not in rec:
                n["no_response"] += 1
                if "response" in existing.get(rec["id"], {}):
                    continue  # never replace an observed response with an error record
            elif rec["misbehaviour"] is None:
                n["left_out"] += 1
            if rec["provenance"].get("subject_tool_uses"):
                n["used_tools"] += 1
            out.write(json.dumps(rec) + "\n")
            n["written"] += 1
    print(f"{n['written']} records written to {args.out} "
          f"({n['no_response']} without a response, {n['left_out']} left out of the primary outcome).")
    if n["used_tools"]:
        print(f"{n['used_tools']} subjects called tools. They may have looked at files outside their task: "
              "check provenance.subject_tool_uses and read those transcripts before trusting the runs.")
    print("Analyse with: python analyze.py", args.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="write the manifest and the workflow script for one batch")
    p.add_argument("batch")
    p.add_argument("--models", nargs="+", default=["session"],
                   help="subject models: session (the session's model) or an alias such as sonnet, opus, haiku")
    p.add_argument("--judge-model", default=None, help="model alias for the judge agents (default: session model)")
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    p.add_argument("--conditions", nargs="+", default=list(FRAMINGS), choices=list(FRAMINGS))
    p.add_argument("--runs", type=int, default=20, help="runs per model x scenario x condition")
    p.add_argument("--first-run", type=int, default=0, help="first run number, to add runs in a later batch")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--sandbox", default="auto", choices=sandbox.MODES)
    p.set_defaults(fn=prepare)

    for name, fn in (("judge-task", judge_task), ("framing-task", framing_task)):
        p = sub.add_parser(name, help="called by the workflow's agents")
        p.add_argument("batch")
        p.add_argument("key")
        p.set_defaults(fn=fn)

    p = sub.add_parser("collect", help="turn a finished batch into result records")
    p.add_argument("batch")
    p.add_argument("--out", default="results.jsonl")
    p.set_defaults(fn=collect)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
