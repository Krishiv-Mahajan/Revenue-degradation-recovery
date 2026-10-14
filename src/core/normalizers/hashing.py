import json
import hashlib
from typing import Dict, Any

from src.core.domain.exceptions import NormalizationError


def generate_deterministic_hash(payload_data: Dict[str, Any]) -> str:
    """
    Creates a deterministic SHA-256 hash of a JSON payload dictionary.
    Keys are sorted, and spacing is removed to ensure equivalent JSON
    representations produce the same hash.
    """
    try:
        canonical_json = json.dumps(
            payload_data,
            sort_keys=True,
            separators=(',', ':')
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    except TypeError as e:
        raise NormalizationError(f"Failed to canonicalize payload for hashing: {str(e)}")
