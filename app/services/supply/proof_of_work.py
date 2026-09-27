import hashlib


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + (8 - byte.bit_length())
    return bits


def solve_challenge(prefix: str, difficulty: int) -> str:
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{prefix}:{nonce}".encode()).digest()
        if leading_zero_bits(digest) >= difficulty:
            return str(nonce)
        nonce += 1
