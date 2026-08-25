from opentelemetry.metrics import get_meter

_meter = get_meter("pool.fleet.runners")

JOBS_ACQUIRED = _meter.create_counter(
    "pool.fleet.runners.jobs_acquired",
    unit="{job}",
    description="Jobs taken off the GitHub queue",
)
RUNNERS_LAUNCHED = _meter.create_counter(
    "pool.fleet.runners.runners_launched",
    unit="{runner}",
    description="Attempts to submit a runner to the pool",
)
FLEET_SIZE = _meter.create_gauge(
    "pool.fleet.runners.fleet_size",
    unit="{runner}",
    description="Runners the controller counts as its own",
)
LAUNCH_DURATION = _meter.create_histogram(
    "pool.fleet.runners.launch_duration",
    unit="s",
    description="Time spent spreading a batch of runners across the pool",
)
