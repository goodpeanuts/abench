# SFFD Rolling Surge 01

You are dispatching a small emergency medical services fleet during a short,
changing incident window.  Your objective is to serve as many high-priority
incidents as possible, respect unit capability and occupancy constraints, and
avoid unnecessary reassignment or repositioning.

The task is grounded in an anonymized San Francisco Fire Department/EMS CAD
slice.  Time is **logical simulation time**; waiting for a model response does
not advance it.  New incidents become visible only when the simulator advances
to their arrival.  Do not assume that a historical dispatch is optimal.

## Interaction

Use the `emsctl` command supplied by the environment.  It emits one JSON object
per call.  Start with:

```sh
emsctl observe
```

Then use commands of the following form.  Always copy the latest
`state_version` and use a unique `action_id` for every write:

```sh
emsctl dispatch --incident INCIDENT_ID --unit UNIT_ID \
  --state-version STATE_VERSION --action-id ACTION_ID
emsctl reassign --incident INCIDENT_ID --from UNIT_ID --to UNIT_ID \
  --state-version STATE_VERSION --action-id ACTION_ID
emsctl reposition --unit UNIT_ID --zone ZONE \
  --state-version STATE_VERSION --action-id ACTION_ID
emsctl advance --next-event
emsctl finish --reason "all feasible work complete"
```

`emsctl` is the only environment surface available to the Agent.  In Harbor
it is a client for a simulator sidecar over a Unix socket; the scenario file,
calibration tables, random seed, checkpoints, and verifier are not mounted in
the Agent container.  A local checkout may use the direct CLI fallback with an
explicit `--scenario` and `--state-dir` for development.

Only currently observed incidents and available units may be used.  The
simulator rejects a stale state version, a conflicting reuse of an action id,
an unknown object, a busy unit, or a capability-incompatible assignment without
changing state.  Retrying the identical action id is idempotent.
Continue advancing until the horizon or finish when no useful action remains.

## What is scored

The independent verifier scores priority-weighted response time, quality-weighted
timely coverage, safety/validity, adaptation to newly revealed incidents,
operational efficiency, and replay integrity.  Response quality is bounded by
the incident SLA and squared, so repeatedly arriving near or after the SLA is
materially worse than serving the same calls early.  The operational
dimensions are gated by actual service; a no-op trace therefore receives only
the 0.05 integrity residual.  It reports both a continuous score and a strict
full-pass bit.  A valid but suboptimal action is penalized; it is not
automatically a zero.  Do not attempt to inspect files outside the task or
infer hidden future events.

## Reproducibility

The task stores an append-only event log and state hashes.  The same action log
must replay to the same final hash.  The verifier runs after the agent and has
access to hidden scenario material that is unavailable to the agent.

For local development without Harbor, run `bash tests/test.sh` from this task
directory.  The test suite uses a pure-Python structural fallback when the
simulator package or Harbor CLI is not installed; that fallback is diagnostic
only and is not a leaderboard score.

For a standalone container smoke test, compose
`environment/docker-compose.yaml` together with
`environment/docker-compose.local.yaml`. Harbor supplies the equivalent main
service build configuration automatically.
