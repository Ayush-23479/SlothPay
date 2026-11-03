import type { Metadata } from "next";
import { Suspense } from "react";
import { PassportSkeleton, SharedPassport } from "@/components/PassportScreens";

export const metadata: Metadata = {
  title: "Trust Passport – SlothPay",
  robots: { index: false, follow: false },
};

export default function SharedPassportPage(props: PageProps<"/passport/[token]">) {
  return (
    <Suspense fallback={<PassportSkeleton />}>
      <SharedPassport params={props.params} />
    </Suspense>
  );
}
