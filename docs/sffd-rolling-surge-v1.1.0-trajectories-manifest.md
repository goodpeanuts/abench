# SFFD Rolling Surge 01: trajectory archive

Archive created for the current five-model diagnostic cohort on 2026-08-23.

## Selection rule

- Latest complete five-model cohort available at freeze time (00:19:22Z for the four
  1.1.0 runs; the Opus carry-over is retained alongside that cohort). Later isolated
  exploratory runs are intentionally omitted so every row shares one comparison
  window and one archive cut.
- Highest declared thinking mode: max for Opus, DeepSeek, Kimi and Qwen; xhigh for GPT-5.6 Sol.
- Same scenario, source scenario and initial state hashes across all five runs.
- verdict=ok, pi_exit_code=0, timed_out=false.
- The word successful here describes a clean harness/verifier run. It does not mean the episode was completed. Qwen is intentionally retained because its clean early stop is the most informative failure mode.

## Runs

| model | thinking | run directory | task/version evidence | overall | completion | sim end | caveat |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Opus 5 | max | results/pi-runs/20260822T215558Z-wodex-claude-opus-5-max-evaluation | verifier 0.2 + reference-track keys; task_version absent in raw manifest | 0.287464 | 1.0 | 7200, done | early 1.1.0 carry-over; prompt retains old end wording |
| GPT-5.6 Sol | xhigh | results/pi-runs/20260823T001010Z-wodex-gpt-gpt-5.6-sol-xhigh-evaluation | explicit task_version 1.1.0, contract v2, verifier 0.2 | 0.252751 | 1.0 | 7200, done | none |
| DeepSeek V4 Pro | max | results/pi-runs/20260823T001922Z-deepseek-deepseek-v4-pro-max-evaluation | explicit task_version 1.1.0, contract v2, verifier 0.2 | 0.208978 | 1.0 | 7200, done | none |
| Kimi K3 | max | results/pi-runs/20260823T001922Z-moonshot-kimi-k3-max-evaluation | explicit task_version 1.1.0, contract v2, verifier 0.2 | 0.197613 | 1.0 | 7200, done | none |
| Qwen 3.8 Max | max | results/pi-runs/20260823T001922Z-dashscope-qwen3.8-max-max-evaluation | explicit task_version 1.1.0, contract v2, verifier 0.2 | 0.076426 | 0.0 | 658, not done | clean process exit, but episode stopped before horizon |

## Included evidence per run

run-manifest.json, prompt.md, pi-events.jsonl, pi-stderr.log, harness.log,
state/actions.jsonl, state/event_log.jsonl, state/checkpoint.json,
state/replay_manifest.json, state/rng_manifest.json, state/state_hashes.jsonl,
and verifier/details.json, verifier/replay.json, verifier/reward.json.

The state/event_log.jsonl files contain simulator/verifier-private fields such as
travel and release durations. Those fields were not visible to the Agent through
emsctl; they are retained here as post-run audit evidence.

## Reading the scores

Official ranking is overall from verifier 0.2. overall_reference is an
ungated explanatory track and must not replace the official score. All five runs
have integrity=1, replay invariant true, and equal repeated state/trace hashes.
