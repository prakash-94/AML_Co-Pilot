"""Google Cloud adapters: the real database, audit storage and job queue behind the service's small
interfaces. Each client library is imported only when its adapter is created, so everything else
(and every test) runs without them. These adapters have no automated tests of their own - they need a
real project - and are exercised by the deployed smoke test.
"""

import json
from typing import Any

from aml_copilot.service.audit import ObjectExistsError
from aml_copilot.service.jobs import Key, TransactFn

DISPATCH_DEADLINE_SECONDS = 1800  # Cloud Tasks' maximum for HTTP targets: 30 minutes


class FirestoreBackend:
    """`DocumentBackend` on Firestore. `transact` is a real Firestore transaction: it reads the keys,
    runs the rule function, writes the result, and Firestore re-runs it if another writer interfered."""

    def __init__(self, project: str):
        from google.cloud import firestore

        self._firestore = firestore
        self._client = firestore.Client(project=project or None)

    def get(self, collection: str, doc_id: str) -> dict | None:
        snapshot = self._client.collection(collection).document(doc_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def transact(self, keys: list[Key], fn: TransactFn) -> None:
        refs = {key: self._client.collection(key[0]).document(key[1]) for key in keys}

        @self._firestore.transactional
        def run(transaction: Any) -> None:
            docs: dict[Key, dict | None] = {}
            for key, ref in refs.items():
                snapshot = ref.get(transaction=transaction)
                docs[key] = snapshot.to_dict() if snapshot.exists else None
            for key, document in fn(docs).items():
                transaction.set(self._client.collection(key[0]).document(key[1]), document)

        run(self._client.transaction())

    def list_recent(self, collection: str, order_by: str, limit: int) -> list[dict]:
        query = self._client.collection(collection).order_by(order_by, direction=self._firestore.Query.DESCENDING)
        return [doc.to_dict() for doc in query.limit(limit).stream()]


class GcsObjects:
    """`ObjectBackend` on Cloud Storage. `put_if_absent` uses the `ifGenerationMatch=0` precondition,
    which Cloud Storage enforces: the upload fails if an object with that name already exists."""

    def __init__(self, bucket: str):
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)

    def put_if_absent(self, name: str, data: str) -> None:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._bucket.blob(name).upload_from_string(data, content_type="application/json", if_generation_match=0)
        except PreconditionFailed as exc:
            raise ObjectExistsError(name) from exc

    def list_names(self, prefix: str) -> list[str]:
        return sorted(blob.name for blob in self._bucket.client.list_blobs(self._bucket, prefix=prefix))

    def read(self, name: str) -> str:
        return self._bucket.blob(name).download_as_text()


class CloudTasksLauncher:
    """`JobLauncher` on Cloud Tasks. The task is named after the job, so asking twice for the same job
    creates one task; Cloud Tasks signs the call with an OIDC token for `invoker_email`."""

    def __init__(self, project: str, location: str, queue: str, worker_url: str, invoker_email: str):
        from google.cloud import tasks_v2

        self._tasks = tasks_v2
        self._client = tasks_v2.CloudTasksClient()
        self._queue_path = self._client.queue_path(project, location, queue)
        self._worker_url = worker_url.rstrip("/")
        self._invoker_email = invoker_email

    def enqueue(self, job_id: str) -> None:
        from google.api_core.exceptions import AlreadyExists
        from google.protobuf import duration_pb2

        task = self._tasks.Task(
            name=f"{self._queue_path}/tasks/{job_id}",
            http_request=self._tasks.HttpRequest(
                http_method=self._tasks.HttpMethod.POST,
                url=f"{self._worker_url}/internal/run",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"job_id": job_id}).encode("utf-8"),
                oidc_token=self._tasks.OidcToken(service_account_email=self._invoker_email, audience=self._worker_url),
            ),
            dispatch_deadline=duration_pb2.Duration(seconds=DISPATCH_DEADLINE_SECONDS),
        )
        try:
            self._client.create_task(parent=self._queue_path, task=task)
        except AlreadyExists:
            pass  # the same job was already queued - exactly what we want


__all__ = ["CloudTasksLauncher", "FirestoreBackend", "GcsObjects"]
