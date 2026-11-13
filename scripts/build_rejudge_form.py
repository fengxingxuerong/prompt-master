"""生成"改判 25 条"的**可判**载体：完整输出 + 一卡一条 + 导出回填 CSV。

## 为什么不是直接用 `score_form.owner_rejudge.csv`

那张表由 `pm.cli.calibrate._make_score_form` 生成，`输出摘要` 一列被 `_clip` 截到
**160 字符**。而这次要判的恰恰是两类只有看全文才判得出来的东西：

- **编造/无依据的关键事实**（rubric 锚定规则：总分封顶 6.0）；
- **核心交付物缺位、被一句不可核对的话糊过去**（task_completion ≤5，总分封顶 7.0）。

160 字摘要里看不出"这个数字是算出来的还是编的"，也看不出"需求的那一件东西到底给了没有"。
凭它打出来的 1-10 分，与本项目已经踩过的那次事故同类——活账本里 6/11 条锚点的"人工分"
是编的（`docs/evaluation.md` §三）。**让所有者凭摘要打分，等于再制造一批假人工锚点。**

所以本脚本只做一件事：把 `samples.candidates.json` 里的**完整** `test_output`
摊成一页能直接判的 HTML，判完导出一张与 `_FORM_COLUMNS` 同列的 CSV，
交给既有的 `--apply-scores` 回填。不新建存储、不改锚点文件。

## 两条刻意的设计

1. **评委分默认隐藏**（可一键展开）。评委分是同族模型的读数，摆在最显眼处就是锚定。
   `REVIEW.md` 的立场是"先去看你和它不一致的那些"，不是抄它——那就让它默认不在视线里。
2. **`human_score` 一律留空、导出只导填了的行**。与 `build_owner_rejudge_queue.py`
   同一条约束：留空 = 没判 = 不进分母；导出 48 行会把"没判"变成"判了但没写"。

用法：`python scripts/build_rejudge_form.py [--batch main|r4|all] [--check]`
（`--check` 只比对已提交的 HTML 是否仍与数据源逐字一致，守卫测试调的就是它）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent
# 两个目录都要在 sys.path 上：它是 `python scripts/build_rejudge_form.py` 直接跑的，
# 也被 tests/ 用 spec_from_file_location 加载 —— 后一种情况 sys.path[0] 不含 scripts/，
# 只加 ROOT 会让 `from build_owner_rejudge_queue import …` 在测试里 ImportError。
for _p in (ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from build_owner_rejudge_queue import ANCHOR_DIR, TARGET, plan  # noqa: E402
from pm.cli.calibrate import _FORM_COLUMNS  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "judge_calibration" / "rejudge_form.html"

RUBRIC = [
    ("9-10", "明显超出上线要求：逐条对照承诺清单后仍找不到实质问题"),
    ("8-8.9", "生产可用（判定线）：只有 1-2 处不影响读者决策的轻微问题"),
    ("7-7.9", "接近可用但单条不成立：存在会误导读者或需人工返工的问题"),
    ("5-6", "勉强可用，有明显问题"),
    ("1-4", "不可用，需重写：缺失核心要求、格式全错或大量幻觉"),
]
ANCHORS = [
    "编造/无依据的关键事实（数值、日期、结论凭空生成）：总分封顶 6.0，不得给 5 分以上。",
    "核心交付物缺位，或被一句不可核对的话糊过去（需求点名要的那件东西其实没给）：task_completion ≤5，总分封顶 7.0。",
    "空壳/纯拒答（只有模板骨架、「无」「数据缺失」「请提供后重试」，对输入已有信息没有任何实质处理）：总分封顶 4.0。",
    "反过来的防线：输入确实撑不起结论时，显式列缺失项 + 给可操作的补全路径**就是正确交付**，该给 8 分以上就给。判据只有一条——是「已够判断却推给信息不足」还是「确实不足」。",
]

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f1216;--card:#161b22;--line:#262d36;--fg:#e6edf3;--dim:#8b949e;--acc:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:9;background:#0f1216f2;border-bottom:1px solid var(--line);padding:12px 20px;backdrop-filter:blur(6px)}
h1{font-size:15px;font-weight:500;margin:0 0 8px}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:13px}
.bar .grow{flex:1}
button{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer}
button:hover{border-color:#3d444d}
button.on{border-color:var(--acc);color:var(--acc)}
#prog{min-width:130px;color:var(--dim)}
#prog b{color:var(--acc)}
main{max-width:900px;margin:0 auto;padding:18px 20px 120px}
.rubric{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:18px}
.rubric[hidden]{display:none}
.rubric table{border-collapse:collapse;width:100%;font-size:13px}
.rubric td{padding:3px 8px 3px 0;vertical-align:top}
.rubric td:first-child{white-space:nowrap;color:var(--warn);font-variant-numeric:tabular-nums}
.rubric ul{margin:10px 0 0;padding-left:18px;color:#c9d1d9}
.rubric li{margin:4px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card.done{border-color:#1f6f3f}
.meta{display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--dim);margin-bottom:8px}
.tag{background:#21262d;border:1px solid var(--line);border-radius:5px;padding:1px 7px}
.tag.band{color:var(--warn);border-color:#3d3418}
.tag.judge{color:#79c0ff;border-color:#1f3b57}
.judge{display:none}
.showj .judge{display:inline}
.req{font-size:13px;color:#c9d1d9;margin:6px 0}
.note{font-size:12px;color:var(--warn);margin:6px 0}
pre{white-space:pre-wrap;word-break:break-word;background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:420px;overflow:auto;font:12.5px/1.6 ui-monospace,Consolas,monospace;color:#c9d1d9}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
input[type=number]{width:82px;background:#0d1117;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:5px 8px;font-size:14px}
.row .hint{color:var(--dim);font-size:12px}
footer{position:fixed;left:0;right:0;bottom:0;background:#0f1216f2;border-top:1px solid var(--line);padding:10px 20px;display:flex;gap:12px;align-items:center}
footer .grow{flex:1;font-size:12px;color:var(--dim)}
</style>
</head>
<body>
<header>
<h1>__H1__</h1>
<div class="bar">
<span id="prog"></span>
<span class="grow"></span>
<button id="tJudge">显示评委分参考</button>
<button id="tRubric">判定口径</button>
<button id="exp">导出 CSV（只导已填）</button>
</div>
</header>
<main>
<section class="rubric" id="rubric">
<table>__RUBRIC__</table>
<ul>__ANCHORS__</ul>
<p style="color:#8b949e;font-size:12px;margin:10px 0 0">每条只回答一件事：<b style="color:#e6edf3">换你来看这份输出，值不值 8.0（可上线）</b>，再给一个 1-10 的分（可带小数）。判不了就留空——留空不回填，也不会进分母。</p>
</section>
<div id="list"></div>
</main>
<footer>
<span class="grow" id="fmsg">填完点「导出 CSV」，再用 <code>run.py calibrate --samples … --apply-scores 该CSV</code> 回填。</span>
<button id="next">跳到下一条未判</button>
</footer>
<script>
const DATA = __DATA__;
const KEY = "__KEY__";
let scores = JSON.parse(localStorage.getItem(KEY) || "{}");
const list = document.getElementById("list");

const _escEl=document.createElement("span");
function esc(s){_escEl.textContent=String(s??"");return _escEl.innerHTML;}

function card(it, idx){
  const d = document.createElement("section");
  d.className = "card";
  d.id = "c-"+it.id;
  d.innerHTML = `
  <div class="meta">
    <span class="tag">#${idx+1}</span>
    <span class="tag">${esc(it.id)}</span>
    <span class="tag band">带 ${esc(it.band)}</span>
    <span class="tag judge">评委分参考 ${esc(it.judge)}</span>
    <span class="tag">${esc(it.target)}</span>
    ${it.dims ? `<span class="tag judge">${esc(it.dims)}</span>` : ""}
  </div>
  <div class="req"><b>原始需求：</b>${esc(it.task)}</div>
  ${it.note ? `<div class="note">机械线索：${esc(it.note)}</div>` : ""}
  ${it.input ? `<details><summary style="color:#8b949e;font-size:12px;cursor:pointer">被测输入（点开）</summary><pre>${esc(it.input)}</pre></details>` : ""}
  <pre>${esc(it.output)}</pre>
  <div class="row">
    <b>你给的分</b>
    <input type="number" min="1" max="10" step="0.5" data-id="${it.id}" value="${scores[it.id] ?? ""}" placeholder="1-10">
    <button data-quick="4" data-id="${it.id}">4 不可用</button>
    <button data-quick="6" data-id="${it.id}">6 勉强</button>
    <button data-quick="7" data-id="${it.id}">7 差一点</button>
    <button data-quick="8" data-id="${it.id}">8 可上线</button>
    <button data-quick="9" data-id="${it.id}">9 超预期</button>
    <button data-clr="${it.id}">清空/判不了</button>
    <span class="hint">Enter 到下一条未判</span>
  </div>`;
  return d;
}

DATA.forEach((it,idx)=>list.appendChild(card(it,idx)));

function paint(){
  let n = 0;
  list.querySelectorAll("input[data-id]").forEach(inp=>{
    const v = scores[inp.dataset.id];
    inp.value = (v===undefined?"":v);
    inp.closest(".card").classList.toggle("done", v!==undefined);
    if(v!==undefined) n++;
  });
  document.getElementById("prog").innerHTML = `已判 <b>${n}</b> / ${DATA.length}`;
  document.getElementById("fmsg").textContent = n
    ? `已判 ${n} 条。导出的 CSV 里只有这 ${n} 行，其余不会被写回锚点文件。`
    : "填完点「导出 CSV」，再用 run.py calibrate --samples … --apply-scores 该CSV 回填。";
}

function save(){localStorage.setItem(KEY, JSON.stringify(scores));paint();}

list.addEventListener("input", e=>{
  if(!e.target.matches("input[data-id]")) return;
  const raw = e.target.value.trim();
  if(raw === ""){delete scores[e.target.dataset.id];save();return;}
  const v = Number(raw);
  if(!isFinite(v) || v < 1 || v > 10){e.target.style.borderColor="#f85149";return;}
  e.target.style.borderColor="";
  scores[e.target.dataset.id] = v; save();
});
list.addEventListener("click", e=>{
  const b = e.target.closest("button"); if(!b) return;
  if(b.dataset.quick){scores[b.dataset.id] = Number(b.dataset.quick);save();}
  if(b.dataset.clr){delete scores[b.dataset.clr];save();}
});
list.addEventListener("keydown", e=>{
  if(e.key!=="Enter" || !e.target.matches("input[data-id]")) return;
  e.preventDefault();
  const inputs=[...list.querySelectorAll("input[data-id]")];
  const i=inputs.indexOf(e.target);
  for(let k=1;k<=inputs.length;k++){
    const nx=inputs[(i+k)%inputs.length];
    if((nx.value||"").trim()===""){nx.focus();nx.scrollIntoView({block:"center"});return;}
  }
});
document.getElementById("tJudge").addEventListener("click", e=>{
  document.body.classList.toggle("showj");
  const on = document.body.classList.contains("showj");
  e.target.classList.toggle("on", on);
  e.target.textContent = on ? "隐藏评委分参考" : "显示评委分参考";
});
document.getElementById("tRubric").addEventListener("click", ()=>{
  const r=document.getElementById("rubric"); r.hidden=!r.hidden;
});
document.getElementById("exp").addEventListener("click", ()=>{
  const cols = __COLS__;
  const rows = DATA.filter(it=>scores[it.id]!==undefined).map(it=>[
    it.id, it.band, it.judge, it.note, it.target, it.task, it.summary, scores[it.id], it.remark
  ]);
  if(!rows.length){alert("一条都还没填。");return;}
  const q = v => `"${String(v??"").replace(/"/g,'""')}"`;
  const body = [cols.map(q).join(","), ...rows.map(r=>r.map(q).join(","))].join("\\r\\n");
  const blob = new Blob(["\\ufeff"+body], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "__DOWNLOAD__";
  a.click();
  document.getElementById("fmsg").textContent = `已导出 ${rows.length} 行 → ` + a.download;
});
document.getElementById("next").addEventListener("click", ()=>{
  const nx=[...list.querySelectorAll("input[data-id]")].find(i=>(i.value||"").trim()==="");
  if(nx){nx.focus();nx.scrollIntoView({block:"center"});}
});
paint();
</script>
</body>
</html>
"""


