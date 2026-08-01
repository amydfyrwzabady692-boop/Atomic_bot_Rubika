import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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


def checked_decimal(value, *, minimum=Decimal("0.000001"),
                    maximum=Decimal("1000000"), scale=6,
                    label="مقدار اعشاری"):
    """Parse a finite bounded decimal without introducing binary-float error."""
    if isinstance(value, bool):
        raise ValueError(f"{label} نامعتبر است.")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} باید عدد معتبر باشد.") from None
    if not number.is_finite() or number < minimum or number > maximum:
        raise ValueError(f"{label} خارج از محدوده مجاز است.")
    quantum = Decimal(1).scaleb(-int(scale))
    return number.quantize(quantum, rounding=ROUND_HALF_UP)


def supplier_cost_toman(cost_usd, usd_toman_rate_value):
    cost = checked_decimal(cost_usd, label="هزینه دلاری")
    rate = checked_amount(
        usd_toman_rate_value,
        maximum=10_000_000,
        label="نرخ دلار",
    )
    return int((cost * Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def order_amounts(total, discount=0, wallet_paid=0):
    total = checked_amount(total, label="مبلغ سفارش")
    discount, wallet_paid = int(discount or 0), int(wallet_paid or 0)
    if discount < 0 or discount >= total:
        raise ValueError("تخفیف نامعتبر است.")
    net = total - discount
    if wallet_paid < 0 or wallet_paid > net:
        raise ValueError("برداشت کیف پول نامعتبر است.")
    return net, net - wallet_paid


def valid_card_number(value) -> bool:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) != 16 or len(set(digits)) == 1:
        return False
    checksum = 0
    for index, character in enumerate(digits):
        number = int(character) * (2 if index % 2 == 0 else 1)
        checksum += number - 9 if number > 9 else number
    return checksum % 10 == 0
