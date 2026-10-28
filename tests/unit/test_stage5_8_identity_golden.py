"""
Golden values for Stage 5-8 deterministic identities.

Record ids, input fingerprints and advisory-lock keys are derived from the payment attempt key and
persisted or used for cross-transaction serialization, so they must stay byte-identical when the
provider identity is threaded through Stages 5-8. The values below were recorded from the code at
c8b82d4. A change here changes the identity of every existing row or lock; never update a value to
make a test pass.
"""
import uuid
from datetime import datetime, timezone

import pytest

from src.core.domain.attribution_models import make_attribution_id
from src.core.domain.execution_models import make_command_id, make_observation_id
from src.core.domain.failure_prediction_models import compute_input_fingerprint, make_prediction_id
from src.core.domain.intervention_models import compute_decision_fingerprint, make_decision_id
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.intervention_repository import InterventionRepository

T = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
FEATURES = {"amount_minor_units": 50000, "payment_method": "card"}

# attempt key -> (prediction, decision, command, observation, attribution) ids for version 1
GOLDEN_IDS = {
    "pay_GOLDEN000001": (
        "54a0f4b0-0725-5fbb-adce-9bf02b1028f2",
        "9e51c98f-44d0-5738-8148-cc8d35c458f7",
        "4578c4bf-ed6b-5939-a1dd-8657f28727d5",
        "7c1d15fb-a6e4-554c-8531-0f466c38d02c",
        "014d1522-416a-595a-b1cd-be6e426abda1",
    ),
    "pay_syn_golden01": (
        "847f59e4-3ca6-5b44-8011-029cd42efea4",
        "e21231de-c820-56ea-b57f-a2ac49d3d5c9",
        "3fbe0c2e-076c-5fb8-9d64-fb838e69f9df",
        "39a8fc1a-e29b-5372-9e9c-32a803f49484",
        "e9514a46-cc01-5412-b414-412207276544",
    ),
    "paypal-order:8MC585209K746392H": (
        "ee6b7898-3201-5e2d-80d2-41d333395f39",
        "e68c3c9c-1f23-5ae7-9a2c-98da291d8551",
        "5fbec54d-0933-5b0e-a60c-2bf95db1acaa",
        "3ec61970-9307-5755-a3d4-cf4ddb1cc166",
        "0dbebf5e-03df-5060-a8f4-5cc6f0e45a7a",
    ),
}

# attempt key -> (Stage 5 input fingerprint, Stage 6 decision fingerprint)
GOLDEN_FINGERPRINTS = {
    "pay_GOLDEN000001": (
        "sha256:31087fb745e6ba92c9f11490c65f75255655ebc69bfb399dabfd66558a849504",
        "sha256:1fccbf1653af4716c2f81d927e3de9284f3a895411ef5103dc1babf2e41d3956",
    ),
    "pay_syn_golden01": (
        "sha256:dc907392effcd0f176c9f194e186975f69cf1bd89c543583491f2bbad9a3ecdd",
        "sha256:edbd5cc4bb33c946817c18c0170202219fd1ade9c173fe4bab4cb94c2242f412",
    ),
    "paypal-order:8MC585209K746392H": (
        "sha256:ae168e5077551394b1ee338c22dae9fcbae5d3873a69242af4748dcd85ea19c3",
        "sha256:51ed80a2ec8af7e0a6c1799bd09ad26861068944c780344e977d7074a5eff786",
    ),
}

# attempt key -> (version-allocation lock (k1, k2), command lock (k1, k2), attribution lock id)
GOLDEN_LOCKS = {
    "pay_GOLDEN000001": ((560766715, -1873449396), (-1077643195, 962161645), 5005967314423339321),
    "pay_syn_golden01": ((624518840, 1633531382), (772587071, -1201705977), 4593122061797056440),
    "paypal-order:8MC585209K746392H": ((241020103, 223795801), (1304804959, 240857865), 6899168613835692814),
}

GLOBAL_RATE_LIMIT_LOCK = (1416050241, 1380013125)


class _RecordingSession:
    """Captures the statement and parameters each repository lock method executes."""

    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


async def _lock_params(acquire, *args):
    session = _RecordingSession()
    await acquire(session)(*args)
    [(statement, params)] = session.calls
    assert statement.startswith("SELECT pg_advisory_xact_lock(")
    return params


@pytest.mark.parametrize("key", sorted(GOLDEN_IDS))
def test_record_ids_are_unchanged(key):
    prediction, decision, command, observation, attribution = map(uuid.UUID, GOLDEN_IDS[key])
    assert make_prediction_id(key, 1) == prediction
    assert make_decision_id(key, 1) == decision
    assert make_command_id(decision) == command
    assert make_observation_id(command, 1) == observation
    assert make_attribution_id(command, 1) == attribution


@pytest.mark.parametrize("key", sorted(GOLDEN_FINGERPRINTS))
def test_input_fingerprints_are_unchanged(key):
    stage5, stage6 = GOLDEN_FINGERPRINTS[key]
    assert compute_input_fingerprint(
        key, T, "30m", FEATURES, "DeterministicBaselineModel", "1.0.0", "v1"
    ) == stage5
    assert compute_decision_fingerprint(
        key, T, 50000, "INR", make_prediction_id(key, 1), 0.9, "PREDICTED",
        None, None, None, None, None, None, "p", "1.0.0",
    ) == stage6


@pytest.mark.parametrize("key", sorted(GOLDEN_LOCKS))
async def test_advisory_lock_keys_are_unchanged(key):
    version_lock, command_lock, attribution_lock = GOLDEN_LOCKS[key]
    command_id = make_command_id(make_decision_id(key, 1))

    for repository in (FailurePredictionRepository, InterventionRepository):
        params = await _lock_params(lambda s: repository(s).acquire_version_allocation_lock, key)
        assert (params["k1"], params["k2"]) == version_lock, repository.__name__

    params = await _lock_params(lambda s: ExecutionRepository(s).acquire_command_lock, command_id)
    assert (params["k1"], params["k2"]) == command_lock

    params = await _lock_params(lambda s: AttributionRepository(s).acquire_attribution_lock, command_id)
    assert params["lock_id"] == attribution_lock


async def test_global_rate_limit_lock_key_is_unchanged():
    params = await _lock_params(lambda s: InterventionRepository(s).acquire_global_rate_limit_lock)
    assert (params["k1"], params["k2"]) == GLOBAL_RATE_LIMIT_LOCK
