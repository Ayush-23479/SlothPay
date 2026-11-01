from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from backend.config.settings import get_settings
from backend.orchestration.conditions import PaymentService
from backend.payments.paypal_client import PayPalClient
from backend.reliability.reconcile import DbReconcileStep
from backend.workflows.activities import PaymentActivities
from backend.workflows.common import TASK_QUEUE
from backend.workflows.conditional_pay import ConditionalPayWorkflow
from backend.workflows.reconcile_workflow import ReconcileWorkflow, SilenceWatcherWorkflow

log = logging.getLogger("slothpay.workflows")

WORKFLOWS = [ConditionalPayWorkflow, ReconcileWorkflow, SilenceWatcherWorkflow]


def build_worker(client: Client, activities: PaymentActivities, task_queue: str = TASK_QUEUE):
    return Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=activities.all())


async def run_worker() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with PayPalClient() as paypal:
            activities = PaymentActivities(
                PaymentService(session_factory, paypal),
                DbReconcileStep(session_factory, paypal),
            )
            client = await Client.connect(
                settings.TEMPORAL_ADDRESS, namespace=settings.TEMPORAL_NAMESPACE
            )
            log.info("Worker running on task queue %s at %s", TASK_QUEUE, settings.TEMPORAL_ADDRESS)
            await build_worker(client, activities).run()
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        log.info("Worker stopped")


if __name__ == "__main__":
    main()