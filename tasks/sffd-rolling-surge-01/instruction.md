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
emsctl finish --reason "no remaining work"
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
Continue advancing until the horizon (`done` is true).  Call `finish` only
under the episode-end rules below.

## Episode end and completion

- A completion is produced only when `advance` processes a unit's scheduled
  service-completion event.  There is no timer and no automatic release: a
  unit that is EN_ROUTE, ON_SCENE, or SERVICING in your latest observation
  stays in that status until you `advance` past its completion event.
- Every observation carries `horizon_seconds`, `done`, `finished`, and
  `finish_reason`.  `done` is true once logical time reaches the horizon or no
  scheduled events remain; further `advance` calls are then idempotent
  no-ops.  `finished` is true only after an accepted `finish`, which records
  your `--reason` in `finish_reason`.
- Unit statuses are AVAILABLE, BUSY (occupied before the slice starts;
  released by a scheduled event, not by anything you do), EN_ROUTE, ON_SCENE,
  SERVICING, REPOSITIONING, and CLEARING (briefly occupied after a service
  completes).  Incident statuses are QUEUED, UNASSIGNED, EN_ROUTE, ON_SCENE,
  SERVICING, COMPLETED, UNSERVED, and BACKGROUND.  These are the complete
  enumerations: there are no hidden intermediate states, no automatic release
  timer, and no field beyond the ones each observation prints.
- After `finish`, every write command and every `advance` is rejected.
  QUEUED/UNASSIGNED incidents are marked UNSERVED, and units that are
  EN_ROUTE, ON_SCENE, or SERVICING are frozen: their incidents never complete
  and score no coverage.
- Therefore keep advancing while any dispatchable incident is unserved or any
  controllable unit is in transit or on scene.  Call `finish` only when no
  useful work of any kind remains; an early `finish` forfeits every in-flight
  response.

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
