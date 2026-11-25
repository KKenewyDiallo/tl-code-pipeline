"""MR ouverte au push via les push options GitLab (token sans scope `api`,
constat TOOLLIVE-3585)."""

import asyncio

from worker.activities import git_tools


def test_push_options_mr():
    args = git_tools.mr_push_options("develop", "TOOLLIVE-3585 Titre", "ligne 1\nligne 2")
    opts = args[1::2]
    assert args[0::2] == ["-o"] * len(opts)
    assert opts == ["merge_request.create", "merge_request.target=develop",
                    "merge_request.title=TOOLLIVE-3585 Titre",
                    "merge_request.remove_source_branch",
                    "merge_request.description=ligne 1 ligne 2"]


def test_push_options_sans_description():
    assert not any("description" in a for a in git_tools.mr_push_options("develop", "T"))


def test_parse_mr_url_creee():
    out = ("remote: \nremote: View merge request for ia-generated/x:\n"
           "remote:   https://gitlab.example/g/p/-/merge_requests/1608\nremote: \n")
    assert git_tools.parse_mr_url(out) == {
        "iid": 1608, "web_url": "https://gitlab.example/g/p/-/merge_requests/1608"}


def test_parse_mr_url_non_creee():
    out = ("remote: To create a merge request for x, visit:\n"
           "remote:   https://gitlab.example/g/p/-/merge_requests/new?merge_request%5Bsource_branch%5D=x\n")
    assert git_tools.parse_mr_url(out) is None


def test_push_with_mr_repli_sans_push_options(monkeypatch):
    calls = []

    async def fake_sh(cmd, cwd, extra_env=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "config"]:
            return {"code": 0, "output": "https://gitlab.example/g/p.git\n"}
        if "-o" in cmd:
            return {"code": 1, "output": "fatal: the receiving end does not support push options"}
        return {"code": 0, "output": "ok"}

    monkeypatch.setattr(git_tools, "_sh", fake_sh)
    res = asyncio.run(git_tools.push_branch_with_mr("/r", "b", "develop", "T"))
    assert res == {"mr": None}
    assert "-o" not in calls[-1] and calls[-1][:2] == ["git", "push"]
