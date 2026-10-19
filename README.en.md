# PromptMaster

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
python run.py --task "analyze sales data" --target-model deepseek-v3
python run.py --task "…" --prompt-file my_prompt.md   # improve your own draft; Δ = vs. your version
```

Exit codes are a protocol for agents: `0` passed / `1` delivered but below bar / `2` bad config / `3` run failed.
With `--json`, stdout is exactly one JSON object; all progress goes to stderr.

## When *not* to use it

1. **You just want a nicer-sounding prompt** → use [prompt-optimizer](https://github.com/linshenkx/prompt-optimizer).
2. **You need 100+ providers or red-team scanning** → use [promptfoo](https://github.com/promptfoo/promptfoo).
3. **You need a statistically defensible "it got better"** → not yet. Judge discrimination has no statistical support (see below).
4. **You need LangChain / LlamaIndex integration** → it only speaks `openai` / `anthropic`-compatible endpoints.

## What is actually different

- **Zero-call checks.** `check` / `diff` / `gate` render verdicts without a single API call. Most competitors
  require a paid round-trip before you learn anything.
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
| Product code | 74 files / ~25.6k lines (`pm/`) |
| Tests | 126 files / ~37.8k lines, **1,921 collected**, no API key, no live network |
| Coverage | 97.15% on `pm/` (floor 92), 99.05% on `pm/modelhub/*` (floor 82) |
| CI | 13 gates × Linux + Windows, ruff + `mypy --strict` + coverage ratchet |
| Interfaces | CLI (13 subcommands) · REST · MCP (13 tools) · web console · Tkinter desktop |

## Honest limitation (read before trusting any score)

The measuring stick is not aligned yet. Against human anchors: MAE **3.13**, bias **+3.09**
(systematic leniency), κ **0.074**; discrimination AUC **0.623** with 95% CI 0.341–0.841 —
**it covers 0.5**. In other words, at the current sample size there is *no statistical evidence*
that a higher judge score means a better output. The checklist scoring protocol was tested and
**rejected** (it collapsed 10 of 17 anchors onto 9.35 and stayed lenient), so it ships disabled.

Full experiment log, including the self-audit that found 6 of 11 "human" anchor scores were
fabricated: [`docs/evaluation.md`](docs/evaluation.md) §三十四 (Chinese).

---

中文完整文档：[`README.md`](README.md) · CLI 指南 [`docs/agent-cli-guide.md`](docs/agent-cli-guide.md) ·
REST [`docs/rest-api.md`](docs/rest-api.md) · 运维 [`docs/operations.md`](docs/operations.md)

MIT License.
