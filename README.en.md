# PromptMaster

**[中文](README.md) · [Releases](https://github.com/fengxingxuerong/prompt-master/releases)**

> Turn a one-line request into a prompt that has been **tested, scored, and falsified once** —
> and say plainly what is still wrong with it.
> Output: final prompt + delivery report (Markdown) + optional `SKILL.md` + machine-readable JSON.

**This is not a prompt polisher.** Ranked by cost, it does exactly three things:

| What you want | Command | Cost |
|---|---|---|
| Audit an existing prompt | `run.py check --prompt-file x.md` | Zero calls, zero network. Exit 1 on findings |
| Block only *newly introduced* defects in CI | `run.py gate` | Zero calls. Compares against git baseline |
| Run the full loop: generate → cases → execute → judge → revise | `run.py --task "…"` | Costs money. Run `--preflight` first |

## 30-second quickstart

```bash
pip install -r requirements.txt          # Python >= 3.11
python run.py --selftest                 # no API key: proves the code runs, not that it works
python run.py check --prompt-file my_prompt.md   # free, offline; decide later whether to spend
```

Real run (set `PM_API_KEY`, see `.env.example`):

```bash
python run.py --preflight                # smoke-test every role before burning budget
python run.py --task "analyze sales data" --fast      # first run: ~8-10 calls / a few minutes
python run.py --task "analyze sales data" --target-model deepseek-v3   # full loop: 45-102 calls
python run.py --task "…" --prompt-file my_prompt.md   # improve your own draft; Δ = vs. your version
```

Exit codes are a protocol for agents: `0` passed / `1` delivered but below bar / `2` bad config / `3` run failed.
For the zero-call commands, `1` means "findings" (`check`) or "newly introduced defects" (`diff`/`gate`).
With `--json`, stdout is exactly one JSON object; all progress goes to stderr.

## When *not* to use it

1. **You just want a nicer-sounding prompt** → use [prompt-optimizer](https://github.com/linshenkx/prompt-optimizer).
2. **You need 100+ providers or red-team scanning** → use [promptfoo](https://github.com/promptfoo/promptfoo).
3. **You need a statistically defensible "it got better"** → not yet. Judge discrimination has no statistical support (see below).
4. **You need LangChain / LlamaIndex integration** → it only speaks `openai` / `anthropic`-compatible endpoints.

## What is actually different

- **Zero-call checks.** `check` / `diff` / `gate` render verdicts without a single API call. Most competitors
  require a paid round-trip before you learn anything. For your own repository, `diff --base-ref origin/main
  prompts/x.md` compares the working tree against a git revision, so a PR can block only *newly introduced* defects.
- **Judge calibration as a first-class feature.** `run.py calibrate` reports MAE, bias, rank correlation,
  pass/fail agreement (κ), and discrimination (AUC with confidence intervals) against human anchors.
  Promptfoo scores outputs; it does not tell you whether its scorer is any good.
- **Incremental gate, not absolute.** 16 of 17 built-in prompt templates trip at least one style rule,
  so an absolute gate is permanently red. `gate` compares to the git baseline and only blocks *new* failures.
- **Anti-cheat by construction.** Baseline arm for Δ, `expected`-leak detection in case files,
  `hijacked > 0` blocks `SKILL.md` emission, and zero-valid-sample runs report "not trustworthy".

## Engineering status (measured, not claimed)

| Item | Value |
|---|---|
| Tests | **1,916 collected**, no API key, no live network |
| Coverage | 96.75% on `pm/` (floor 92), 98.35% on `pm/modelhub/*` (floor 82); full run 2026-10-22, 1,907 tests |
| CI | GitHub Actions: Linux matrix (3.11 / 3.12 / 3.13) + a Windows job; ruff + mypy + coverage ratchet |
| Interfaces | CLI (12 subcommands) · REST · MCP (13 tools) · zero-call local panel · optional Tkinter desktop (`extras/`, project-aware: read-only scan → one analysis call → prompt; free scan without a key) |

## Honest limitation (read before trusting any score)

The measuring stick is not aligned yet, and the current ledger cannot prove that it is.

- **Live ledger** (1 round / 11 anchors): MAE **0.882**, bias **+0.291**, κ **0.814**, AUC **0.875**
  (95% CI 0.6–1.0). Those numbers look good, but **6 of the 11 anchors are repository examples
  whose "human" scores were fabricated**, and the thinnest cell of the pass/fail 2×2 has only
  4 anchors (<10) — so this paper cannot answer "pass/fail agreement" at all.
- **Historical 4-round snapshot**: MAE **3.13**, bias **+3.09** (systematic leniency), κ **0.074**;
  AUC **0.623** with 95% CI covering **0.5**. It cannot be recomputed from the live ledger
  (the input rounds were archived), so it is direction-only.

Neither layer is statistical evidence that a higher judge score means a better output.
The checklist scoring protocol was tested and **rejected** (it collapsed 10 of 17 anchors onto
9.35 and stayed lenient), so it ships disabled.

Full experiment log, including the self-audit that found the fabricated anchors:
[`docs/evaluation.md`](docs/evaluation.md) §三十四 (Chinese).

---

中文完整文档：[`README.md`](README.md) · CLI 指南 [`docs/agent-cli-guide.md`](docs/agent-cli-guide.md) ·
REST [`docs/rest-api.md`](docs/rest-api.md) · 运维 [`docs/operations.md`](docs/operations.md)

MIT License.
