import hashlib

from app.services.supply.proof_of_work import leading_zero_bits, solve_challenge


def test_leading_zero_bits_counts_full_and_partial_bytes() -> None:
    assert leading_zero_bits(bytes.fromhex("000f")) == 12
    assert leading_zero_bits(bytes.fromhex("10")) == 3


def test_solve_challenge_returns_valid_nonce() -> None:
    prefix = "test-prefix"

    nonce = solve_challenge(prefix, difficulty=8)

    digest = hashlib.sha256(f"{prefix}:{nonce}".encode()).digest()
    assert leading_zero_bits(digest) >= 8
