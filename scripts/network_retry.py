"""Bounded recovery for idempotent release-network reads."""
import random
import os
from pathlib import Path
import time

NETWORK_RETRY_BUDGET_SECONDS = 30 * 60
NETWORK_BACKOFF_CAP_SECONDS = 60


class NetworkUnavailable(RuntimeError):
    pass


class RetryBudget:
    def __init__(self, seconds=NETWORK_RETRY_BUDGET_SECONDS, clock=time.monotonic, state_path=None):
        self.clock = clock
        self.started = clock()
        self.seconds = seconds
        configured_path = state_path or os.environ.get('NETWORK_RETRY_BUDGET_STATE')
        self.state_path = Path(configured_path) if configured_path else None
        self.spent = 0.0
        if self.state_path and self.state_path.exists():
            try:
                self.spent = max(0.0, float(self.state_path.read_text().strip()))
            except (OSError, ValueError):
                raise RuntimeError('Invalid shared network retry budget state') from None
        self.deadline = self.started + max(0.0, seconds - self.spent)

    def persist(self, elapsed):
        if self.state_path:
            self.spent += max(0.0, elapsed)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + '.tmp')
            temporary.write_text(f'{self.spent:.6f}\n')
            temporary.replace(self.state_path)


def retry_network_read(operation, stage, is_retryable, *, budget=None,
                       budget_seconds=NETWORK_RETRY_BUDGET_SECONDS, clock=None,
                       sleep=None, jitter=None, emit=print):
    """Retry a caller-supplied read only, against one bounded monotonic deadline."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    jitter = jitter or random.uniform
    budget = budget or RetryBudget(budget_seconds, clock)
    call_started = clock()
    deadline = clock() + max(0.0, budget.seconds - budget.spent)
    retries = 0
    try:
      while True:
        if clock() >= deadline:
            elapsed = budget.spent + clock() - call_started
            emit(f'NETWORK_UNAVAILABLE stage={stage} elapsed_s={elapsed:.1f} budget_s={budget.seconds}')
            raise NetworkUnavailable(stage)
        try:
            result = operation()
            elapsed = budget.spent + clock() - call_started
            if clock() >= deadline:
                emit(f'NETWORK_UNAVAILABLE stage={stage} elapsed_s={elapsed:.1f} budget_s={budget.seconds}')
                raise NetworkUnavailable(stage)
            if retries:
                emit(f'NETWORK_RESTORED stage={stage} elapsed_s={elapsed:.1f}')
            emit(f'RELEASE_CONTINUING stage={stage}')
            return result
        except Exception as error:
            if not is_retryable(error):
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                elapsed = budget.spent + clock() - call_started
                emit(f'NETWORK_UNAVAILABLE stage={stage} elapsed_s={elapsed:.1f} budget_s={budget_seconds}')
                raise NetworkUnavailable(stage) from None
            cap = min(NETWORK_BACKOFF_CAP_SECONDS, 2 ** min(retries, 6))
            delay = min(max(0.0, jitter(0.0, cap)), remaining)
            emit(f'NETWORK_RETRYING stage={stage} retry_in_s={delay:.1f} elapsed_s={budget.spent + clock()-call_started:.1f}')
            retries += 1
            sleep(delay)
    finally:
        if retries:
            budget.persist(clock() - call_started)
