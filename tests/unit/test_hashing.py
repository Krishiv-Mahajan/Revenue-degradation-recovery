import json
from src.core.normalizers.razorpay import generate_deterministic_hash

def test_generate_deterministic_hash():
    dict1 = {"b": 2, "a": 1, "c": [3, 4]}
    dict2 = {"a": 1, "c": [3, 4], "b": 2}
    
    # Hashes should be exactly the same for differently ordered keys
    hash1 = generate_deterministic_hash(dict1)
    hash2 = generate_deterministic_hash(dict2)
    
    assert hash1 == hash2

    # And should change if payload changes
    dict3 = {"a": 1, "c": [3, 5], "b": 2}
    hash3 = generate_deterministic_hash(dict3)
    
    assert hash1 != hash3
