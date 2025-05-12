import json
import logging
import random
from datetime import datetime
from pathlib import Path

import gevent
import grpc
from faker import Faker
from locust import constant_pacing, task
from locust.env import Environment
from locust.exception import LocustError
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING

import grpc_user
from pb import (
    auth_service_pb2_grpc,
    rpc_create_vacancy_pb2,
    rpc_signin_user_pb2,
    rpc_update_vacancy_pb2,
    vacancy_pb2,
    vacancy_service_pb2,
    vacancy_service_pb2_grpc,
)

DEFAULT_CREDENTIALS_DIR = Path(__file__).parent / 'credentials'
DEFAULT_CREDENTIALS_FILE = DEFAULT_CREDENTIALS_DIR / 'credentials.json'
DEFAULT_GRPC_SERVER_HOST = 'vacancies.cyrextech.dev:7823'
CREDENTIALS_FILE_SCHEMA = DEFAULT_CREDENTIALS_DIR / 'credentials.schema.json'
VACANCY_FETCH_BACKGROUND_TASK_INTERVAL_SEC = 45
VACANCY_SERVICE_TEST_FLOW_INTERVAL_SEC = 30

logger = logging.getLogger('vacancy_service_loader')
fake = Faker()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')


class VacancyServiceGrpcUser(grpc_user.GrpcUser):
    """A concrete implementation of GrpcUser for "VacancyService" gRPC service testing."""

    stub_class = vacancy_service_pb2_grpc.VacancyServiceStub
    wait_time = constant_pacing(VACANCY_SERVICE_TEST_FLOW_INTERVAL_SEC)

    def __init__(self, environment: Environment):
        self.host = environment.host or DEFAULT_GRPC_SERVER_HOST  # Get host from UI or fallback to default
        super().__init__(environment)

    @staticmethod
    def _load_random_credentials() -> tuple[str, str]:
        """
        Loads a random email/password pair from the default credentials file.

        Reads a JSON file containing a list of user credentials and randomly selects one.
        This is used to simulate different users during load testing.

        Returns:
            A tuple containing the selected email and password.
        """
        if not DEFAULT_CREDENTIALS_FILE.exists():
            raise LocustError(
                f'Credentials file not found at "{DEFAULT_CREDENTIALS_FILE}"! '
                f'Please provide a file matching the following schema: "{CREDENTIALS_FILE_SCHEMA}".'
            )
        with DEFAULT_CREDENTIALS_FILE.open('r') as fd:
            default_credentals = json.load(fd)
        random_user_creds = random.choice(default_credentals)  # NOQA: S311
        email, password = random_user_creds['email'], random_user_creds['password']

        return email, password

    def _authenticate_user(self, email: str, password: str):
        """
        Authenticates a user with the given email and password.

        Creates a gRPC client for the AuthService and sends a SignInUser request
        using the provided credentials. On successful authentication, stores the
        access token for future authenticated calls.
        """
        auth_stub = auth_service_pb2_grpc.AuthServiceStub(grpc.insecure_channel(self.host))
        request = rpc_signin_user_pb2.SignInUserInput(email=email, password=password)
        response = auth_stub.SignInUser(request)
        self._access_token = response.access_token

    def on_start(self):
        """
        Called automatically when a Locust user starts.

        Authenticates the user using randomly selected credentials and
        schedules periodic background vacancies fetch tasks.
        """
        self._authenticate_user(*self._load_random_credentials())
        self._add_background_task(self._schedule_vacancies_fetch)

    def _schedule_vacancies_fetch(self):
        """
        Schedules vacancies fetch every 45 seconds.

        Each scheduled fetch runs in its own greenlet and is tracked to ensure cleanup on shutdown.
        """
        while self.environment.runner.state not in (STATE_STOPPING, STATE_STOPPED, STATE_CLEANUP):
            self._add_background_task(self._fetch_vacancies)
            gevent.sleep(VACANCY_FETCH_BACKGROUND_TASK_INTERVAL_SEC)

    def _fetch_vacancies(self):
        """Fetches a list of all vacancies available on the server."""

        req = vacancy_service_pb2.GetVacanciesRequest()
        self.stub.GetVacancies(req, metadata=self._auth_metadata)

    @task
    def vacancy_flow(self):
        """
        Creates, updates, reads, and deletes a random vacancy.

        This method simulates the full lifecycle of a vacancy entity for testing purposes:
        1. Creates a vacancy with pseudo-random data.
        2. Updates the created vacancy with a new description.
        4. Fetches that specific vacancy.
        5. Deletes the vacancy.

        All operations are authenticated and use gRPC service stubs with appropriate metadata.
        """

        title = f'Vacancy {fake.uuid4()}-{datetime.now().isoformat()}'
        description = fake.text(max_nb_chars=200)
        division = fake.random_int(min=0, max=len(vacancy_pb2.Vacancy.DIVISION.values()) - 1)
        country = fake.country()
        req = rpc_create_vacancy_pb2.CreateVacancyRequest(
            Title=title, Description=description, Division=division, Country=country
        )
        resp = self.stub.CreateVacancy(req, metadata=self._auth_metadata)

        vacancy_id = resp.vacancy.Id  # vacancy ID for the further requests

        # Update vacancy
        req = rpc_update_vacancy_pb2.UpdateVacancyRequest(
            Id=vacancy_id,
            Description='Updated description',
        )
        self.stub.UpdateVacancy(req, metadata=self._auth_metadata)

        # Read vacancy
        req = vacancy_service_pb2.VacancyRequest(Id=vacancy_id)
        self.stub.GetVacancy(req, metadata=self._auth_metadata)

        # Delete vacancy
        req = vacancy_service_pb2.VacancyRequest(Id=vacancy_id)
        self.stub.DeleteVacancy(req, metadata=self._auth_metadata)
