"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api, ApiRequestError } from "@/lib/api";
import type { Passport } from "@/lib/types";
import { ConditionCard } from "./ConditionCard";
import { ErrorsCard } from "./ErrorsCard";
import { PassportHeader } from "./PassportHeader";
import { PassportTimeline } from "./PassportTimeline";
import { ProtectionsCard } from "./ProtectionsCard";

export function PassportSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading the passport">
      <div className="bg-sky">
        <div className="mx-auto max-w-4xl space-y-4 px-4 py-14 sm:px-6">
          <div className="skeleton h-4 w-40" />
          <div className="skeleton h-16 w-full max-w-xl" />
          <div className="skeleton h-8 w-60" />
        </div>
      </div>
      <div className="mx-auto max-w-4xl space-y-4 px-4 py-8 sm:px-6">
        <div className="skeleton h-40" />
        <div className="skeleton h-24" />
      </div>
    </div>
  );
}

function PassportProblem({
  title,
  text,
  onRetry,
}: {
  title: string;
  text: string;
  onRetry?: () => void;
}) {
  return (
    <div className="mx-auto max-w-xl px-4 py-16 sm:px-6">
      <div className="card text-center" role="alert">
        <h1 className="text-3xl font-extrabold">{title}</h1>
        <p className="mt-3 text-muted">{text}</p>
        <div className="mt-6 flex flex-wrap justify-center gap-3">
          {onRetry ? (
            <button type="button" className="btn btn-outline" onClick={onRetry}>
              Try again
            </button>
          ) : null}
          <Link href="/" className="btn btn-primary">
            Back to dashboard
          </Link>
        </div>
      </div>
    </div>
  );
}

function PassportBody({ passport, paymentId }: { passport: Passport; paymentId?: string }) {
  return (
    <>
      <PassportHeader passport={passport} paymentId={paymentId} />
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-8 sm:px-6">
        <PassportTimeline timeline={passport.timeline} />
        <ConditionCard condition={passport.condition} />
        <ErrorsCard errors={passport.errors} />
        <ProtectionsCard protections={passport.protections} />
        {passport.view === "shared" ? (
          <p className="text-sm text-muted">
            This is a shared, read-only view. Account details and PayPal ids are hidden.
          </p>
        ) : null}
      </div>
    </>
  );
}

type Problem = { title: string; text: string; retry: boolean };

function usePassport(load: () => Promise<Passport>, notFound: Problem, key: string) {
  const [passport, setPassport] = useState<Passport | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    load()
      .then((value) => {
        if (alive) setPassport(value);
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiRequestError && e.status === 404) setProblem(notFound);
        else
          setProblem({
            title: "We could not load the passport",
            text:
              e instanceof ApiRequestError ? e.message : "Something went wrong. Please try again.",
            retry: true,
          });
      });
    return () => {
      alive = false;
    };
    // `load` and `notFound` are rebuilt every render; the key and attempt decide when to refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt]);

  function retry() {
    setProblem(null);
    setPassport(null);
    setAttempt((n) => n + 1);
  }
  return { passport, problem, retry };
}

const OWNER_404: Problem = {
  title: "Passport not found",
  text: "This payment does not exist, or it belongs to another account.",
  retry: false,
};
const SHARED_404: Problem = {
  title: "This link is not valid",
  text: "The link may have expired (they last 7 days) or been changed. Ask the sender for a new one.",
  retry: false,
};

/** The owner's passport. `params` is a promise in this Next.js version, read with use(). */
export function OwnerPassport({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { passport, problem, retry } = usePassport(() => api.getPassport(id), OWNER_404, id);
  if (problem) {
    return (
      <PassportProblem
        title={problem.title}
        text={problem.text}
        onRetry={problem.retry ? retry : undefined}
      />
    );
  }
  if (!passport) return <PassportSkeleton />;
  return <PassportBody passport={passport} paymentId={id} />;
}

/** The shared view: anyone holding a valid link, no login. */
export function SharedPassport({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  const { passport, problem, retry } = usePassport(
    () => api.getSharedPassport(token),
    SHARED_404,
    token,
  );
  if (problem) {
    return (
      <PassportProblem
        title={problem.title}
        text={problem.text}
        onRetry={problem.retry ? retry : undefined}
      />
    );
  }
  if (!passport) return <PassportSkeleton />;
  return <PassportBody passport={passport} />;
}