"""Payment orchestration for the fixture application."""

from dataclasses import dataclass

from gateway import PaymentGateway


@dataclass(frozen=True)
class PaymentReceipt:
    charge_id: str
    amount_cents: int


class PaymentService:
    def __init__(self, gateway: PaymentGateway) -> None:
        self.gateway = gateway

    def capture_order(self, order_id: str, amount_cents: int) -> PaymentReceipt:
        """Authorize and capture an order through the configured gateway."""

        authorization = self.gateway.authorize(order_id, amount_cents)
        charge_id = self.gateway.capture(authorization)
        return PaymentReceipt(charge_id=charge_id, amount_cents=amount_cents)
