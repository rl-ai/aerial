"""CLI tests: call hub_cli.main() directly with argv against the real app."""
import json

import pytest

from hub_client import HubClient
from hub_cli import main


@pytest.fixture()
def cli(hub_base_url):
    def run(*argv) -> tuple[int, str, str]:
        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), client=HubClient(base_url=hub_base_url))
        return code, out.getvalue(), err.getvalue()
    return run


def _new(cli, title="CLI ticket", **flags) -> str:
    argv = ["new", title]
    for k, v in flags.items():
        key = "--" + k.replace("_", "-")
        if isinstance(v, list):
            for item in v:
                argv += [key, item]
        elif v is not None:
            argv += [key, str(v)]
    code, out, err = cli(*argv)
    assert code == 0, err
    return out.split()[0]


# -- new / get / ls ----------------------------------------------------------

def test_new_and_get_one_line(cli):
    tid = _new(cli, "CLI one-liner", owner="owner")
    code, out, err = cli("get", tid)
    assert code == 0, err
    line = out.strip()
    assert line.startswith(tid)
    assert "draft" in line and "CLI one-liner" in line and "[owner]" in line


def test_get_json_parses(cli):
    tid = _new(cli, "CLI json get")
    code, out, err = cli("get", tid, "--json")
    assert code == 0, err
    t = json.loads(out)
    assert t["id"] == tid and t["title"] == "CLI json get"


def test_ls_filters_and_json(cli):
    tid = _new(cli, "CLI ls alpha", tag=["uixa"])
    cli("set", tid, "--status", "open")
    code, out, _ = cli("ls", "--status", "open")
    assert code == 0 and tid in out
    code, out, _ = cli("ls", "--tag", "uixa")
    assert code == 0 and tid in out
    code, out, _ = cli("ls", "--status", "done")
    assert code == 0 and tid not in out
    code, out, _ = cli("ls", "--json")
    assert code == 0
    assert any(t["id"] == tid for t in json.loads(out))


def test_ls_bad_status_exit_1(cli):
    code, _, err = cli("ls", "--status", "bogus")
    assert code == 1
    assert "bad_request" in err


# -- set / comment / event ------------------------------------------------------

def test_set_status_and_illegal_transition_exit_1(cli):
    tid = _new(cli, "CLI set me")
    code, out, err = cli("set", tid, "--status", "open", "--actor", "owner")
    assert code == 0 and "open" in out, err
    code, out, err = cli("set", tid, "--status", "draft")  # illegal
    assert code == 1
    assert "bad_request" in err


def test_set_requires_a_field(cli):
    tid = _new(cli, "CLI no field")
    code, _, err = cli("set", tid)
    assert code == 1 and "at least one" in err


def test_comment(cli):
    tid = _new(cli, "CLI comment")
    code, out, err = cli("comment", tid, "hello", "--actor", "owner")
    assert code == 0, err
    assert "comment" in out
    code, out, _ = cli("get", tid, "--json")
    # comment lands in lineage events; verify via lineage
    code, out, _ = cli("lineage", tid, "--json")
    evs = json.loads(out)["events"]
    assert any(e["kind"] == "comment" and e["payload"]["text"] == "hello"
               for e in evs)


def test_event_json_payload_and_bad_json(cli):
    tid = _new(cli, "CLI event")
    code, out, err = cli("event", tid, "artifact", "--json",
                         '{"name": "pkg"}')
    assert code == 0, err
    code, _, err = cli("event", tid, "artifact", "--json", "{bad")
    assert code == 1 and "not valid JSON" in err


# -- link / lineage ----------------------------------------------------------------

def test_link_and_cycle_rejection_exit_1(cli):
    a = _new(cli, "CLI link A")
    b = _new(cli, "CLI link B")
    code, out, err = cli("link", a, b, "blocks")
    assert code == 0 and "blocks" in out, err
    child = _new(cli, "CLI link child", parent=a)
    code, _, err = cli("link", child, a, "parent_of")  # closes the loop
    assert code == 1 and "cycle_rejected" in err


def test_lineage_human_and_json(cli):
    parent = _new(cli, "CLI lin root")
    child = _new(cli, "CLI lin child", parent=parent)
    code, out, err = cli("lineage", parent)
    assert code == 0, err
    assert "children: 1" in out
    code, out, _ = cli("lineage", child, "--json")
    lin = json.loads(out)
    assert [a["id"] for a in lin["ancestry"]] == [parent]


# -- digest / board -----------------------------------------------------------------

def test_digest_json_md_full(cli):
    tid = _new(cli, "CLI digest")
    cli("start", tid)
    code, out, err = cli("digest", tid)
    assert code == 0 and "advanced to Running" in out, err
    code, out, _ = cli("digest", tid, "--json")
    assert any("advanced to Running" == b["text"]
               for b in json.loads(out)["bullets"])
    code, out, _ = cli("digest", tid, "--md")
    assert code == 0 and out.startswith("# Updates")
    code, out, _ = cli("digest", tid, "--full", "--json")
    assert code == 0
    assert json.loads(out)["project"] == tid


def test_board(cli):
    tid = _new(cli, "CLI board item")
    code, out, err = cli("board")
    assert code == 0, err
    assert "draft" in out and tid in out
    code, out, _ = cli("board", "--json")
    board = json.loads(out)
    assert "columns" in board and "child_counts" in board


# -- bind ------------------------------------------------------------------------------

def test_bind(cli):
    tid = _new(cli, "CLI bind me")
    code, out, err = cli("bind", tid, "matrix", "!cli-room:example.org",
                         "--thread", "$t1", "--root", "$r1")
    assert code == 0, err
    assert "matrix" in out and "!cli-room:example.org" in out


# -- start / done shortcuts --------------------------------------------------------------

def test_start_from_draft_two_patches(cli):
    tid = _new(cli, "CLI start draft")
    code, out, err = cli("start", tid)
    assert code == 0, err
    assert "running" in out
    code, out, _ = cli("get", tid, "--json")
    assert json.loads(out)["status"] == "running"


def test_done_walks_from_draft(cli):
    tid = _new(cli, "CLI done walk")
    code, out, err = cli("done", tid)
    assert code == 0, err
    assert "done" in out
    code, out, _ = cli("get", tid, "--json")
    assert json.loads(out)["status"] == "done"


def test_missing_ticket_exit_1(cli):
    code, _, err = cli("get", "TK-GHOST")
    assert code == 1 and "not_found" in err
