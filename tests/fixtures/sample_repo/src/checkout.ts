import { postJson } from "./http";

export async function submitCheckout(orderId: string, amountCents: number) {
  return postJson("/api/payments/capture", {
    order_id: orderId,
    amount_cents: amountCents,
  });
}
