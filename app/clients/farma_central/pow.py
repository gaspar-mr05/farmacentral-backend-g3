import hashlib


def leading_zero_bits(hex_hash: str) -> int:
    bits = 0
    for ch in hex_hash:
        n = int(ch, 16)
        if n == 0:
            bits += 4
            continue
        if n < 2:
            bits += 3
        elif n < 4:
            bits += 2
        elif n < 8:
            bits += 1
        break
    return bits


def solve_challenge(prefix: str, difficulty: int) -> str:
    nonce = 0
    while True:
        h = hashlib.sha256(f"{prefix}:{nonce}".encode()).hexdigest()
        if leading_zero_bits(h) >= difficulty:
            return str(nonce)
        nonce += 1
