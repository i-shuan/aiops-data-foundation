"""回饋自然語言抽取的評測（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）：
tests/feedback_corpus.yaml 的每一句跑一次 core.llm.extract_feedback，四欄（feedback_type、identified_cause、target_service、
suggested_action）全對才算對；印每句對錯、全對率、每欄的正確率、錯在哪類句子。不改標準答案。

用法（在 engine 底下）：
  LLM_PROVIDER=template python3 -m tests.feedback_extract_eval                  # 不叫模型的退路
  python3 -m tests.feedback_extract_eval --env ../e2e/local-k3d/.env            # 真的模型：只從 .env 載入 LLM_PROVIDER、LLM_API_KEY、
                                                                                 #   LLM_CHAT_MODEL、OPENAI_BASE_URL；key 不印、不寫檔
  python3 -m tests.feedback_extract_eval --provider openai --out eval.md        # 指定 provider、結果另存 Markdown（不含任何機密）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

import yaml

import service.rca.llm as LLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "feedback_corpus.yaml")
ENV_KEYS = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_CHAT_MODEL", "OPENAI_BASE_URL")
SECRET = ("LLM_API_KEY",)


def load_env(path: str) -> list[str]:
    """只把 ENV_KEYS 這四個載進環境變數（其他一律不碰）。回傳載入了哪些 key 的名字（不含值）。"""
    got = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ENV_KEYS and v:
                os.environ[k] = v
                got.append(k)
    return got


def evaluate(items: list[dict], provider: str) -> dict:
    rows = []
    for it in items:
        t0 = time.time()
        r = LLM.extract_feedback(it["text"], it.get("alert") or {}, provider)
        got = r["fields"] or {}
        wrong = [f for f in LLM.EXTRACT_FIELDS if got.get(f) != it["expect"].get(f)]
        rows.append({"id": it["id"], "text": it["text"], "expect": it["expect"], "got": got, "ok": r["ok"], "reason": r["reason"],
                     "provider": r["provider"], "wrong": wrong, "seconds": time.time() - t0, "note": it.get("note")})
    n = len(rows)
    exact = sum(1 for r in rows if r["ok"] and not r["wrong"])
    per_field = {f: sum(1 for r in rows if r["ok"] and f not in r["wrong"]) for f in LLM.EXTRACT_FIELDS}
    # 模型掛了、回空的那幾句走了關鍵字退路（provider 會寫明）：要分開看，不然「模型的解析率」會被退路灌水
    fallback = [r["id"] for r in rows if r["provider"] != provider]
    return {"rows": rows, "n": n, "exact": exact, "rate": exact / n if n else None, "validated": sum(1 for r in rows if r["ok"]),
            "per_field": per_field, "seconds": sum(r["seconds"] for r in rows), "fallback": fallback}


def report(res: dict, provider: str, model: str) -> list[str]:
    out = [f"# 回饋抽取評測：provider {provider}" + (f"、模型 {model}" if model else ""), "",
           f"{res['n']} 句，四欄全對 **{res['exact']}／{res['n']}（{res['rate']:.0%}）**，格式驗過的 {res['validated']}／{res['n']}，"
           f"走退路（模型掛了、回空的）{len(res['fallback'])} 句" + (f"（#{'、#'.join(map(str, res['fallback']))}）" if res["fallback"] else "")
           + f"，共 {res['seconds']:.0f} 秒", "",
           "| 欄位 | 對的 |", "|---|---|"] + [f"| {f} | {v}／{res['n']} | " for f, v in res["per_field"].items()]
    out += ["", "| # | 句子 | 結果 | 錯的欄位（抽到 → 應為） |", "|---|---|---|---|"]
    for r in res["rows"]:
        if not r["ok"]:
            detail = f"沒抽到：{r['reason']}"
        else:
            detail = "；".join(f"{f}：{r['got'].get(f)} → {r['expect'][f]}" for f in r["wrong"]) or "—"
        mark = "✓" if r["ok"] and not r["wrong"] else "✗"
        out.append(f"| {r['id']} | {r['text'][:48]}{'…' if len(r['text']) > 48 else ''} | {mark} | {detail} |")
    bad = [r for r in res["rows"] if not (r["ok"] and not r["wrong"])]
    if bad:
        by_field = Counter(f for r in bad for f in (r["wrong"] or ["沒抽到"]))
        by_type = Counter(r["expect"]["feedback_type"] for r in bad)
        by_act = Counter(r["expect"]["suggested_action"] for r in bad)
        out += ["", "錯在哪類：", f"- 依欄位：{dict(by_field)}", f"- 依標準答案的 feedback_type：{dict(by_type)}",
                f"- 依標準答案的 suggested_action：{dict(by_act)}"]
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--env", help="只從這個 .env 載入 LLM_PROVIDER、LLM_API_KEY、LLM_CHAT_MODEL、OPENAI_BASE_URL（key 不印）")
    p.add_argument("--provider", help="template／openai／anthropic（不給就用 LLM_PROVIDER，再不然 template）")
    p.add_argument("--corpus", default=CORPUS)
    p.add_argument("--out", help="結果另存 Markdown")
    args = p.parse_args(argv)
    if args.env:
        got = load_env(args.env)
        print(f"從 {args.env} 載入：{'、'.join(k + ('（不顯示）' if k in SECRET else '=' + os.environ[k]) for k in got) or '（沒有可載入的 key）'}")
    provider = args.provider or os.environ.get("LLM_PROVIDER") or "template"
    model = os.environ.get("LLM_CHAT_MODEL", "") if provider != "template" else ""
    with open(args.corpus, encoding="utf-8") as f:
        items = yaml.safe_load(f)["items"]
    res = evaluate(items, provider)
    text = "\n".join(report(res, provider, model))
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