# 两批判分任务的参数。`key` 是 localStorage 的命名空间：
# 主线沿用 "pm-rejudge-25" 不改 —— 改名会让已经填在浏览器里、还没导出的分变成孤儿，
# 那是最贵的返工；r4 用独立命名空间，避免两批共用一个字典把分串到别的 id 上。
BATCHES: dict[str, dict[str, Any]] = {
    "main": {
        "samples": TARGET,
        "form": ANCHOR_DIR / "score_form.owner_rejudge.csv",
        "out": DEFAULT_OUT,
        "key": "pm-rejudge-25",
        "download": "score_form.owner_rejudge.filled.csv",
        "h1": "改判打分 · 这 {n} 条的人工分是 AI 代判的（与评委同族），需要你亲自判",
    },
    "r4": {
        "samples": ANCHOR_DIR / "samples.candidates.r4.json",
        "form": ANCHOR_DIR / "score_form.r4.csv",
        "out": ANCHOR_DIR / "rejudge_form.r4.html",
        "key": "pm-rejudge-r4",
        "download": "score_form.r4.filled.csv",
        "h1": "改判打分 · r4 批 {n} 条，全部没有人工分（也没有 AI 代判兜底）",
    },
}


def _items(batch: str) -> list[dict[str, Any]]:
    """待判条目。主线沿用 build_owner_rejudge_queue.plan() 的排法（判定线附近优先），
    r4 那批整批待判、按文件顺序 —— 两种批次的选择规则本来就不同，不要硬凑一个。"""
    cfg = BATCHES[batch]
    data = json.loads(Path(cfg["samples"]).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else (data.get("samples") or [])
    if batch != "main":
        return [it for it in items if isinstance(it, dict) and not it.get("human_score")]
    order = {rid: i for i, rid in enumerate(plan()[0])}
    todo = [it for it in items if isinstance(it, dict) and str(it.get("id")) in order]
    todo.sort(key=lambda it: order[str(it.get("id"))])
    return todo


def _remarks(form: Path) -> dict[str, str]:
    """打分表里的「备注」列（判定线附近 / 随机审计切片 / AI 代判……）。

    表单导出的 CSV 要能把这一列原样写回去，所以备注必须以**当前**那张表为准。
    旧版把它写成一句常量，2026-11-09 队列重排后表单里仍是旧文案 —— 表能填、
    导出的备注却是上一版的说法，人看不出错，回填时也不会报错。
    """
    if not form.exists():
        return {}
    with form.open(encoding="utf-8-sig", newline="") as fh:
        return {str(r.get("id")): (r.get("备注") or "") for r in csv.DictReader(fh)}


def _payload(batch: str) -> list[dict[str, Any]]:
    cfg = BATCHES[batch]
    remarks = _remarks(Path(cfg["form"]))
    out = []
    for it in _items(batch):
        prov = it.get("provenance") or {}
        dims = prov.get("judge_dims") or {}
        out.append(
            {
                "id": str(it.get("id")),
                "band": str(it.get("band") or ""),
                "judge": "" if prov.get("judge_score") is None else str(prov["judge_score"]),
                "dims": " / ".join(f"{k}={v}" for k, v in dims.items())
                if isinstance(dims, dict)
                else "",
                "target": str(prov.get("target_model") or ""),
                "note": str(it.get("note") or ""),
                "task": str(it.get("original_task") or ""),
                "input": str(it.get("test_input") or ""),
                "output": str(it.get("test_output") or ""),
                "summary": str(it.get("test_output") or "")[:160],
                "remark": remarks.get(str(it.get("id")), ""),
            }
        )
    return out


def render(batch: str) -> str:
    cfg = BATCHES[batch]
    payload = _payload(batch)
    if not payload:
        return ""
    return (
        _HTML.replace(
            "__RUBRIC__",
            "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in RUBRIC),
        )
        .replace("__ANCHORS__", "".join(f"<li>{a}</li>" for a in ANCHORS))
        .replace("__TITLE__", f"改判打分 · {batch} · {len(payload)} 条")
        .replace("__H1__", cfg["h1"].format(n=len(payload)))
        .replace("__KEY__", cfg["key"])
        .replace("__DOWNLOAD__", cfg["download"])
        .replace("__COLS__", json.dumps(list(_FORM_COLUMNS), ensure_ascii=False))
        .replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    )


def build(batch: str = "main", *, out: Path | None = None, check: bool = False) -> int:
    """生成（或 `check=True` 时只比对）一批判分页。返回 0 = 一致/已写。

    `out` 只给守卫用：`tests/test_rejudge_form.py` 要生成到 tmp_path 再逐字比对，
    绝不能在验证过程中改写被提交的 HTML（那会让守卫自己变成"改完再验"的假绿）。

    `check` 存在的理由：这份 HTML 是**派生产物**，数据源（打分表 + 锚点 JSON）
    一动它就该跟着动。没有守卫时它静默漂移过一轮（表单里 25 条的备注停在
    2026-10-18 的文案上，队列 11-09 已经重排），而人是照着它判分的。
    """
    cfg = BATCHES[batch]
    out = Path(out) if out is not None else Path(cfg["out"])
    payload = _payload(batch)
    if not payload:
        print(f"[{batch}] 没有待判条目（全部已亲判）。", file=sys.stderr)
        return 0
    html = render(batch)
    if check:
        old = out.read_text(encoding="utf-8") if out.exists() else ""
        if html != old:
            print(
                f"[drift] {out} 与数据源不一致 —— 跑 "
                f"`python scripts/build_rejudge_form.py --batch {batch}` 重生成",
                file=sys.stderr,
            )
            return 1
        print(f"[ok] {out} 与数据源逐字一致（{len(payload)} 条）")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"改判页：{out}")
    print(
        f"  [{batch}] {len(payload)} 条，每条带**完整**输出（不再截 160 字）、评委分默认隐藏、进度存本地。"
    )
    print("  判完点「导出 CSV」，再回填：")
    print(f"    python run.py calibrate --samples {cfg['samples']} --apply-scores <导出的 CSV>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--batch", choices=[*BATCHES, "all"], default="all", help="哪一批（默认两批都刷）"
    )
    ap.add_argument("--check", action="store_true", help="只比对已提交的 HTML 是否仍与数据源一致")
    ns = ap.parse_args()
    names = list(BATCHES) if ns.batch == "all" else [ns.batch]
    rc = 0
    for name in names:
        rc |= build(name, check=ns.check)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
