"""External payment gateway adapter for the fixture application."""


class PaymentGateway:
    def authorize(self, order_id: str, amount_cents: int) -> str:
        if amount_cents <= 0:
            raise ValueError("amount must be positive")
        return f"authorization-{order_id}"

    def capture(self, authorization: str) -> str:
        return authorization.replace("authorization-", "charge-", 1)
