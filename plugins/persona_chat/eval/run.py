"""AI 群友的回归评测：拿生产快照里的场景回放，两两盲评比较两种变体。

    uv run python plugins/persona_chat/eval/run.py snapshot
    uv run python plugins/persona_chat/eval/run.py scenes
    uv run python plugins/persona_chat/eval/run.py calibrate --judge deepseek-v4-pro
    uv run python plugins/persona_chat/eval/run.py ab base -style --samples 2

数据（快照、场景、回放结果、群主评过的题）都在 ``$PERSONA_EVAL_DIR``（默认 ``~/persona-eval``），
里面是真实的群聊记录，不进仓库。回放结果按插件源码和变体缓存：代码不变时重跑只花评委的钱。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import httpx
import judge
import replay
import scenes as scene_picker
import variants
from replay import DATA, Host, ReplayError, Reply, Scene

_SRC = Path(__file__).resolve().parents[1] / "src/tenko_plugin_persona_chat"


def _scenes() -> list[Scene]:
    return [Scene(**s) for s in json.loads((DATA / "scenes.json").read_text())["scenes"]]


def _snapshot_of_scenes() -> Path:
    return DATA / "snapshots" / json.loads((DATA / "scenes.json").read_text())["snapshot"]


def _fingerprint(variant: str) -> str:
    """插件源码加变体定义：任何一处变了，缓存的回放就作废。"""

    digest = hashlib.sha256()
    for path in sorted(_SRC.glob("*.py")):
        digest.update(path.read_bytes())
    digest.update(inspect.getsource(variants).encode())
    digest.update(variant.encode())
    return digest.hexdigest()[:12]


def _client(timeout: float = 300) -> httpx.AsyncClient:
    config = replay.prod_config()
    return httpx.AsyncClient(
        base_url=config.get("base_url", "https://api.deepseek.com"),
        headers={"Authorization": f"Bearer {config['api_key']}"},
        timeout=timeout,
    )


async def generate(host: Host, data, scenes: list[Scene], variant: str, samples: int, limit: int):
    """回放并缓存 ``variant`` 在每个场景上的 ``samples`` 次回复：{场景#第几次: Reply 或 None}。"""

    path = DATA / "replies" / f"{variant}@{_fingerprint(variant)}.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    todo = [(s, i) for s in scenes for i in range(samples) if f"{s.id}#{i}" not in cache]
    if todo:
        print(f"{variant}：回放 {len(todo)} 次", file=sys.stderr)
        gate = asyncio.Semaphore(limit)
        done = 0

        async def one(scene: Scene, i: int) -> None:
            nonlocal done
            async with gate:
                try:
                    reply = await host.play(data, scene)
                    cache[f"{scene.id}#{i}"] = asdict(reply)
                except ReplayError as error:
                    print(f"  {scene.id} 作废：{error}", file=sys.stderr)
                    cache[f"{scene.id}#{i}"] = None
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(todo)}", file=sys.stderr)

        with variants.applied(host, variant):
            await asyncio.gather(*(one(s, i) for s, i in todo))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return {k: Reply(**v) if v else None for k, v in cache.items()}


def _sign_test(wins: int, losses: int) -> float:
    """双侧符号检验的 p 值：平局不算。"""

    n = wins + losses
    if not n:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


async def cmd_ab(args) -> None:
    scenes = [s for s in _scenes() if not args.tag or any(t in s.tags for t in args.tag)]
    snapshot = _snapshot_of_scenes()
    data = replay.load_conversations(snapshot)
    moods = {
        name: entry.get("mood", "")
        for name, entry in json.loads((snapshot / "persona_chat/stickers.json").read_text())[
            "data"
        ].items()
    }
    host = await Host.start(snapshot)
    a = await generate(host, data, scenes, args.a, args.samples, args.limit)
    b = await generate(host, data, scenes, args.b, args.samples, args.limit)
    await host.stop()
    async with _client() as client:
        referee = judge.Judge(client, args.judge, args.judge_limit)
        pairs = []
        for scene in scenes:
            lines = data[scene.key]["lines"]
            ctx = judge.context(lines, scene.cut)
            for i in range(args.samples):
                # A/A 对照：同一个变体拿第 i 次和第 i+1 次比，看评委的噪声有多大。
                left = a.get(f"{scene.id}#{i}")
                right = b.get(f"{scene.id}#{(i + 1) % args.samples if args.a == args.b else i}")
                if left and right:
                    pairs.append((scene, i, left, right, ctx, lines))
        verdicts = await asyncio.gather(
            *(
                referee.compare(
                    ctx, judge.show(x.bubbles, lines, moods), judge.show(y.bubbles, lines, moods)
                )
                for _, _, x, y, ctx, lines in pairs
            )
        )
    report = _report(args, scenes, a, b, pairs, verdicts)
    out = DATA / "reports" / f"{args.a}_vs_{args.b}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n明细：{out}")


