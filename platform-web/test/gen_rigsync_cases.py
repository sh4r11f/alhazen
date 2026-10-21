"""Generate platform-web/test/rigsync_cases.json: owner-side rig config operations run through
alhazen.experiment_platform.rigsync, with the record (or the refusal) after each step. The Mana
host's JavaScript port (platform-web/src/rigsync.js) must give the same results;
platform-web/test/rigsync.test.mjs replays these cases.

    python platform-web/test/gen_rigsync_cases.py      (with alhazen importable)
"""
import json
from pathlib import Path

from alhazen.experiment_platform import rigsync as rs

MID = "mc_0123456789abcdef"
TEXT_A = "monitor:\n  width_px: 1920\n  height_px: 1080\n  width_cm: 53\n  distance_cm: 57\n  refresh_rate_hz: 60\n"
TEXT_B = TEXT_A.replace("57", "60")
TEXT_U = "# Überprüfung: ünïcode — ok\n" + TEXT_A


def machine_record():
    push = {
        "label": "Booth", "alhazen": "3.6.0", "default": "c1a2b3c4d5e", "defaultRevSeen": 0,
        "writes": [
            {"id": "c1a2b3c4d5e", "name": "booth", "text": TEXT_A, "sha256": rs.sha256_text(TEXT_A), "base": 0},
            {"id": "c2b3c4d5e6f", "name": "keyboard", "text": TEXT_B, "sha256": rs.sha256_text(TEXT_B), "base": 0},
        ],
        "running": {"c1a2b3c4d5e": {"name": "booth", "text": TEXT_A, "problem": None,
                                    "calibration": {"gamma": {"at": "2026-10-20T10:00:00"}}}},
    }
    rec, _ = rs.apply_push(None, MID, push, 1000)
    return rec


def with_conflict():
    rec = machine_record()
    rec, _ = rs.apply_push(rec, MID, {"conflicts": [{"id": "c1a2b3c4d5e", "text": TEXT_U, "sha256": rs.sha256_text(TEXT_U)}]}, 1500)
    return rec


SCENARIOS = {
    "edit, rename and create": (machine_record, [
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": TEXT_B, "base": 1, "basedOn": rs.sha256_text(TEXT_A), "now": 2000}),
        ("put", "c1a2b3c4d5e", {"name": "booth-2", "text": TEXT_B, "base": 2, "basedOn": None, "now": 2100}),
        ("put", "c9z8y7x6w5v", {"name": "monkey", "text": TEXT_U, "base": 0, "basedOn": "", "now": 2200}),
        ("put", "c9z8y7x6w5v", {"name": "monkey", "text": TEXT_A, "base": 0, "basedOn": None, "now": 2300}),
        ("put", "c9z8y7x6w5v", {"name": "keyboard", "text": TEXT_A, "base": 1, "basedOn": None, "now": 2400}),
        ("put", "c9z8y7x6w5v", {"name": " monkey2 ", "text": TEXT_A, "base": 1, "basedOn": None, "now": 2500}),
    ]),
    "refusals": (machine_record, [
        ("put", "c1a2b3c4d5e", {"name": "bad name", "text": TEXT_A, "base": 1, "basedOn": None, "now": 2000}),
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": "   ", "base": 1, "basedOn": None, "now": 2000}),
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": TEXT_A, "base": -1, "basedOn": None, "now": 2000}),
        ("put", "x", {"name": "booth", "text": TEXT_A, "base": 1, "basedOn": None, "now": 2000}),
        ("put", "c1a2b3c4d5e", {"name": "b" * 65, "text": TEXT_A, "base": 1, "basedOn": None, "now": 2000}),
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": {"repeat": "x", "times": 256 * 1024 + 1}, "base": 1, "basedOn": None, "now": 2000}),
        ("delete", "c1a2b3c4d5e", {"base": 1, "now": 2000}),
        ("delete", "nope-config", {"base": 0, "now": 2000}),
        ("default", "nope-config", {"base": 0, "now": 2000}),
        ("default", "c2b3c4d5e6f", {"base": 5, "now": 2000}),
        ("resolve", "c1a2b3c4d5e", {"keep": "machine", "now": 2000}),
    ]),
    "default and delete": (machine_record, [
        ("default", "c2b3c4d5e6f", {"base": 1, "now": 2000}),
        ("default", "c2b3c4d5e6f", {"base": 2, "now": 2100}),
        ("delete", "c1a2b3c4d5e", {"base": 1, "now": 2200}),
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": TEXT_A, "base": 2, "basedOn": None, "now": 2300}),
        ("delete", "c2b3c4d5e6f", {"base": 1, "now": 2400}),
    ]),
    "never ran: dropped at once": (machine_record, [
        ("delete", "c2b3c4d5e6f", {"base": 1, "now": 2000}),
    ]),
    "conflict kept by machine": (with_conflict, [
        ("resolve", "c1a2b3c4d5e", {"keep": "other", "now": 2000}),
        ("resolve", "c1a2b3c4d5e", {"keep": "machine", "now": 2000}),
        ("resolve", "c1a2b3c4d5e", {"keep": "machine", "now": 2100}),
    ]),
    "conflict kept by platform": (with_conflict, [
        ("resolve", "c1a2b3c4d5e", {"keep": "platform", "now": 2000}),
    ]),
    "not synced yet": (lambda: None, [
        ("put", "c1a2b3c4d5e", {"name": "booth", "text": TEXT_A, "base": 0, "basedOn": None, "now": 2000}),
        ("delete", "c1a2b3c4d5e", {"base": 0, "now": 2000}),
        ("default", "c1a2b3c4d5e", {"base": 0, "now": 2000}),
        ("resolve", "c1a2b3c4d5e", {"keep": "machine", "now": 2000}),
    ]),
}


def expand(value):
    """A large text is written {"repeat", "times"} in the cases file and expanded here and in the test."""
    return value["repeat"] * value["times"] if isinstance(value, dict) else value


def run(op, rec, cid, args):
    if op == "put":
        args = {**args, "text": expand(args["text"])}
        return rs.owner_put(rec, MID, cid, name=args["name"], text=args["text"], base=args["base"],
                            based_on=args["basedOn"], now=args["now"])
    if op == "delete":
        return rs.owner_delete(rec, MID, cid, base=args["base"], now=args["now"])
    if op == "default":
        return rs.owner_default(rec, MID, cid, base=args["base"], now=args["now"])
    return rs.owner_resolve(rec, MID, cid, keep=args["keep"], now=args["now"])


def main():
    cases = []
    for title, (start, steps) in SCENARIOS.items():
        rec = start()
        case = {"title": title, "mid": MID, "start": rec, "steps": []}
        for op, cid, args in steps:
            try:
                after = run(op, rec, cid, args)
                case["steps"].append({"op": op, "cid": cid, "args": args, "record": after, "summary": rs.summary(after)})
                rec = after
            except rs.RigSyncError as error:
                case["steps"].append({"op": op, "cid": cid, "args": args, "error": {"kind": error.kind, "message": str(error)}})
        cases.append(case)
    out = Path(__file__).with_name("rigsync_cases.json")
    out.write_text(json.dumps({"sha256": {t: rs.sha256_text(t) for t in ("", "abc", TEXT_U, "x" * 1000)}, "cases": cases},
                              indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
