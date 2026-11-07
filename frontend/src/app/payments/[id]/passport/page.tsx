import type { Metadata } from "next";
import { Suspense } from "react";
import { OwnerPassport, PassportSkeleton } from "@/components/PassportScreens";

export const metadata: Metadata = {
  title: "Trust Passport – SlothPay",
  robots: { index: false, follow: false },
};

export default function OwnerPassportPage(props: PageProps<"/payments/[id]/passport">) {
  return (
    <Suspense fallback={<PassportSkeleton />}>
      <OwnerPassport params={props.params} />
    </Suspense>
  );
}
