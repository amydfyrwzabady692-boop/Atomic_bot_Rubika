import os

MIN_GATEWAY_AMOUNT = 1_000
MIN_WALLET_CHARGE = 10_000
MAX_PAYMENT_AMOUNT = int(os.getenv("MAX_PAYMENT_AMOUNT", "100000000"))


def checked_amount(value, *, minimum=1, maximum=MAX_PAYMENT_AMOUNT, label="مبلغ"):
    if isinstance(value, bool):
        raise ValueError(f"{label} نامعتبر است.")
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} باید عدد صحیح باشد.") from None
    if amount < minimum or amount > maximum:
        raise ValueError(f"{label} خارج از محدوده مجاز است.")
    return amount


def order_amounts(total, discount=0, wallet_paid=0):
    total = checked_amount(total, label="مبلغ سفارش")
    discount, wallet_paid = int(discount or 0), int(wallet_paid or 0)
    if discount < 0 or discount >= total:
        raise ValueError("تخفیف نامعتبر است.")
    net = total - discount
    if wallet_paid < 0 or wallet_paid > net:
        raise ValueError("برداشت کیف پول نامعتبر است.")
    return net, net - wallet_paid
