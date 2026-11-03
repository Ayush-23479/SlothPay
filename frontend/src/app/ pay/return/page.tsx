import { Suspense } from "react";
import { ReturnClient } from "@/components/ReturnClient";

export const metadata = { title: "Checking your payment – SlothPay" };

export default function ReturnPage() {
  return (
    <Suspense fallback={<p className="px-4 py-16 text-center text-muted">Loading…</p>}>
      <ReturnClient />
    </Suspense>
  );
}