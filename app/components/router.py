# app/components/router.py
def even_split_qty(qty: float, n: int) -> float:
    if n <= 0:
        return 0.0
    return float(qty) / float(n)