def _report(args, scenes, a, b, pairs, verdicts) -> dict:
    tally: dict[str, Counter] = defaultdict(Counter)
    detail = []
    for (scene, i, x, y, _, _), v in zip(pairs, verdicts, strict=True):
        side = {"A": "左", "B": "右"}.get(v, "平")
        for tag in ["all", *[t for t in scene.tags if t == "general" or t.startswith("target:")]]:
            tally[tag][side] += 1
        detail.append(
            {
                "scene": scene.id,
                "kind": scene.kind,
                "tags": scene.tags,
                "sample": i,
                "左": x.bubbles,
                "右": y.bubbles,
                "winner": side,
            }
        )
    print(f"{args.a}（左） vs {args.b}（右），评委 {args.judge}")
    for tag, c in sorted(tally.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        wins, losses = c["左"], c["右"]
        print(
            f"  {tag:16} {wins:3} : {losses:3}  平 {c['平']:3}   p={_sign_test(wins, losses):.2f}"
        )
    for name, replies in ((args.a, a), (args.b, b)):
        rs = [r for s in scenes for i in range(args.samples) if (r := replies.get(f"{s.id}#{i}"))]
        bubbles = [x for r in rs for x in r.bubbles]
        silent = Counter(
            s.kind
            for s in scenes
            for i in range(args.samples)
            if (r := replies.get(f"{s.id}#{i}")) and not r.bubbles
        )
        calls = sum(r.calls for r in rs) / max(1, len(rs))
        chars = sum(map(len, bubbles)) / max(1, len(bubbles))
        print(
            f"  {name}: {len(rs)} 次回放，平均每次 {calls:.2f} 个请求、"
            f"共 {sum(r.cost for r in rs):.2f} 美元；每条平均 {chars:.0f} 字；不说话 {dict(silent)}"
        )
    return {
        "a": args.a,
        "b": args.b,
        "judge": args.judge,
        "tally": {k: dict(v) for k, v in tally.items()},
        "pairs": detail,
    }


async def cmd_calibrate(args) -> None:
    """拿群主亲手评过的题对评委，算评委和群主同向的比例。

    ``labels.json`` 是以前几轮盲评的题和群主的选择。
    """

    labels = json.loads((DATA / "labels.json").read_text())
    moods = json.loads((DATA / "label_moods.json").read_text())
    items = [x for x in labels if x["label"] in ("A", "B")]
    async with _client() as client:
        referee = judge.Judge(client, args.judge)
        got = await asyncio.gather(
            *(
                referee.compare(
                    x["ctx"], judge.show(x["A"], [], moods), judge.show(x["B"], [], moods)
                )
                for x in items
            )
        )
    for name, rounds in (("第 2–4 轮（评分标准从这里提炼）", {2, 3, 4}), ("第 5 轮（留出）", {5})):
        rows = [(x["label"], g) for x, g in zip(items, got, strict=True) if x["round"] in rounds]
        hit = sum(v == g for v, g in rows)
        wrong = sum(g in ("A", "B") and g != v for v, g in rows)
        tie = len(rows) - hit - wrong
        print(
            f"{args.judge} {name}：{len(rows)} 题，同向 {hit}，反向 {wrong}，平 {tie}；"
            f"表态的题里 {hit / max(1, hit + wrong):.0%} 同向"
        )
    tokens = Counter()
    for u in referee.usage:
        tokens.update({k: u.get(k, 0) for k in ("prompt_tokens", "completion_tokens")})
    print(f"用量：{dict(tokens)}")


def cmd_scenes(args) -> None:
    snapshot = Path(args.snapshot) if args.snapshot else replay.latest_snapshot()
    chosen = scene_picker.pick(replay.load_conversations(snapshot), seed=args.seed)
    labels = DATA / "labels.json"
    if labels.exists():
        conversations, found = scene_picker.labeled(json.loads(labels.read_text()))
        (DATA / "labeled.json").write_text(json.dumps(conversations, ensure_ascii=False))
        chosen += found
    (DATA / "scenes.json").write_text(
        json.dumps(
            {"snapshot": snapshot.name, "scenes": [asdict(s) for s in chosen]},
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"{len(chosen)} 个场景：", Counter(s.kind for s in chosen))
    print(Counter(t for s in chosen for t in s.tags if t == "general" or t.startswith("target:")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="把生产数据拷一份定住")
    p = sub.add_parser("history", help="把更早的会话存盘并进最新的快照")
    p.add_argument("older", nargs="+", type=Path)
    p = sub.add_parser("scenes", help="从快照挑场景")
    p.add_argument("--snapshot")
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("calibrate", help="拿群主评过的题对评委")
    p.add_argument("--judge", default="deepseek-flash")
    p = sub.add_parser("ab", help="回放两种变体并两两盲评")
    p.add_argument("a", choices=variants.VARIANTS)
    p.add_argument("b", choices=variants.VARIANTS)
    p.add_argument("--samples", type=int, default=2)
    p.add_argument("--tag", action="append", help="只比带这些标签的场景，如 general、target:bot")
    p.add_argument("--judge", default="deepseek-flash")
    p.add_argument("--limit", type=int, default=6, help="同时回放几个场景")
    p.add_argument("--judge-limit", type=int, default=12, help="同时问评委几次")
    args = parser.parse_args()
    if args.cmd == "snapshot":
        print(replay.take_snapshot())
    elif args.cmd == "history":
        print(replay.merge_history(replay.latest_snapshot(), args.older))
    elif args.cmd == "scenes":
        cmd_scenes(args)
    elif args.cmd == "calibrate":
        asyncio.run(cmd_calibrate(args))
    else:
        asyncio.run(cmd_ab(args))


if __name__ == "__main__":
    main()
