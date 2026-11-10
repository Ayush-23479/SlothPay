import type { Metadata } from "next";
import { CompareView } from "@/components/CompareView";

export const metadata: Metadata = {
  title: "With and without SlothPay",
  description: "The order step in the PayPal sandbox, with and without SlothPay's request ids.",
};

export default function ComparePage() {
  return <CompareView />;
}