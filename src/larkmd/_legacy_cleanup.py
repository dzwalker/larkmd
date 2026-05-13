#!/usr/bin/env python3
"""一次性 cleanup：删除 .feishu-sync-state.json 里 previous_wiki_node_tokens 列表中的所有 orphan wiki 节点。

前提：lark-cli 已用 wiki:wiki scope 重新授权，否则 delete API 会返回 131005。

用法：
  python3 scripts/cleanup_feishu_orphans.py            # dry-run，列出要删的
  python3 scripts/cleanup_feishu_orphans.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_FILE = REPO / ".feishu-sync-state.json"
ENV_FILE = REPO / ".env"


def load_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def get_obj_token_from_node(space_id: str, node_token: str) -> str | None:
    """get_node 反查 obj_token（wiki delete URL 要的是 obj_token，不是 node_token）。"""
    cmd = ["lark-cli", "api", "GET",
           "/open-apis/wiki/v2/spaces/get_node",
           "--as", "user",
           "--params", json.dumps({"token": node_token, "obj_type": "wiki"})]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not r.stdout.strip():
        return None
    try:
        out = json.loads(r.stdout)
        if out.get("code") == 0:
            return out["data"]["node"]["obj_token"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def lark_delete_wiki_doc(space_id: str, obj_token: str) -> tuple[bool, str]:
    """飞书 wiki delete API 要的是 obj_token（docx 的）放路径，不是 wiki node_token。
    返回 (success, msg)。"""
    cmd = ["lark-cli", "api", "DELETE",
           f"/open-apis/wiki/v2/spaces/{space_id}/nodes/{obj_token}",
           "--as", "user", "--data", json.dumps({"obj_type": "docx"})]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not r.stdout.strip():
        return False, f"empty stdout (rc={r.returncode}); stderr={r.stderr[:200]}"
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False, f"non-JSON stdout: {r.stdout[:200]}"
    if out.get("ok") is False or out.get("code", 1) != 0:
        return False, json.dumps(out.get("error", out), ensure_ascii=False)[:200]
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    args = ap.parse_args()

    env = load_env()
    space_id = env["FEISHU_WIKI_SPACE_ID"]
    state = json.loads(STATE_FILE.read_text())

    # 收集 (rel, wiki_node_token)。docx 不靠 state 配对（历史 hand-inject 不可靠），
    # 删之前一律用 get_node 反查 obj_token。
    todo: list[tuple[str, str]] = []
    for rel, v in state.items():
        for w in v.get("previous_wiki_node_tokens", []):
            todo.append((rel, w))

    print(f"=== {len(todo)} orphan wiki nodes to delete ===")
    for rel, w in todo:
        print(f"  wiki={w}  ← {rel}")
    if not args.apply:
        print("\n(dry-run; pass --apply to actually delete)")
        return

    print()
    deleted_wiki_tokens = set()
    already_gone = set()
    failed = []
    for rel, w in todo:
        d = get_obj_token_from_node(space_id, w)
        if d is None:
            # 节点查不到 → 多半已被先前操作删了，当成"已经没了"
            print(f"  GONE wiki={w}  ({rel}): node not in wiki")
            already_gone.add(w)
            continue
        ok, msg = lark_delete_wiki_doc(space_id, d)
        if ok:
            print(f"  OK   wiki={w} docx={d}  ({rel})")
            deleted_wiki_tokens.add(w)
        else:
            print(f"  FAIL wiki={w} docx={d}  ({rel}): {msg}")
            failed.append((rel, w))

    # 删成功的 + 早就没了的，都从 state 里清掉。previous_docx 整体丢弃（不可靠）。
    cleared = deleted_wiki_tokens | already_gone
    if cleared:
        for rel, v in state.items():
            v["previous_wiki_node_tokens"] = [
                w for w in v.get("previous_wiki_node_tokens", []) if w not in cleared
            ]
            if not v["previous_wiki_node_tokens"]:
                v.pop("previous_wiki_node_tokens", None)
            # previous_docx 丢弃 —— 不再维护（删 wiki node 已不需要）
            v.pop("previous_docx_tokens", None)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    print(f"\n=== Summary: deleted {len(deleted_wiki_tokens)}, "
          f"already gone {len(already_gone)}, failed {len(failed)} ===")


if __name__ == "__main__":
    main()
